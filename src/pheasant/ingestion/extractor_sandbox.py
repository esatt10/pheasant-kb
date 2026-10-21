"""WASM-sandboxed PDF text extraction.

Why sandboxing rather than acceleration
---------------------------------------
Phase 34 shipped two WASM tracks: a capability-scoped sandbox for untrusted
guest code (34.1-34.3) and selective hot-loop acceleration (34.5). Document
extraction belongs to the **first** track, and the 34.4/34.6 measurements say
so plainly: a WASM ``Store``+``Instance`` costs ~115 µs fixed, and extraction
runs **once per file** — the same "small units, called often" shape that made
34.6 (WASM chunking) a documented NO-GO. Speed is not the argument here, and
this module makes no speed claim.

The argument is the threat model 34.2 already named: *"Confluence's in-process
BeautifulSoup parse of untrusted remote XHTML"*. PDF is the sharper version of
that same problem. PDF bytes arrive at a region from Google Drive, Slack
uploads, Confluence attachments and IMAP mail — none of it authored by the
operator, all of it fed to a parser. In-process that parse sits inside the
sync worker with the worker's ambient authority: environment variables holding
every configured connector's API token, the writable ``/state`` directory, and
network egress. A malicious PDF that subverts the parser inherits all of it.

Run through this module the tokenizer instead gets:

- a **fuel cap** — a hostile file that drives the scanner into a pathological
  loop exhausts its budget and raises :class:`SandboxFuelExhausted` rather
  than hanging the sync;
- a **linear-memory cap** — allocation-amplifying input traps instead of
  invoking the host OOM killer;
- **zero host capabilities** — the linker defines *no* imports at all, not
  even 34.1's ``host_fetch`` pair, so there is no ambient environment,
  filesystem, or network for a subverted parse to reach. A guest that so much
  as declares an import fails to instantiate (34.3's guarantee).

Honest scope boundary
---------------------
The guest receives **already-inflated** content streams: the host still runs
``zlib`` to decompress them (see
:func:`pheasant.ingestion.extractor.iter_pdf_content_streams`). So this is a
*partial* sandbox, and saying otherwise would overstate it — in the same
spirit as 34.2's documented deviation about host-side directory listing. The
split is deliberate rather than incidental:

- ``zlib`` is a small, extremely well-audited surface whose only
  attacker-driven risk is output amplification, and that is already bounded
  by ``MAX_STREAM_INFLATED_BYTES``.
- The *tokenizer* is where attacker-controlled bytes drive unbounded loops,
  index arithmetic, and nesting depth. That is the part with real
  memory-safety and denial-of-service surface, and that is the part that runs
  inside the cap.

Equations (``ingestion.extractor.math: latex``)
-----------------------------------------------
Rebuilding an equation needs a PDF's object dictionaries, fonts, CMaps and
the positions its content streams draw glyphs at
(:mod:`pheasant.ingestion.pdf_glyphs`). All of that syntax is tokenized by the
guest's ``pdf_lex``, the twin of :func:`~pheasant.ingestion.pdf_objects.lex_pdf`
held byte-for-byte equal to it, so the host walks typed tokens and never
parses attacker bytes itself. What stays on the host is what already did:
finding ``N G obj`` boundaries and inflating streams, both bounded.

Correctness is not taken on trust
---------------------------------
The guest is a port, so it can drift from the Python reference in ways a
sandbox cannot catch — a wrong answer traps nothing. ``pdf_scan_text`` in the
vendored Rust crate is therefore asserted **byte-for-byte identical** to
:func:`~pheasant.ingestion.extractor.scan_pdf_content_stream` in
``tests/test_document_extraction.py``, the same discipline 34.5 used ("every
scale point asserts the WASM output is byte-for-byte identical to the Python
reference before any timing counts").

Failure posture
---------------
A security control that silently degrades is worse than one that fails loudly,
so this module deviates from 34.5's fall-back-on-any-exception policy in one
specific way: if ``wasmtime`` is missing, construction **raises** with an
actionable hint rather than quietly extracting unsandboxed — an operator who
wrote ``provider: sandboxed`` asked for isolation and must not silently get
none. Per-*file* failures behave the opposite way: a file that exhausts its
fuel or traps yields no text for that file and logs, never aborting the sync.
"""

from __future__ import annotations

import hashlib
import logging
import struct
import tempfile
import threading
from pathlib import Path

from pheasant.ingestion._modal import sidecar_text as _sidecar_text
from pheasant.ingestion.extractor import (
    DocumentExtractor,
    _format_for,
    _tidy,
    extract_builtin,
    iter_pdf_content_streams,
)
from pheasant.sandbox.wasm_runtime import (
    SandboxError,
    WasmRuntimeUnavailable,
    guest_failures,
    translate_trap,
)

logger = logging.getLogger(__name__)

try:
    import wasmtime
except ImportError:  # pragma: no cover - exercised only without the [wasm] extra
    wasmtime = None  # type: ignore[assignment]

_ACCEL_WASM_PATH = Path(__file__).resolve().parents[1] / "sandbox" / "accel" / "accel.wasm"

# Untrusted-input budget. Deliberately far tighter than the trusted
# accelerator's 50e9 fuel / 512 MB (`sandbox/accel/loader.py`): that path runs
# vendored code over already-validated in-process graph data, this one runs
# over bytes an attacker chose. Sized to comfortably scan a large real content
# stream while still bounding a pathological one. Note the *module* is shared
# with the accelerator — limits live on the Store, not the Module, so reusing
# the compiled binary costs nothing in isolation.
SANDBOX_FUEL = 2_000_000_000
SANDBOX_MAX_MEMORY_BYTES = 128 * 1024 * 1024

_lock = threading.Lock()
_engine: object | None = None
_module: object | None = None


def _cache_path(wasm_bytes: bytes) -> Path:
    digest = hashlib.sha256(wasm_bytes).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "pheasant-wasm-cache" / f"extract-{digest}.cwasm"


def _engine_and_module() -> tuple[object, object]:
    """Compile-once, AOT-cached Engine+Module for the extraction guest.

    34.4's Finding #1 — a per-call ``wasmtime.Module(...)`` JIT-compiles from
    scratch (~103 ms) and makes WASM slower than the Python it replaces — is
    not optional trivia here: extraction is per-file, so a per-file compile
    would dominate everything. Same AOT-serialization fix as
    ``sandbox/accel/loader.py``, with a distinct cache filename prefix so the
    two paths never read each other's artifact.
    """
    global _engine, _module
    if wasmtime is None:
        raise WasmRuntimeUnavailable(
            "ingestion.extractor.provider='sandboxed' needs the optional 'wasmtime' "
            "dependency. Install with: pip install 'pheasant-kb[wasm]' "
            "(or choose provider 'auto'/'native'/'builtin')"
        )
    with _lock:
        if _module is None:
            config = wasmtime.Config()
            config.consume_fuel = True
            engine = wasmtime.Engine(config)
            wasm_bytes = _ACCEL_WASM_PATH.read_bytes()
            cache = _cache_path(wasm_bytes)
            module = None
            if cache.exists():
                try:
                    module = wasmtime.Module.deserialize_file(engine, str(cache))
                except wasmtime.WasmtimeError as exc:
                    # Fails closed on an engine/architecture mismatch rather
                    # than loading a stale artifact.
                    logger.info("extraction WASM cache unusable, recompiling: %s", exc)
            if module is None:
                module = wasmtime.Module(engine, wasm_bytes)
                try:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    tmp = cache.with_suffix(".tmp")
                    tmp.write_bytes(module.serialize())
                    tmp.replace(cache)
                except OSError as exc:
                    logger.warning("could not write extraction WASM cache: %s", exc)
            _engine, _module = engine, module
        assert _engine is not None and _module is not None
        return _engine, _module


def scan_pdf_content_stream_wasm(stream: bytes) -> str:
    """Run one inflated content stream through the sandboxed guest tokenizer.

    A **fresh** ``Store``+``Instance`` per stream, so one hostile file can
    neither drain a later file's fuel budget nor leave residue in a shared
    linear memory. The linker defines no imports, so the instance has no host
    surface at all.
    """
    if not stream:
        return ""
    engine, module = _engine_and_module()
    assert wasmtime is not None

    store = wasmtime.Store(engine)
    store.set_fuel(SANDBOX_FUEL)
    store.set_limits(memory_size=SANDBOX_MAX_MEMORY_BYTES)
    # No imports defined: zero ambient capabilities for the guest.
    linker = wasmtime.Linker(engine)
    instance = linker.instantiate(store, module)
    exports = instance.exports(store)

    try:
        in_ptr = exports["wasm_alloc"](store, len(stream))
        exports["memory"].write(store, stream, in_ptr)
        out_len_ptr = exports["wasm_alloc"](store, 4)
        out_ptr = exports["pdf_scan_text"](store, in_ptr, len(stream), out_len_ptr)
        (out_len,) = struct.unpack(
            "<I", bytes(exports["memory"].read(store, out_len_ptr, out_len_ptr + 4))
        )
        raw = bytes(exports["memory"].read(store, out_ptr, out_ptr + out_len))
    except guest_failures() as trap:
        # Trap and WasmtimeError are siblings in wasmtime's hierarchy, so
        # catching only Trap lets a raw runtime type escape into a sync.
        raise translate_trap(trap, "pdf_scan_text") from trap
    return raw.decode("utf-8", errors="replace")


def lex_pdf_wasm(data: bytes) -> list[tuple[int, bytes]]:
    """:func:`~pheasant.ingestion.pdf_objects.lex_pdf`, run in the guest.

    A fresh instance per call, as for the scanner. Input the guest rejects
    (fuel, memory) yields no tokens: that object or stream is skipped, the
    way a trapping content stream already costs only its own text.
    """
    from pheasant.ingestion.pdf_objects import deserialize_tokens

    if not data:
        return []
    engine, module = _engine_and_module()
    assert wasmtime is not None
    store = wasmtime.Store(engine)
    store.set_fuel(SANDBOX_FUEL)
    store.set_limits(memory_size=SANDBOX_MAX_MEMORY_BYTES)
    instance = wasmtime.Linker(engine).instantiate(store, module)
    exports = instance.exports(store)
    try:
        in_ptr = exports["wasm_alloc"](store, len(data))
        exports["memory"].write(store, data, in_ptr)
        out_len_ptr = exports["wasm_alloc"](store, 4)
        out_ptr = exports["pdf_lex"](store, in_ptr, len(data), out_len_ptr)
        (out_len,) = struct.unpack(
            "<I", bytes(exports["memory"].read(store, out_len_ptr, out_len_ptr + 4))
        )
        raw = bytes(exports["memory"].read(store, out_ptr, out_ptr + out_len))
    except guest_failures() as trap:
        logger.warning("sandboxed PDF lexer rejected input: %s", translate_trap(trap, "pdf_lex"))
        return []
    return deserialize_tokens(raw)


def extract_pdf_text_sandboxed(content: bytes, math: bool = False) -> str:
    """Sandboxed twin of ``extract_pdf_text_builtin``.

    Host-side stream carving + inflate (bounded), guest-side tokenizing
    (capped, capability-free). A single stream that traps is skipped rather
    than failing the whole document — a hostile object embedded in an
    otherwise-legitimate PDF costs that object's text, not the file's.
    """
    from pheasant.ingestion.extractor import MAX_TOTAL_TEXT_CHARS

    if math:
        try:
            from pheasant.ingestion.pdf_glyphs import math_text

            rewritten = math_text(content, lexer=lex_pdf_wasm, scan=_scan_or_empty)
        except Exception as exc:  # noqa: BLE001 - equations are an improvement, never a failure
            logger.warning("sandboxed PDF equation reading failed (%s); using the plain scan", exc)
            rewritten = None
        if rewritten is not None:
            return _tidy(rewritten)
    parts: list[str] = []
    total = 0
    for stream in iter_pdf_content_streams(content):
        try:
            text = scan_pdf_content_stream_wasm(stream)
        except SandboxError as exc:
            logger.warning("sandboxed PDF scan rejected a content stream: %s", exc)
            continue
        if not text.strip():
            continue
        parts.append(text)
        total += len(text)
        if total >= MAX_TOTAL_TEXT_CHARS:
            break
    return _tidy("\n".join(parts))


def _scan_or_empty(stream: bytes) -> str:
    try:
        return scan_pdf_content_stream_wasm(stream)
    except SandboxError as exc:
        logger.warning("sandboxed PDF scan rejected a content stream: %s", exc)
        return ""


class SandboxedExtractor:
    """PDF extraction with the tokenizer under a WASM fuel/memory cap.

    **Only PDF is sandboxed.** Every other format keeps its pure-Python
    reader: DOCX/PPTX/XLSX/EPUB are ``zipfile`` + ``xml.etree``, RTF is a
    string tokenizer, HTML is BeautifulSoup, and legacy DOC is this project's
    own bounded binary reader. All are memory-safe Python over bounded input,
    so wrapping them would add per-file cost for no threat-model gain.

    Claiming a blanket "sandboxed extraction" while only PDF is capped would be
    the kind of overstatement this repo's run summaries exist to prevent. The
    honest ranking of remaining exposure: ``.doc`` is the next most
    attacker-facing format here, since it is the only one where this project
    does its own offset arithmetic on untrusted binary structures — but in
    Python that arithmetic is bounds-checked, so the failure mode is a raised
    exception or a bounded allocation, not memory corruption. That is why it
    gets explicit caps (:mod:`pheasant.ingestion.msdoc`) rather than a guest.
    """

    def __init__(
        self,
        fallback: DocumentExtractor | None = None,
        model: str = "sandboxed",
        math: bool = False,
    ):
        self.model = model
        self.calls = 0
        self.math = math
        self._fallback = fallback
        # Fail loudly at construction when the extra is absent: an operator
        # who asked for isolation must not silently run without it.
        _engine_and_module()

    def extract(self, content: bytes, relative_path: str, sidecar: bytes | None = None) -> str:
        authored = _sidecar_text(sidecar)
        if authored is not None:
            return authored
        self.calls += 1
        kind = _format_for(relative_path)
        if kind == "pdf":
            return extract_pdf_text_sandboxed(content, math=self.math)
        return extract_builtin(kind, content, math=self.math)


def reset_for_tests() -> None:
    """Drop the cached Engine/Module (tests only)."""
    global _engine, _module
    with _lock:
        _engine = None
        _module = None

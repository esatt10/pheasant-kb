"""Reference sandboxed connector (Synapse Step 34.2).

Ports the canonical third-party connector shape
(``tests/fixtures/pheasant-connector-example``'s ``StaticDirConnector``) to
run its per-item content transform inside a :class:`~pheasant.sandbox.
wasm_runtime.WasmSandbox` guest instead of trusted in-process Python.

**Scope note:** directory listing and the raw file read stay host-side —
they mirror the native connector exactly and are guarded by
:func:`pheasant.security.path_policy.resolve_under`, reused rather than
duplicated. The guest's job is the one per-item operation that is the
actual threat-model target named in ``docs/SYNAPSE_INTEGRATION.md`` §5
("bounding execution memory for untrusted parses" — e.g. Confluence's
in-process BeautifulSoup parse of untrusted remote XHTML): transforming
already-fetched bytes under a fuel + memory cap. A guest that does its own
WASI ``fd_readdir``/``path_open`` filesystem syscalls is future work, not
required to validate the harness (see the Step 34.2 run summary for the
full rationale).

This is one connector tier, opted into per-source via
``connector.runtime: sandboxed`` (default stays ``"native"`` —
``sync/connectors.py:connector_for_source`` routes here only when a source
asks for it, so an untouched region is byte-identical to pre-34.1).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pheasant.sandbox.wasm_runtime import HostCapabilities, WasmSandbox
from pheasant.security.path_policy import resolve_under
from pheasant.sync.connectors import (
    ConnectorItem,
    ConnectorPayload,
    ConnectorUnavailable,
    SourceConnector,
)

_GUEST_DIR = Path(__file__).parent / "guests"
_DEFAULT_GUEST_PATH = _GUEST_DIR / "reference_connector.wat"

# Fixed guest-memory layout for the normalize() ABI: input at offset 0
# (up to IN_CAP bytes), output at OUT_OFFSET (up to OUT_CAP bytes). The
# guest module declares 4 pages (256 KiB) of memory, comfortably above both.
IN_CAP = 65536
OUT_OFFSET = 65536
OUT_CAP = 65536


class SandboxedConnector(SourceConnector):
    connector_type = "sandboxed_staticdir"
    complete_listing = True  # a walk of the scoped root

    def __init__(self, source: Any, state: Any) -> None:
        super().__init__(source, state)
        # Scope the readable root exactly like the native connector would —
        # resolve_under is the one filesystem guard in this codebase, reused
        # here rather than re-implemented for the sandboxed tier.
        self._root = resolve_under(source.path, [source.path]) if source.path else source.path
        guest_path = source.connector.wasm_module_path
        self._guest_wat = (
            Path(guest_path).read_text() if guest_path else _DEFAULT_GUEST_PATH.read_text()
        )

    def _sandbox(self) -> WasmSandbox:
        capabilities = HostCapabilities(
            allowed_hosts=frozenset(h.lower() for h in self.source.connector.allowed_hosts),
        )
        return WasmSandbox(self._guest_wat, capabilities=capabilities)

    def _normalized_content(self, path: Path) -> bytes:
        raw = path.read_bytes()
        if len(raw) > IN_CAP:
            raise ConnectorUnavailable(
                f"{path}: content ({len(raw)} bytes) exceeds the sandboxed "
                f"connector's {IN_CAP}-byte input buffer"
            )
        sandbox = self._sandbox()
        sandbox.write_memory(0, raw)
        n = sandbox.call("normalize", 0, len(raw), OUT_OFFSET, OUT_CAP)
        if n < 0:
            raise ConnectorUnavailable(f"sandboxed guest failed to normalize {path}")
        return sandbox.read_memory(OUT_OFFSET, n)

    def list_items(self) -> list[ConnectorItem]:
        root = self._root
        if not root.is_dir():
            return []
        items: list[ConnectorItem] = []
        for path in sorted(root.rglob("*.txt")):
            stat = path.stat()
            content = self._normalized_content(path)
            items.append(
                ConnectorItem(
                    identity=(
                        f"sandboxed_staticdir:{self.source.name}:"
                        f"{path.relative_to(root).as_posix()}"
                    ),
                    relative_path=path.relative_to(root).as_posix(),
                    uri=path.resolve().as_uri(),
                    mime_type="text/plain",
                    size_bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    mtime=datetime.fromtimestamp(stat.st_mtime, UTC)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    metadata={"path": str(path), "sandboxed": True},
                )
            )
        return items

    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        content = self._normalized_content(self._root / item.relative_path)
        return ConnectorPayload(
            item=item,
            content=content,
            mime_type=item.mime_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            mtime=item.mtime,
            metadata=dict(item.metadata),
        )

    def checkpoint_from_items(
        self, items: list[ConnectorItem]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        newest = max((item.mtime or "" for item in items), default="")
        return {"item_count": len(items)}, {"max_mtime": newest}

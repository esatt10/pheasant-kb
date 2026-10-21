"""Equations in PDFs and DOCX extracted as LaTeX (``ingestion.extractor.math``).

Three properties, each on every provider:

1. **It reads the equation.** Fixtures from three typesetters (pdfTeX with
   Computer Modern Type 1 fonts and no ToUnicode; LibreOffice with OpenSymbol;
   matplotlib's BaKoMa TrueType Computer Modern, which shows α as ``®``) and a
   Word-style DOCX produce named TeX, asserted as exact strings.
2. **It touches nothing else.** A document with no equation extracts
   byte-identically with the switch on, and with it off every reader is the
   reader it was.
3. **The sandbox boundary holds.** The guest's ``pdf_lex`` is byte-for-byte
   the reference lexer, and the sandboxed provider's text equals the builtin's.

The fixtures are committed with their sources (``tests/fixtures/math``):
pdfTeX, LibreOffice and pandoc are not part of the test environment.
"""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import ExtractorSettings, PheasantConfig
from pheasant.ingestion.extractor import (
    AutoExtractor,
    BuiltinExtractor,
    NativeExtractor,
    build_extractor,
    extract_docx_text_builtin,
    extract_pdf_text_builtin,
    iter_pdf_content_streams,
)
from pheasant.ingestion.pdf_objects import lex_pdf, serialize_tokens
from pheasant.ingestion.pdf_pages import pdf_page_math_texts, pdf_page_texts, pdf_reader_version

MATH = Path(__file__).parent / "fixtures" / "math"
DOCS = Path(__file__).parent / "fixtures" / "sample_workspace" / "documents"

TEX_ARTICLE = [
    r"$X\in\mathbb{R}^{n\times p}$",
    r"$$L(\beta)=\sum_{i=1}^n(y_i-x_i^{\top}\beta)^2, \tag{1}$$",
    r"$\operatorname{Var}(\hat{\beta})=\sigma^2(X^{\top}X)^{-1}$",
    r"$$f(x)=\frac{1}{\sqrt{2\pi\sigma^2}}\exp\left( -\frac{(x-\mu)^2}{2\sigma^2} \right).$$",
    r"$\bar{x}=\frac{1}{n}\sum_{i=1}^n x_i$",
    r"$\lim_{n\to\infty}(1+\frac{1}{n})^n=e$",
    r"\begin{cases} x & \text{ if }x\geq 0, \\ -x & \text{ otherwise }. \end{cases}",
    r"A=\begin{pmatrix} a & b \\ c & d \end{pmatrix}",
    r"Roots such as $\sqrt[3]{x+1}$, and",
    r"$\alpha\leq\beta\neq \gamma$",
    r"$x_{i_j}\neq y'$",
    r"$n>p$",
]

OFFICE = [
    r"Energy is $E=mc^2$ and the golden ratio is $\varphi=\frac{1+\sqrt{5}}{2}$.",
    r"$$\int_0^{\infty}e^{-x^2}dx=\frac{\sqrt{\pi}}{2}$$",
    r"$p(\theta\mid x)=\frac{p(x\mid\theta)p(\theta)}{p(x)}$",
    r"$$A=\begin{pmatrix} a & b \\ c & d \end{pmatrix},\det A=ad-bc$$",
    r"$$\lim_{n\to\infty}{\left( 1+\frac{1}{n} \right)}^n=e$$",
    r"$\|v\|_2=\sqrt{\sum_k v_k^2}$",
    r"\binom{n}{k}=2^n",
    r"for data $x_1,\ldots,x_n$.",
]

BAKOMA = [
    r"$E=mc^2$",
    r"$\varphi=\frac{1+\sqrt{5}}{2}$",
    r"$\bar{x}=\frac{1}{n}\sum_{i=1}^n x_i$",
    r"$$\int_0^{\infty}e^{-x^2}dx=\frac{\sqrt{\pi}}{2}$$",
    r"$$\nabla\cdot\mathbf{E}=\frac{\rho}{\varepsilon_0}$$",
    r"$\alpha\leq\beta\neq\gamma$",
    r"\sum_{k=0}^n a_k=2^n",
]

DOCX = [
    r"Energy is $E=mc^2$ and the golden ratio is $\varphi=\frac{1+\sqrt{5}}{2}$.",
    r"$\overline{x}=\frac{1}{n}\sum_{i=1}^n x_i$",
    r"$$\int_0^{\infty}e^{-x^2}\,dx=\frac{\sqrt{\pi}}{2}$$",
    r"$$A=\begin{pmatrix} a & b \\ c & d \end{pmatrix},\quad\det A=ad-bc$$",
    r"$$f\left( x \right)=\begin{cases} x^2 & x\geq 0 \\ -x & x<0 \end{cases}$$",
    r"$$\lim_{n\to\infty}{\left( 1+\frac{1}{n} \right)}^n=e$$",
    r"$\|v\|_2=\sqrt{\sum_k v_k^2}$",
    r"$$\sum_{k=0}^n \binom{n}{k}=2^n,\qquad\prod_{j=1}^m a_j \leq\max_j a_j^m$$",
]


def _sandboxed() -> Any:
    pytest.importorskip("wasmtime", reason="the [wasm] extra is optional")
    from pheasant.ingestion.extractor_sandbox import SandboxedExtractor

    return SandboxedExtractor(math=True)


def _providers() -> list[Any]:
    return ["native", "builtin", "sandboxed"]


def _extractor(name: str, math: bool = True) -> Any:
    if name == "sandboxed":
        extractor = _sandboxed()
        extractor.math = math
        return extractor
    return {"native": NativeExtractor, "builtin": BuiltinExtractor}[name](math=math)


def _equations(text: str) -> list[str]:
    return [m.group(0) for m in re.finditer(r"\$\$[\s\S]+?\$\$|\$[^$\n]+?\$", text)]


def _balanced(tex: str) -> bool:
    depth = 0
    for char in re.sub(r"\\[{}]", "", tex):
        depth += {"{": 1, "}": -1}.get(char, 0)
        if depth < 0:
            return False
    return depth == 0


# ---------------------------------------------------------------------------
# 1. It reads the equation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", _providers())
@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("tex_article.pdf", TEX_ARTICLE),
        ("office_equations.pdf", OFFICE),
        ("bakoma_equations.pdf", BAKOMA),
    ],
)
def test_a_pdf_equation_is_written_as_latex(
    provider: str, fixture: str, expected: list[str]
) -> None:
    text = _extractor(provider).extract((MATH / fixture).read_bytes(), fixture)
    missing = [tex for tex in expected if tex not in text]
    assert not missing, f"{provider} missed {missing}\n---\n{text}"
    assert all(_balanced(tex) for tex in _equations(text))
    assert not re.search(r"ACCENT:|RADICAL|PIECE:", text)


def test_the_stdlib_readers_and_the_sandbox_agree_on_every_equation() -> None:
    sandboxed = _sandboxed()
    for fixture in ("tex_article.pdf", "office_equations.pdf", "bakoma_equations.pdf"):
        content = (MATH / fixture).read_bytes()
        assert sandboxed.extract(content, fixture) == BuiltinExtractor(math=True).extract(
            content, fixture
        )


@pytest.mark.parametrize("reader", ["builtin", "native"])
def test_a_docx_equation_is_written_as_latex(reader: str) -> None:
    content = (MATH / "office_equations.docx").read_bytes()
    text = _extractor(reader).extract(content, "equations.docx")
    missing = [tex for tex in DOCX if tex not in text]
    assert not missing, f"{reader} missed {missing}\n---\n{text}"


def test_both_docx_readers_write_the_same_text() -> None:
    content = (MATH / "office_equations.docx").read_bytes()
    assert NativeExtractor(math=True).extract(content, "e.docx") == extract_docx_text_builtin(
        content, math=True
    )


def test_a_numbered_equation_is_tagged_and_a_footnote_mark_is_not_math() -> None:
    text = NativeExtractor(math=True).extract((MATH / "tex_article.pdf").read_bytes(), "a.pdf")
    assert r"\tag{1}" in text
    assert "1We assume $n>p$." in text  # the mark stays prose, the formula does not


# ---------------------------------------------------------------------------
# 2. It touches nothing else
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", _providers())
@pytest.mark.parametrize("fixture", [MATH / "office_prose.pdf", DOCS / "handbook.pdf"])
def test_a_document_without_equations_is_byte_identical(provider: str, fixture: Path) -> None:
    content = fixture.read_bytes()
    assert _extractor(provider, math=True).extract(content, fixture.name) == _extractor(
        provider, math=False
    ).extract(content, fixture.name)


def test_fallback_greek_and_symbols_in_office_prose_are_not_equations() -> None:
    """LibreOffice sets μ, α, β and ± in a fallback font: prose, not math."""

    text = NativeExtractor(math=True).extract((MATH / "office_prose.pdf").read_bytes(), "p.pdf")
    assert "$" not in text.replace("$4.2M", "")


def test_off_is_the_reader_it_always_was() -> None:
    pdf = (MATH / "tex_article.pdf").read_bytes()
    docx = (MATH / "office_equations.docx").read_bytes()
    assert NativeExtractor().extract(pdf, "a.pdf") == NativeExtractor(math=False).extract(
        pdf, "a.pdf"
    )
    assert BuiltinExtractor().extract(pdf, "a.pdf") == extract_pdf_text_builtin(pdf)
    assert r"\frac" not in BuiltinExtractor().extract(pdf, "a.pdf")
    plain_docx = extract_docx_text_builtin(docx)
    assert "\\frac" not in plain_docx and "Energy is and the golden ratio is ." in plain_docx
    assert build_extractor(ExtractorSettings()).math is False


def test_native_pages_without_math_are_get_text_exactly() -> None:
    content = (DOCS / "handbook.pdf").read_bytes()
    assert pdf_page_math_texts(content) == pdf_page_texts(content)


def test_the_setting_is_validated_and_selects_every_provider() -> None:
    with pytest.raises(ValueError, match="math"):
        ExtractorSettings(math="mathml")
    for provider in ("auto", "native", "builtin"):
        assert build_extractor(ExtractorSettings(provider=provider, math="latex")).math is True


def test_turning_math_on_re_reads_document_sources_only() -> None:
    from pheasant.sync.fingerprint import source_fingerprint

    def config(math: str) -> PheasantConfig:
        return PheasantConfig.model_validate(
            {
                "ingestion": {"extractor": {"math": math}},
                "sources": [
                    {
                        "name": "papers",
                        "type": "document_folder",
                        "path": "/tmp",
                        "include": ["**/*.pdf"],
                    },
                    {"name": "notes", "type": "markdown_folder", "path": "/tmp"},
                ],
            }
        )

    off, on = config("off"), config("latex")
    papers = [source_fingerprint(c.sources[0], c.ingestion.extractor) for c in (off, on)]
    notes = [source_fingerprint(c.sources[1], c.ingestion.extractor) for c in (off, on)]
    assert papers[0] != papers[1]
    assert notes[0] == notes[1]


# ---------------------------------------------------------------------------
# 3. The sandbox boundary
# ---------------------------------------------------------------------------


def _lexer_inputs() -> list[bytes]:
    samples: list[bytes] = []
    for path in sorted(MATH.glob("*.pdf")) + [DOCS / "handbook.pdf"]:
        content = path.read_bytes()
        samples.append(content[:40_000])
        samples.extend(iter_pdf_content_streams(content)[:12])
    rng = random.Random(1729)
    alphabet = b"()<>[]{}/%#\\ \n\r\t0123456789.+-abcdefTJIDEIobjR" + bytes(range(256))
    for _ in range(200):
        samples.append(bytes(rng.choice(alphabet) for _ in range(rng.randint(0, 300))))
    samples += [b"(unterminated", b"<abc", b"/A#2", b"BI /W 1 ID \x00\xffEI Q", b"((nested) \\) x)"]
    return samples


def test_the_guest_lexer_is_the_reference_lexer_byte_for_byte() -> None:
    pytest.importorskip("wasmtime", reason="the [wasm] extra is optional")
    from pheasant.ingestion.extractor_sandbox import lex_pdf_wasm

    for sample in _lexer_inputs():
        assert serialize_tokens(lex_pdf_wasm(sample)) == serialize_tokens(lex_pdf(sample)), sample[
            :80
        ]


def test_the_lexer_skips_inline_image_data() -> None:
    tokens = lex_pdf(b"BI /W 2 ID \x28\x29\x5bTj EI (after) Tj")
    assert (5, b"Tj") in tokens and (3, b"after") in tokens
    assert tokens.count((5, b"Tj")) == 1  # the one inside the image was data


def test_a_malformed_pdf_reads_as_it_did() -> None:
    for blob in (b"%PDF-1.4\ntruncated", b"", b"not a pdf at all"):
        assert extract_pdf_text_builtin(blob, math=True) == extract_pdf_text_builtin(blob)


# ---------------------------------------------------------------------------
# The fleet reads a long PDF's equations the way the indexer does
# ---------------------------------------------------------------------------

TOKEN_ENV = "PHEASANT_INDEX_WORKER_TOKEN"


def _long_math_pdf(pages: int = 9) -> bytes:
    import pymupdf

    source = pymupdf.open(MATH / "tex_article.pdf")
    document = pymupdf.open()
    for _ in range(pages):
        document.insert_pdf(source)
    try:
        return document.tobytes()
    finally:
        document.close()
        source.close()


def test_the_reader_version_names_the_equation_reader() -> None:
    from pheasant.ingestion.pdf_math import MATH_VERSION

    assert pdf_reader_version(math=True) == f"{pdf_reader_version()}+math{MATH_VERSION}"


def test_an_http_worker_reads_a_long_pdfs_equations(tmp_path: Path, monkeypatch: Any) -> None:
    from pheasant.api.app import create_app
    from pheasant.sync.pdf_split import RemotePdfPages
    from tests.test_pdf_split import _HttpWorker

    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "math-worker",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "sync": {"concurrency": {"remote_worker_enabled": True}},
        }
    )
    monkeypatch.setenv(TOKEN_ENV, "http-token")
    content = _long_math_pdf()
    expected = AutoExtractor(math=True).extract(content, "long.pdf")
    assert r"\sqrt[3]{x+1}" in expected
    from pheasant.sync import pdf_split

    reads: list[Any] = []
    real = pdf_split.pdf_page_texts
    monkeypatch.setattr(
        pdf_split, "pdf_page_texts", lambda *a, **k: reads.append(a[1:]) or real(*a, **k)
    )
    with _HttpWorker(create_app(config)) as url:
        provider = RemotePdfPages(
            [url], TOKEN_ENV, pages_per_task=3, timeout=30, transport="http", math=True
        )
        try:
            assert (
                AutoExtractor(pdf_pages=provider, math=True).extract(content, "long.pdf")
                == expected
            )
        finally:
            provider.close()
    assert reads == [], "a range fell back to the indexer"


def test_a_grpc_worker_without_the_equation_reader_is_read_around(monkeypatch: Any) -> None:
    grpc = pytest.importorskip("grpc", reason="the [grpc] extra is optional")
    from concurrent.futures import ThreadPoolExecutor

    from pheasant.sync.grpc_worker import PreparationWorkerServicer, load_protos
    from pheasant.sync.pdf_split import RemotePdfPages

    class OldWorker(PreparationWorkerServicer):
        """Answers as a worker that predates the math field would: plain pages."""

        def ExtractPages(self, request: Any, context: Any):  # noqa: N802
            request.math = False
            return super().ExtractPages(request, context)

    monkeypatch.setenv(TOKEN_ENV, "grpc-token")
    config = PheasantConfig()
    config.sync.concurrency.remote_worker_enabled = True
    _pb2, pb2_grpc = load_protos()
    content = _long_math_pdf()
    expected = AutoExtractor(math=True).extract(content, "long.pdf")
    for servicer, label in ((PreparationWorkerServicer(config, version="t"), "current"),
                            (OldWorker(config, version="t"), "old")):  # fmt: skip
        server = grpc.server(ThreadPoolExecutor(max_workers=4))
        pb2_grpc.add_PreparationWorkerServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        provider = RemotePdfPages(
            [f"grpc://127.0.0.1:{port}"], TOKEN_ENV, pages_per_task=3, timeout=30, math=True
        )
        try:
            got = AutoExtractor(pdf_pages=provider, math=True).extract(content, "long.pdf")
        finally:
            provider.close()
            server.stop(grace=0).wait()
        assert got == expected, label

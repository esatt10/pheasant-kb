"""``ingestion.extractor.html_chrome: strip`` -- a page without the site around it.

Measured where it started: 69% of every boomi.com page's indexed text was the
same menus and footer, so 56% of that collection's chunks were exact copies of
one another and matched nearly every query. These hold the rules to what they
are for -- remove the site, keep the writing -- and the switch to its promise:
off is byte-identical, and on re-reads exactly the web collections.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.ingestion.extractor import (
    ChromeStrippingExtractor,
    build_extractor,
    extract_html_text,
)
from pheasant.sync.fingerprint import source_fingerprint

PAGE = b"""<!doctype html><html><head><title>Fast Pipes</title></head><body>
<div class="announcement">Close message</div>
<header class="site"><a href="/">Home</a><a href="/pricing">Pricing</a></header>
<div class="mbnav d-md-none"><ul><li>Platform</li><li>Solutions</li></ul></div>
<nav aria-label="primary"><a>Platform</a><a>Resources</a></nav>
<div role="navigation"><a>Partners</a></div>
<div class="cookie-banner">We use cookies</div>
<div hidden>Hidden promo</div>
<article>
  <header><h1>Fast Pipes</h1><p>by Ada, published 2024</p></header>
  <p>Throughput doubles when the queue is drained in batches.</p>
  <aside>A note that belongs to the article.</aside>
  <footer>Filed under integration</footer>
</article>
<aside class="related">Related posts</aside>
<footer class="site">Copyright and every link again</footer>
</body></html>"""


def test_the_site_goes_and_the_writing_stays() -> None:
    text = extract_html_text(PAGE, strip_chrome=True)
    for kept in (
        "Fast Pipes",
        "by Ada, published 2024",
        "Throughput doubles",
        "A note that belongs to the article.",
        "Filed under integration",
    ):
        assert kept in text, f"content was stripped: {kept!r}"
    for gone in (
        "Pricing",
        "Platform",
        "Partners",
        "We use cookies",
        "Hidden promo",
        "Related posts",
        "Copyright",
    ):
        assert gone not in text, f"chrome survived: {gone!r}"


def test_a_menu_that_is_the_content_survives() -> None:
    page = b"""<html><body><nav>Home</nav><main>
    <div class="menu"><h2>Dinner</h2><p>Soup of the day, 9 dollars.</p></div>
    </main></body></html>"""
    text = extract_html_text(page, strip_chrome=True)
    assert "Soup of the day" in text and "Home" not in text


def test_a_page_that_is_nothing_but_navigation_is_kept_whole() -> None:
    page = b"<html><body><nav><a>One</a><a>Two</a><a>Three</a></nav><p>x</p></body></html>"
    assert extract_html_text(page, strip_chrome=True) == extract_html_text(page)


def test_off_is_byte_identical() -> None:
    assert extract_html_text(PAGE) == extract_html_text(PAGE, strip_chrome=False)
    assert "Pricing" in extract_html_text(PAGE)


def _config(tmp_path: Path, **extractor: object) -> PheasantConfig:
    return PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "chrome",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "ingestion": {"extractor": {"html_text": True, **extractor}},
            "sources": [
                {
                    "name": "site",
                    "type": "web_collection",
                    "path": "/unused",
                    "urls": ["https://example.test/a"],
                },
                {"name": "docs", "type": "document_folder", "path": str(tmp_path)},
            ],
        }
    )


def test_the_extractor_strips_only_when_asked(tmp_path: Path) -> None:
    keep = build_extractor(_config(tmp_path).ingestion.extractor)
    strip = build_extractor(_config(tmp_path, html_chrome="strip").ingestion.extractor)
    assert not isinstance(keep, ChromeStrippingExtractor)
    assert isinstance(strip, ChromeStrippingExtractor)
    # A fetched page reaches the extractor as `<path>.html`.
    assert "Pricing" not in strip.extract(PAGE, "example.test/a.txt.html")
    assert "Pricing" in keep.extract(PAGE, "example.test/a.txt.html")
    # Everything that is not HTML goes to the provider untouched.
    assert strip.extract(b"plain", "notes.txt") == keep.extract(b"plain", "notes.txt")
    # And an authored sidecar still wins.
    assert strip.extract(PAGE, "page.html", b"authored text") == "authored text"


def test_turning_it_on_re_reads_web_collections_and_nothing_else(tmp_path: Path) -> None:
    keep, strip = _config(tmp_path), _config(tmp_path, html_chrome="strip")

    def fingerprints(config: PheasantConfig) -> dict[str, str]:
        return {
            source.name: source_fingerprint(source, config.ingestion.extractor)
            for source in config.sources
        }

    before, after = fingerprints(keep), fingerprints(strip)
    assert before["site"] != after["site"], "a collection would keep its stale text"
    assert before["docs"] == after["docs"], "a folder source would re-read for nothing"
    no_html = _config(tmp_path, html_chrome="strip")
    no_html.ingestion.extractor.html_text = False
    assert (
        fingerprints(no_html)["site"] == fingerprints(_config(tmp_path, html_text=False))["site"]
    ), "strip without html_text does nothing, so it must move nothing"


def test_an_unknown_setting_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="html_chrome must be 'keep' or 'strip'"):
        _config(tmp_path, html_chrome="aggressive")

"""HTML to indexable text, and the site around a page left out of it.

``extract_html_text`` is how every extractor provider reads HTML -- a local
``.html`` file, an EPUB chapter, and a page a web collection fetched (which
reaches the extractor as ``<path>.html`` because its server said ``text/html``).
With ``ingestion.extractor.html_chrome: strip`` it also removes the site's
chrome: measured on boomi.com, 69% of each page's text was the same menus and
footer, so more than half of that collection's chunks were exact copies of one
another and matched nearly every query. The rules are per page and
deterministic -- nothing is learned from the rest of the site, so a page
extracts identically whatever else is indexed (rule 1).

Its own module because `extractor.py` holds every other format and sits at its
size threshold; `extractor` re-exports what used to live there.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pheasant.ingestion.extractor import DocumentExtractor

#: Bump when the chrome rules below change what text a page produces. Part of
#: a web collection's fingerprint while ``html_chrome: strip`` is on, so a
#: bump re-reads exactly those sources and nothing else.
HTML_CHROME_VERSION = 1

#: Elements that are site chrome wherever they appear.
_CHROME_ALWAYS = ("nav",)
#: Elements that are chrome at page level and content inside a piece of
#: writing: an ``<article>``'s ``<header>`` is its title and byline, and its
#: ``<aside>`` a note that belongs to it.
_CHROME_PAGE_LEVEL = ("header", "footer", "aside")
_CHROME_ROLES = ("navigation", "banner", "contentinfo", "complementary")
#: What makes a ``<header>``/``<footer>``/``<aside>`` (or a role) part of the
#: content rather than of the page around it.
_CONTENT_SCOPES = ("article", "main", "section")
#: A class or id that names a menu. Matched per token, and only outside
#: ``<main>``/``<article>``: a site's mobile menu is a ``div`` with a class
#: (``mbnav``, ``navbar``, ``mega-menu``) where a restaurant's menu, which is
#: its content, sits inside the page's main element.
_CHROME_TOKEN = re.compile(
    r"^(?:[a-z0-9]*nav|nav(?:bar|igation)?(?:[-_].*)?|(?:.*[-_])?(?:mega-?)?menu(?:[-_].*)?"
    r"|(?:.*[-_])?breadcrumbs?(?:[-_].*)?|cookie(?:[-_].*)?|skip[-_]?(?:link|to[-_]content))$"
)
#: Stripping that would leave less than this share of the page's text keeps
#: the page whole instead: a sitemap or a link hub is nothing *but* navigation,
#: and indexing it as empty would lose the one thing it says.
_CHROME_MIN_KEPT = 0.15


def extract_html_text(content: bytes, *, strip_chrome: bool = False) -> str:
    """Strip markup so HTML/XML indexes its prose, not its tags.

    ``bs4`` is a core dependency, but a regex strip stands in when it is
    missing, so a partial install degrades to something useful rather than
    indexing raw markup.

    ``strip_chrome`` (``ingestion.extractor.html_chrome: strip``) also removes
    the site around the page: menus, page-level headers and footers, cookie
    banners and hidden elements. On a marketing site that is most of the text
    -- measured at 69% of every boomi.com page, as the same menu chunks
    indexed once per page -- and it matches nearly every query. The rules are
    per page and deterministic (`_strip_chrome`); nothing is learned from the
    rest of the site, so a page extracts identically whatever else is indexed.
    """
    raw = content.decode("utf-8", errors="ignore")
    try:
        from bs4 import BeautifulSoup
    except ModuleNotFoundError:
        no_script = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.I)
        no_style = re.sub(r"<style[\s\S]*?</style>", " ", no_script, flags=re.I)
        return _tidy_text(re.sub(r"<[^>]+>", " ", no_style))
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    if not strip_chrome:
        return _tidy_text(soup.get_text("\n", strip=True))
    whole = _tidy_text(soup.get_text("\n", strip=True))
    _strip_chrome(soup)
    stripped = _tidy_text(soup.get_text("\n", strip=True))
    if len(stripped) < _CHROME_MIN_KEPT * len(whole):
        return whole
    return stripped


def _strip_chrome(soup: Any) -> None:
    """Remove the site around a page's content, in place. See the constants above."""

    def in_content(tag: Any) -> bool:
        return tag.find_parent(_CONTENT_SCOPES) is not None

    def named_like_chrome(tag: Any) -> bool:
        tokens = [str(token).lower() for token in tag.get("class") or []]
        if tag.get("id"):
            tokens.append(str(tag.get("id")).lower())
        return any(_CHROME_TOKEN.match(token) for token in tokens)

    doomed: list[Any] = []
    for tag in soup.find_all(True):
        attrs = getattr(tag, "attrs", None)
        if attrs is None:
            continue
        name = tag.name
        hidden = "hidden" in attrs or str(attrs.get("aria-hidden", "")).lower() == "true"
        role = str(attrs.get("role", "")).lower()
        if name in ("html", "body", "main", "article"):
            continue
        if (
            hidden
            or name in _CHROME_ALWAYS
            or role == "navigation"
            or ((name in _CHROME_PAGE_LEVEL or role in _CHROME_ROLES) and not in_content(tag))
            or (
                named_like_chrome(tag)
                and tag.find_parent(("main", "article")) is None
                and tag.find(("main", "article")) is None
            )
        ):
            doomed.append(tag)
    for tag in doomed:
        if not tag.decomposed:
            tag.decompose()


class ChromeStrippingExtractor:
    """Any provider, with HTML read by ``extract_html_text(strip_chrome=True)``.

    One wrapper rather than a parameter threaded through every provider:
    every provider already reads HTML with the same function, so the setting
    has one place to act. A sidecar still wins, as it does everywhere.
    """

    def __init__(self, inner: DocumentExtractor) -> None:
        self.inner = inner
        self.model = inner.model
        self._html_calls = 0

    @property
    def calls(self) -> int:
        return self.inner.calls + self._html_calls

    def extract(self, content: bytes, relative_path: str, sidecar: bytes | None = None) -> str:
        from pheasant.ingestion._modal import sidecar_text
        from pheasant.ingestion.extractor import _format_for

        if _format_for(relative_path) != "html" or sidecar_text(sidecar) is not None:
            return self.inner.extract(content, relative_path, sidecar)  # type: ignore[call-arg]
        self._html_calls += 1
        return extract_html_text(content, strip_chrome=True)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def html_chrome_enabled(settings: Any) -> bool:
    """Whether ``ingestion.extractor.html_chrome`` strips site chrome from HTML."""

    return bool(getattr(settings, "html_text", False)) and (
        str(getattr(settings, "html_chrome", "keep") or "keep").strip().lower() == "strip"
    )


def _tidy_text(text: str) -> str:
    """The extractor's own tidy, so HTML chunks exactly as every format does."""

    from pheasant.ingestion.extractor import _tidy

    return _tidy(text)

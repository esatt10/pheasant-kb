"""Bounded public reads and structured source adapters."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import http.client
import json
import re
import socket
import ssl
import time
import zlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from io import BytesIO
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from pheasant.security.url_policy import _addresses, _is_public
from pheasant_gtm.catalog import Kind, now_iso
from pheasant_gtm.guard import suspicious
from pheasant_gtm.runtime import parse_in_sandbox
from pheasant_gtm.technology import technical_link, website_markers

_AGENT = "PheasantResearch"
_LIMIT = 3_000_000
_REDIRECTS = {301, 302, 303, 307, 308}
_TRACKING = {"fbclid", "gclid", "mc_cid", "mc_eid"}
_ATS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.eu.lever.co": "lever_eu",
    "jobs.ashbyhq.com": "ashby",
}
_ATS_API = {
    "boards-api.greenhouse.io": "greenhouse",
    "api.lever.co": "lever",
    "api.eu.lever.co": "lever_eu",
    "api.ashbyhq.com": "ashby",
}


class FetchError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class ResponseTooLarge(FetchError):
    """This publisher needs a more selective endpoint."""


def url(value: str, base: str | None = None) -> str:
    value = value.strip()
    if not value or len(value) > 2048 or any(ord(ch) < 32 or ch == "\\" for ch in value):
        raise ValueError("invalid web URL")
    if base:
        value = urljoin(base, value)
    elif "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443 if parts.scheme == "https" else 80)
    ):
        raise ValueError("only public HTTP and HTTPS URLs on standard ports are supported")
    host = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    if "." not in host or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
        raise ValueError("expected a public DNS hostname or IPv4 address")
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = "&".join(
        segment
        for segment in parts.query.split("&")
        if segment
        and not any(
            key.lower().startswith("utm_") or key.lower() in _TRACKING
            for key, _ in parse_qsl(segment, keep_blank_values=True)
        )
    )
    return urlunsplit((parts.scheme, host, path, quote(query, safe="=&%/+,:;?@!$'()*-._~"), ""))


def host(value: str) -> str:
    return (urlsplit(value).hostname or "").removeprefix("www.")


def same_site(value: str, website: str) -> bool:
    site = host(website)
    return bool(site) and (host(value) == site or host(value).endswith("." + site))


def root(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


def _decode_content(data: bytes, encoding: str) -> bytes:
    encoding = encoding.strip().casefold()
    if encoding in {"", "identity"}:
        return data
    try:
        if encoding == "gzip":
            with gzip.GzipFile(fileobj=BytesIO(data)) as stream:
                decoded = stream.read(_LIMIT + 1)
        elif encoding == "deflate":
            stream = zlib.decompressobj()
            decoded = stream.decompress(data, _LIMIT + 1)
            if len(decoded) <= _LIMIT and (not stream.eof or stream.unused_data):
                raise FetchError("invalid deflate response")
        else:
            raise FetchError("response used unsupported content encoding")
    except (OSError, EOFError, zlib.error) as exc:
        raise FetchError("invalid compressed response") from exc
    if len(decoded) > _LIMIT:
        raise ResponseTooLarge("decoded response exceeded the byte limit")
    return decoded


def _request(target: str, deadline: float, headers: dict | None = None) -> tuple[int, dict, bytes]:
    parts = urlsplit(target)
    name = parts.hostname
    addresses = _addresses(name)
    if not addresses:
        raise FetchError("DNS resolution failed", retryable=True)
    if not all(_is_public(address) for address in addresses):
        raise FetchError("host resolved to a non-public address")
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        raise FetchError("fetch time budget exhausted", retryable=True)
    connection = (
        http.client.HTTPSConnection(name, timeout=timeout, context=ssl.create_default_context())
        if parts.scheme == "https"
        else http.client.HTTPConnection(name, timeout=timeout)
    )

    # Pin each connection to the validated DNS answers while retaining Host and TLS SNI.
    def connect(_address, timeout, source_address=None):
        last = None
        for address in addresses:
            remaining = min(timeout, deadline - time.monotonic())
            if remaining <= 0:
                break
            try:
                return socket.create_connection(
                    (str(address), connection.port), remaining, source_address
                )
            except OSError as exc:
                last = exc
        raise last or TimeoutError("connection time budget exhausted")

    connection._create_connection = connect
    path = parts.path + ("?" + parts.query if parts.query else "")
    try:
        connection.request(
            "GET",
            path,
            headers={"User-Agent": _AGENT, "Accept-Encoding": "gzip, deflate", **(headers or {})},
        )
        response = connection.getresponse()
        metadata = dict((k.lower(), v) for k, v in response.getheaders())
        if response.status != 200:
            return response.status, metadata, b""
        data = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError("fetch time budget exhausted", retryable=True)
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, _LIMIT + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > _LIMIT:
                raise ResponseTooLarge("response exceeded the byte limit")
        decoded = _decode_content(bytes(data), metadata.get("content-encoding", ""))
        return response.status, metadata, decoded
    except ssl.SSLCertVerificationError as exc:
        raise FetchError("TLS certificate verification failed") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise FetchError("network read failed", retryable=True) from exc
    finally:
        connection.close()


def _allowed(robots: str, target: str) -> bool:
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    for line in robots.splitlines():
        key, _, value = line.split("#", 1)[0].partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
        elif key in {"allow", "disallow"} and agents:
            rules.append((key, value))
    groups.append((agents, rules))
    matching = [r for a, r in groups if _AGENT.lower() in a]
    if not matching:
        matching = [r for a, r in groups if "*" in a]
    parts = urlsplit(target)
    path = parts.path + ("?" + parts.query if parts.query else "")
    best = (0, True)
    for group in matching:
        for key, pattern in group:
            if not pattern:
                continue
            end = pattern.endswith("$")
            pieces = pattern.removesuffix("$").split("*")
            # Bound wildcard matching independently of the publisher's file size.
            if len(pieces) > 12 or len(pattern) > 2048:
                if key == "disallow":
                    return False
                continue
            # Match star-separated literals in order. Earliest matches leave the
            # most room for later literals and avoid regex backtracking.
            if not path.startswith(pieces[0]):
                continue
            offset = len(pieces[0])
            middle = pieces[1:-1] if end and len(pieces) > 1 else pieces[1:]
            for piece in middle:
                found = path.find(piece, offset)
                if found < 0:
                    break
                offset = found + len(piece)
            else:
                if not end or (
                    path.endswith(pieces[-1])
                    and (
                        offset <= len(path) - len(pieces[-1])
                        if len(pieces) > 1
                        else offset == len(path)
                    )
                ):
                    best = max(best, (sum(map(len, pieces)), key == "allow"))
    return best[1]


def fetch(target: str, robots_cache: dict[str, str]) -> tuple[str, str, str]:
    deadline = time.monotonic() + 14
    for _ in range(5):
        base = root(target)
        if base not in robots_cache:
            robots_url = url("/robots.txt", base)
            for _redirect in range(3):
                code, headers, body = _request(robots_url, deadline)
                if code not in _REDIRECTS or not headers.get("location"):
                    break
                destination = url(headers["location"], robots_url)
                if host(destination) != host(base):
                    break
                robots_url = destination
            if code == 200:
                robots_cache[base] = body[:192_000].decode("utf-8-sig", errors="replace")
            elif code in {404, 410}:
                robots_cache[base] = ""
            elif code in {401, 403}:
                robots_cache[base] = "User-agent: *\nDisallow: /"
            else:
                raise _http_error(code, headers, message="robots.txt unavailable")
        if not _allowed(robots_cache[base], target):
            raise FetchError("robots.txt disallows this page")
        code, headers, body = _request(target, deadline)
        if code in _REDIRECTS and headers.get("location"):
            target = url(headers["location"], target)
            continue
        if code != 200:
            raise _http_error(code, headers)
        content = headers.get("content-type", "").lower()
        if content and not any(
            x in content for x in ("html", "xml", "json", "text/plain", "rss", "atom")
        ):
            raise FetchError("unsupported content type")
        charset = re.search(r"charset\s*=\s*([a-z0-9_-]+)", content)
        encoding = charset[1] if charset else "utf-8-sig"
        try:
            decoded = body.decode(encoding, errors="replace")
        except LookupError:
            decoded = body.decode("utf-8-sig", errors="replace")
        if not plausible_prose(decoded[:16384]):
            raise FetchError("response contains binary data instead of page text")
        return target, content, decoded
    raise FetchError("redirect limit exceeded")


def _http_error(code: int, headers: dict, *, message: str | None = None) -> FetchError:
    retry_after = None
    if value := headers.get("retry-after"):
        with contextlib.suppress(ValueError, TypeError, OverflowError, IndexError):
            retry_after = (
                float(int(value))
                if value.isascii() and value.isdecimal()
                else (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
            )
    retryable = code in {408, 425, 429} or code >= 500
    if code == 429 and retry_after is None:
        retry_after = 10.0
    return FetchError(message or f"HTTP {code}", retryable=retryable, retry_after=retry_after)


def clean(text: object, limit: int = 12000) -> str:
    return " ".join(str(text or "").split()).replace("\x00", "")[:limit]


def plausible_prose(text: str) -> bool:
    corrupt = sum(ch == "\ufffd" or (ord(ch) < 32 and ch not in "\t\r\n\f") for ch in text)
    return corrupt <= max(3, len(text) // 100)


def date_value(value: object) -> str | None:
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return date.fromisoformat(value[:10]).isoformat()
        with contextlib.suppress(ValueError, TypeError, OverflowError):
            return parsedate_to_datetime(value).date().isoformat()
    return None


@dataclass
class Document:
    url: str
    title: str
    kind: Kind
    text: str
    published: str | None = None
    fetched_at: str = field(default_factory=now_iso)
    records: list[dict] = field(default_factory=list)
    links: list[list[str]] = field(default_factory=list)
    attributed: bool = False
    origin: str = ""
    expires: str | None = None
    active_board: bool = False
    content_sha256: str = ""
    quarantined: bool = False
    embedded_jobs: list[dict] = field(default_factory=list)
    data_links: list[str] = field(default_factory=list)
    acquisition: str = "page"
    acquired_from: str | None = None
    script_shell: bool = False
    web_technologies: list[str] = field(default_factory=list)

    def dump(self) -> dict:
        return asdict(self)

    @classmethod
    def from_parser(cls, packet: dict, *, target: str | None = None) -> Document:
        doc = cls(**packet)
        if doc.kind not in {"official", "jobs", "news", "filing", "external"}:
            raise ValueError("unexpected source kind from parser")
        if doc.url != url(doc.url) or (target is not None and doc.url != target):
            raise ValueError("unexpected source URL from parser")
        doc.attributed, doc.origin, doc.fetched_at = False, "", now_iso()
        return doc

    def screened(self) -> Document:
        values = (
            self.title,
            self.text,
            json.dumps(self.records, ensure_ascii=False),
            json.dumps(self.links, ensure_ascii=False),
            json.dumps(self.embedded_jobs, ensure_ascii=False),
            json.dumps(self.data_links, ensure_ascii=False),
        )
        if any(suspicious(value) for value in values):
            return Document(
                self.url,
                "",
                self.kind,
                "",
                content_sha256=self.content_sha256,
                quarantined=True,
            )
        return self


def _json_nodes(payload: object, level: int = 0):
    if level > 10:
        return
    if isinstance(payload, list):
        for entry in payload[:150]:
            yield from _json_nodes(entry, level + 1)
    elif isinstance(payload, dict):
        if "@type" in payload:
            yield payload
        for entry in ("@graph", "mainEntity"):
            if entry in payload:
                yield from _json_nodes(payload[entry], level + 1)


def node_types(node: dict) -> set[str]:
    value = node.get("@type")
    return {
        str(item).rsplit("/", 1)[-1] for item in (value if isinstance(value, list) else [value])
    }


def _microdata_job(soup: BeautifulSoup) -> dict | None:
    scopes = soup.find_all(
        attrs={
            "itemscope": True,
            "itemtype": re.compile(r"(?:^|\s)https?://schema\.org/JobPosting(?:\s|$)"),
        },
        limit=2,
    )
    if len(scopes) != 1:
        return None
    scope = scopes[0]
    properties = {}
    for tag in scope.find_all(attrs={"itemprop": True}, limit=80):
        if tag.find_parent(attrs={"itemscope": True}) is not scope:
            continue
        for key in tag["itemprop"].split():
            if key in {"title", "description"}:
                value = clean(tag.get_text(" ", strip=True), 1_300)
            elif key in {"datePosted", "validThrough"}:
                value = date_value(tag.get("datetime") or tag.get("content") or tag.get_text())
            else:
                continue
            if value:
                properties.setdefault(key, value)
    description = properties.pop("description", None)
    if not properties.get("title") or not description:
        return None
    return {"@type": "JobPosting", **properties}


def _link(candidates: dict[str, str], value: object, label: object, base: str) -> None:
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            target = url(value, base)
            if target not in candidates:
                candidates[target] = clean(label, 180)


def topic(target: str, label: str = "") -> str | None:
    path = urlsplit(target).path.lower() + " " + label.lower()
    for kind, words in (
        (
            "filing",
            (
                "investor",
                "annual-report",
                "annual report",
                "10-k",
                "financials",
                "regulatory filings",
            ),
        ),
        ("jobs", ("career", "job", "hiring", "vacanc", "open positions")),
        ("news", ("news", "press", "blog", "insight", "announcement")),
        (
            "official",
            (
                "about",
                "company",
                "contact",
                "support",
                "solution",
                "product",
                "customer",
                "service",
                "technology",
            ),
        ),
    ):
        if any(re.search(r"\b" + word, path) for word in words):
            return kind
    if technical_link(urlsplit(target).path, label):
        return "official"
    return None


def _job_records(payload: object, target: str, limit: int) -> list[dict]:
    """Walk bounded inert application data and project only job-shaped records."""
    pending = [(payload, False, 0)]
    rows = {}
    for _ in range(6000):
        if not pending or len(rows) >= limit:
            break
        node, job_list, depth = pending.pop()
        if depth > 16:
            continue
        if isinstance(node, list):
            pending.extend((value, job_list, depth + 1) for value in reversed(node[:100]))
        elif isinstance(node, dict):
            typed = node.get("@type")
            if typed == "JobPosting" or job_list:
                title, address = node.get("title"), node.get("url") or node.get("absolute_url")
                description = node.get("description") or node.get("content")
                if all(
                    isinstance(value, str) and value.strip()
                    for value in (title, address, description)
                ):
                    with contextlib.suppress(ValueError):
                        address = url(address, target)
                        if same_site(address, target):
                            snippet = BeautifulSoup(description[:9000], "html.parser")
                            for tag in snippet.find_all(_hidden):
                                if tag.attrs is not None:
                                    tag.decompose()
                            rows.setdefault(
                                address,
                                {
                                    "url": address,
                                    "title": clean(title, 220),
                                    "text": clean(snippet.get_text(" "), 2600),
                                    "published": date_value(node.get("datePosted")),
                                    "expires": date_value(node.get("validThrough")),
                                },
                            )
            pending.extend(
                (
                    value,
                    key in {"jobs", "jobPostings", "postings"}
                    or job_list
                    and key in {"data", "items", "nodes", "results"},
                    depth + 1,
                )
                for key, value in reversed(list(node.items())[:96])
                if isinstance(value, (list, dict))
            )
    return list(rows.values())


def parse(
    target: str, content_type: str, body: str, kind: Kind, *, embedded: bool = True
) -> Document:
    digest = hashlib.sha256(body.encode()).hexdigest()
    if "json" in content_type:
        packet = json.loads(body) if embedded else None
        jobs = _job_records(packet, target, 30)
        links: dict[str, str] = {}
        if isinstance(packet, dict):
            for container in (packet, packet.get("links"), packet.get("pagination")):
                if isinstance(container, dict):
                    _link(links, container.get("next"), "", target)
        return Document(
            target,
            "Structured data",
            kind,
            "",
            content_sha256=digest,
            embedded_jobs=jobs,
            data_links=[link for link in links if same_site(link, target)],
        ).screened()
    if (
        "xml" in content_type
        or "rss" in content_type
        or "atom" in content_type
        or body.lstrip().startswith(("<?xml", "<urlset", "<sitemapindex", "<rss", "<feed"))
    ):
        return _feed(target, body, kind).screened()
    soup = BeautifulSoup(body, "html.parser")
    has_script = soup.find("script", src=True) is not None
    for element in soup.find_all(_hidden):
        if element.attrs is not None:
            element.decompose()
    records: list[dict] = []
    embedded_jobs: list[dict] = []
    record_bytes = 0
    acquisition = "page"
    links: dict[str, str] = {}
    for script in soup.find_all("script", type="application/ld+json", limit=24):
        try:
            packet = json.loads(script.string or script.get_text())
            for node in _json_nodes(packet):
                size = len(json.dumps(node))
                if size <= 12000 and size + record_bytes <= 40000:
                    records.append(node)
                    record_bytes += size
                if len(records) >= 40:
                    break
        except (ValueError, RecursionError):
            continue
        if len(records) >= 40:
            break
    if len(records) < 40 and not any("JobPosting" in node_types(node) for node in records):
        posting = _microdata_job(soup)
        if posting and record_bytes + len(json.dumps(posting)) <= 40000:
            records.append(posting)
            acquisition = "html_microdata"
    if embedded:
        for script in soup.find_all("script", type="application/json", limit=8):
            data = script.string or ""
            if not data or len(data) > 1_500_000 or len(embedded_jobs) >= 30:
                continue
            with contextlib.suppress(ValueError, RecursionError):
                embedded_jobs.extend(
                    _job_records(json.loads(data), target, 30 - len(embedded_jobs))
                )
    published = expires = None
    for node in records:
        types = node_types(node)
        if "JobPosting" in types:
            kind = "jobs"
            published = date_value(node.get("datePosted"))
            expires = date_value(node.get("validThrough"))
            break
        if types.intersection({"Article", "NewsArticle", "BlogPosting", "TechArticle"}):
            kind = "filing" if kind == "filing" else "news"
            published = published or date_value(node.get("datePublished"))
    meta = {
        tag.get("property") or tag.get("name"): clean(tag.get("content"), 1500)
        for tag in soup.find_all("meta", limit=100)
    }
    published = published or date_value(meta.get("article:published_time"))
    marked_date = soup.find("time", itemprop="datePublished")
    if marked_date and kind == "news":
        published = published or date_value(marked_date.get("datetime"))
    data_links: dict[str, str] = {}
    markers = website_markers(
        [
            value
            for tag in soup.find_all(["script", "link", "iframe", "div", "meta"], limit=750)
            for value in (
                tag.get("src"),
                tag.get("href"),
                tag.get("id"),
                tag.get("content") if tag.get("name") == "generator" else None,
            )
            if isinstance(value, str)
        ]
    )
    for tag in soup.find_all(["a", "iframe", "link"], limit=1000):
        label = tag.get_text(" ", strip=True)
        if (
            embedded
            and (
                (tag.name == "iframe" and kind == "jobs")
                or (
                    tag.name == "link"
                    and "alternate" in tag.get("rel", [])
                    and tag.get("type") == "application/json"
                )
            )
            and len(data_links) < 8
        ):
            _link(data_links, tag.get("href") or tag.get("src"), "", target)
        if tag.name == "link" and tag.get("type") not in {
            "application/rss+xml",
            "application/atom+xml",
        }:
            continue
        if tag.name == "link":
            label = "news feed"
        _link(links, tag.get("href") or tag.get("src"), label, target)
    title = clean(soup.title.get_text(" ", strip=True) if soup.title else meta.get("og:title"), 220)
    for tag in soup.find_all(
        ["script", "style", "template", "noscript", "nav", "footer", "header"]
    ):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    paragraphs = [
        clean(tag.get_text(" ", strip=True), 1300)
        for tag in main.find_all(["p", "h1", "h2", "h3", "li", "tr"], limit=220)
        if tag.find(["p", "li", "tr"]) is None
    ]
    text = "\n".join(dict.fromkeys(part for part in paragraphs if part)).strip()
    text = text or clean(main.get_text(" ", strip=True))
    summary = next((meta[k] for k in ("description", "og:description") if meta.get(k)), "")
    if summary or meta.get("og:site_name"):
        records.append(
            {
                "@type": "PageMetadata",
                "description": summary,
                "site_name": meta.get("og:site_name"),
            }
        )
    return Document(
        target,
        title,
        kind,
        text[:18000],
        published,
        records=records,
        links=[
            [k, v]
            for k, v in links.items()
            if topic(k, v)
            or board(k)
            or k == root(k)
            or host(k) in {"linkedin.com", "facebook.com", "x.com", "twitter.com"}
        ][:220],
        expires=expires,
        content_sha256=digest,
        embedded_jobs=embedded_jobs,
        acquisition=acquisition,
        data_links=[link for link in data_links if same_site(link, target)],
        script_shell=has_script and len(text) < 160 and not embedded_jobs,
        web_technologies=markers,
    ).screened()


def _hidden(tag) -> bool:
    return (
        tag.name in {"template", "noscript"}
        or "hidden" in tag.attrs
        or "inert" in tag.attrs
        or str(tag.get("aria-hidden", "")).lower() == "true"
        or bool(
            re.search(
                r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden|"
                r"(?:opacity|font-size)\s*:\s*0(?:\s*!important)?\s*(?:;|$))",
                str(tag.get("style", "")),
                re.I,
            )
        )
    )


def _feed(target: str, body: str, kind: Kind) -> Document:
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", body, re.I):
        raise FetchError("unsupported XML declaration")
    try:
        tree = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise FetchError("malformed XML feed") from exc
    links: dict[str, str] = {}
    for item in tree.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag in {"url", "sitemap", "item", "entry"}:
            values = {
                child.tag.rsplit("}", 1)[-1]: child.text or child.get("href") for child in item
            }
            _link(links, values.get("loc") or values.get("link"), values.get("title") or "", target)
        if len(links) >= 8000:
            break
    relevant = [[k, v] for k, v in links.items() if topic(k, v) or "sitemap" in k]
    return Document(target, "Site feed or sitemap", kind, "", links=relevant[:220])


def board(target: str) -> str | None:
    parts = urlsplit(target)
    provider = _ATS.get(parts.hostname)
    path = parts.path.strip("/").split("/")
    if not provider or not path or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", path[0]):
        return None
    slug = path[0]
    endpoints = {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
        "lever": f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=60",
        "lever_eu": f"https://api.eu.lever.co/v0/postings/{slug}?mode=json&limit=60",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
    }
    return endpoints[provider]


def board_documents(target: str, body: str, limit: int) -> list[Document]:
    provider = _ATS_API.get(urlsplit(target).hostname)
    if not provider:
        raise FetchError("job board endpoint redirected outside a supported provider")
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise FetchError("invalid job board response") from exc
    if provider == "greenhouse" and isinstance(payload, dict) and "absolute_url" in payload:
        return _board_entries(provider, [payload], limit)
    jobs = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(jobs, list):
        raise FetchError("invalid job board payload")
    return _board_entries(provider, jobs, limit)


def greenhouse_job_links(target: str, body: str, limit: int) -> list[str]:
    parts = urlsplit(target)
    if parts.hostname != "boards-api.greenhouse.io" or not re.fullmatch(
        r"/v1/boards/[a-zA-Z0-9_-]+/jobs", parts.path
    ):
        raise FetchError("Greenhouse summary redirected outside the board endpoint")
    try:
        jobs = json.loads(body)["jobs"]
    except (ValueError, KeyError, TypeError, RecursionError) as exc:
        raise FetchError("invalid Greenhouse job summary") from exc
    if not isinstance(jobs, list):
        raise FetchError("invalid Greenhouse job summary")
    ranked = sorted(
        (
            entry
            for entry in jobs[:1000]
            if isinstance(entry, dict)
            and type(entry.get("id")) is int
            and entry.get("internal_job_id", True) is not None
        ),
        key=lambda item: (date_value(item.get("updated_at")) or "", item["id"]),
        reverse=True,
    )
    return [f"https://boards-api.greenhouse.io{parts.path}/{job['id']}" for job in ranked[:limit]]


def _board_entries(provider: str, jobs: list, limit: int) -> list[Document]:
    docs: list[Document] = []
    for item in jobs[:250]:
        if not isinstance(item, dict) or item.get("isListed") is False:
            continue
        if provider == "greenhouse" and item.get("internal_job_id", True) is None:
            continue
        title = clean(item.get("title") or item.get("text"), 220)
        public_url = item.get("absolute_url") or item.get("hostedUrl") or item.get("jobUrl")
        if not title or not isinstance(public_url, str):
            continue
        try:
            public_url = url(public_url)
        except ValueError:
            continue
        body_html = (
            item.get("content") or item.get("descriptionHtml") or item.get("description") or ""
        )
        plain = item.get("descriptionPlain") or clean(
            BeautifulSoup(unescape(str(body_html)[:30000]), "html.parser").get_text(" ", strip=True)
        )
        extras = item.get("lists")
        if isinstance(extras, list):
            plain = (
                str(plain)
                + " "
                + " ".join(
                    BeautifulSoup(str(block.get("content", ""))[:10000], "html.parser").get_text(
                        " ", strip=True
                    )
                    for block in extras[:12]
                    if isinstance(block, dict)
                )
            )
        published = date_value(item.get("publishedAt") or item.get("first_published"))
        text = clean(plain)
        docs.append(
            Document(
                public_url,
                title,
                "jobs",
                text,
                published,
                active_board=True,
                expires=date_value(item.get("application_deadline")),
                content_sha256=hashlib.sha256((title + "\n" + text).encode()).hexdigest(),
            ).screened()
        )
    return sorted(docs, key=lambda doc: (doc.published or "", doc.url), reverse=True)[:limit]


def search(query: str, key: str, *, freshness: str | None = None) -> list[str]:
    params = {"q": query, "count": "8", "safesearch": "strict"}
    if freshness:
        params["freshness"] = freshness
    target = "https://api.search.brave.com/res/v1/web/search?" + urlencode(params)
    status, _, data = _request(
        target, time.monotonic() + 12, {"X-Subscription-Token": key, "Accept": "application/json"}
    )
    if status != 200:
        raise FetchError(f"web search HTTP {status}", retryable=status == 429 or status >= 500)
    return parse_in_sandbox("search", body=data.decode("utf-8", errors="replace"))


def search_links(body: str) -> list[str]:
    try:
        results = json.loads(body).get("web", {}).get("results", [])
        return [
            item["url"]
            for item in results
            if isinstance(item, dict)
            and isinstance(item.get("url"), str)
            and len(item["url"]) <= 2048
            and not suspicious(item["url"])
        ][:8]
    except (ValueError, TypeError, AttributeError, RecursionError) as exc:
        raise FetchError("web search returned no usable results") from exc

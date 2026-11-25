"""Map admitted GTM crawl documents onto the host's ordinary web artifacts."""

from __future__ import annotations

import re
from hashlib import sha256
from urllib.parse import urlsplit

from pheasant.ingestion.media import PAGE_SUFFIX, MediaStore
from pheasant.sync.connectors import ConnectorItem, ConnectorPayload
from pheasant_gtm.sources import Document, plausible_prose

PLAIN = "text/plain"
HTML = "text/html"


def reference(url: str) -> str:
    return sha256(url.encode()).hexdigest()[:24]


def capture(store: MediaStore | None, captures: dict, url: str, mime: str, body: str) -> None:
    if store is None or mime.partition(";")[0].strip().lower() not in {
        HTML,
        "application/xhtml+xml",
    }:
        return
    content = body.encode()
    digest = sha256(content).hexdigest()
    if store.put(digest, PAGE_SUFFIX, content) is not None:
        captures[url] = {"sha256": digest, "size_bytes": len(content)}


def admitted(documents: list[Document], captures: dict) -> dict[str, dict]:
    pages = {}
    for doc in documents:
        if not doc.attributed or doc.quarantined or not doc.text.strip():
            continue
        text = "\n\n".join(part for part in (doc.title, doc.text) if part)
        content = text.encode()
        original = captures.get(doc.url)
        html = original is not None and original["sha256"] == doc.content_sha256
        pages[reference(doc.url)] = {
            "url": doc.url,
            "mime_type": HTML if html else PLAIN,
            "sha256": original["sha256"] if html else sha256(content).hexdigest(),
            "size_bytes": original["size_bytes"] if html else len(content),
            "text": text,
        }
    return pages


def research_documents(profile: dict, pages: dict) -> list[Document]:
    documents = []
    for row in profile["source_outcomes"]:
        if not row["attributed"] or row["quarantined"]:
            continue
        page = pages.get(reference(row["url"]))
        if not page or not plausible_prose(page["text"][:16384]):
            continue
        title = row["title"] or ""
        documents.append(
            Document(
                row["url"],
                title,
                row["kind"],
                page["text"].removeprefix(f"{title}\n\n") if title else page["text"],
                published=row["published"],
                fetched_at=row["accessed_at"],
                active_board=row.get("active_board", False),
                expires=row.get("expires"),
                attributed=True,
            )
        )
    return documents


def items(pages: dict, source: str, company: str | None = None) -> list[ConnectorItem]:
    directory = f"{company}/pages" if company else "pages"
    result = []
    for key, page in sorted(pages.items()):
        url = page["url"]
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", urlsplit(url).path.strip("/")).strip("-")
        slug = slug[:60].lower() or "home"
        result.append(
            ConnectorItem(
                identity=f"gtm:{source}:{directory}:{key}",
                relative_path=f"{directory}/{slug}-{key}.txt",
                uri=url,
                mime_type=page["mime_type"],
                sha256=page["sha256"],
                size_bytes=page["size_bytes"],
                metadata={"company_id": company, "page_reference": key},
            )
        )
    return result


def inputs(items: list[ConnectorItem], evidence: list[dict]) -> dict[str, list[str]]:
    field_keys = {ref["id"]: ref["field_keys"] for ref in evidence}
    return {
        item.relative_path: sorted(field_keys.get(item.metadata["page_reference"], ()))
        for item in items
    }


def payload(item: ConnectorItem, page: dict, store: MediaStore | None) -> ConnectorPayload:
    if not page or item.sha256 != page["sha256"]:
        raise ValueError("GTM page checkpoint no longer matches the listed revision")
    text = page["text"]
    html = item.mime_type == HTML
    body = store.get(page["sha256"], PAGE_SUFFIX) if html and store is not None else None
    if html and body is None:
        raise FileNotFoundError("GTM HTML snapshot is missing. Refresh the company to fetch it.")
    return ConnectorPayload(
        item,
        body if html else text.encode(),
        mime_type=item.mime_type,
        sha256=item.sha256,
        normalized_text=text,
    )

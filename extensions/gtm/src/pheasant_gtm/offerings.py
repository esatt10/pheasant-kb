"""Company-owned products and services, with bounded evidence for each claim."""

from __future__ import annotations

import re
from collections import defaultdict
from itertools import pairwise
from urllib.parse import SplitResult, unquote, urlsplit

from pheasant_gtm.catalog import words
from pheasant_gtm.evidence import name_key, subject
from pheasant_gtm.field_report import _PRODUCTS
from pheasant_gtm.guard import suspicious
from pheasant_gtm.sources import Document, host, node_types, same_site

_ROOTS = {
    "app": "product",
    "apps": "product",
    "product": "product",
    "products": "product",
    "services": "service",
    "service": "service",
    "solutions": "service",
    "logistics-solutions": "service",
    "offerings": "service",
    "support": "support",
    "subscriptions": "support",
    "pricing": "support",
    "documentation": "reference",
    "docs": "reference",
}
_IGNORE = {"blog", "news", "careers", "jobs", "community", "press", "investors", "customer-stories"}
_MARKETPLACE = {
    "marketplace",
    "appstore",
    "app-store",
    "addons",
    "add-ons",
    "extensions",
    "plugins",
}
_LISTING_PAGES = {
    "search",
    "compare",
    "filter",
    "tag",
    "category",
    "faq",
    "terms",
    "sales-conditions",
    "vendor-guidelines",
}
_GENERIC = {
    "about",
    "all products",
    "documentation",
    "home",
    "overview",
    "platform",
    "product",
    "products",
    "products and documentation",
    "services",
    "solutions",
    "pricing",
    "contact",
    "learn more",
    "read more",
    "compare",
    "get started",
    "customer stories",
    "resources",
    "technical support",
}
_NAME = re.compile(r"[\w][\w .,&()+/’'®™–-]{1,79}\Z")
_BRAND_SEP = re.compile(r"\s*(?:\||•)\s*|\s+[–—-]\s+")
_CALL_TO_ACTION = re.compile(
    r"\b(?:ignore|disregard|override|reveal|execute|invoke|upload|install|click|subscribe|sign in|"
    r"sign up|buy now|download now|buy|tell|say|output|must|instructions?|prompts?)\b",
    re.I,
)
_OFFER = re.compile(
    r"\b(?:we (?:offer|provide|deliver|develop|build|operate|help)|our (?:app|platform|software|"
    r"product|service)|offers?|provides?|powered by|platform|software|"
    r"apps?|applications?|services?)\b",
    re.I,
)
_PARTNER = re.compile(r"\b(?:we help|we provide|we deliver|our (?:services|expertise))\b", re.I)
_NEGATED = re.compile(
    r"\b(?:no longer|does not|do not|not available|not supported|without support)\b", re.I
)
_OWNED_SERVICE = re.compile(
    r"\b(?:our|we (?:offer|provide)|(?:offers|provides))\b[^?!;.]{0,75}\b"
    r"(air freight|ocean freight|customs (?:brokerage|clearance)|freight forwarding)"
    r"(?:\s+services?)?\b",
    re.I,
)
_SERVICE_LABELS = {
    "air freight": "Air freight services",
    "ocean transport": "Ocean transport services",
    "inland transport": "Inland transport services",
    "less than container load lcl": "Less-than-container-load shipping",
    "warehousing and distribution": "Warehousing and distribution",
    "customs services": "Customs services",
}

# Controlled facets are public. The source wording is stored only in analyst evidence.
_PATTERNS = {
    "capabilities": {
        "warehouse management": r"\bwarehouse management\b",
        "financial reconciliation": r"\b(?:bank reconciliation|reconcile bank transactions)\b",
        "appointment scheduling": (
            r"\b(?:self-service booking|book meetings|schedule appointments)\b"
        ),
        "customer relationship management": r"\b(?:customer relationship management|CRM)\b",
        "enterprise resource planning": r"\b(?:cloud ERP|enterprise resource planning)\b",
        "inventory tracking": (
            r"\b(?:inventory tracking|real.time (?:inventory|visibility)|stock levels)\b"
        ),
        "automated replenishment": (
            r"\b(?:replenishment strategies|reordering rules|automated replenishment)\b"
        ),
        "barcode scanning": (
            r"\b(?:barcode scanning|barcode scanner|scan barcodes|supports barcode)\b"
        ),
        "application deployment": r"\b(?:deploy|deliver|run) (?:cloud.native )?applications\b",
        "workflow automation": (
            r"\b(?:automate (?:workflows|business processes)|workflow automation)\b"
        ),
        "data governance": r"\b(?:data governance|govern (?:their |your )?data)\b",
        "AI implementation": (
            r"\b(?:design agentic systems|implement AI|build AI (?:solutions|applications))\b"
        ),
        "air freight": r"\bair freight\b",
        "ocean freight": r"\b(?:ocean freight|ocean transport)\b",
        "customs services": r"\b(?:customs (?:brokerage|services|clearance))\b",
        "warehousing and distribution": r"\bwarehousing and distribution\b",
    },
    "buyers": {
        "small and midsized businesses": (
            r"\b(?:for|helps?|serves?|built for|designed for|built to help) small and midsized "
            r"(?:businesses|companies)\b"
        ),
        "enterprise teams": r"\b(?:for|helps?|serves?|built for|designed for)"
        r" enterprise (?:teams|customers|businesses)\b",
        "retailers": r"\b(?:for|helps?|serves?|built for|designed for) retailers\b",
        "manufacturers": r"\b(?:for|helps?|serves?|built for|designed for) manufacturers\b",
    },
    "delivery": {
        "cloud hosted": r"\b(?:cloud.hosted|hosted in (?:our|the) cloud|cloud SaaS)\b",
        "self-hosted or on-premises": r"\b(?:self.hosted|on.premise(?:s)?)\b",
        "subscription": (
            r"\b(?:available (?:as|by) subscription|requires a subscription|subscription plans?)\b"
        ),
    },
    "support": {
        "technical support": r"\btechnical support\b",
        "support portal": r"\b(?:customer portal|support portal)\b",
        "24/7 support": r"\b(?:24/7|24x7) (?:technical |customer )?support\b",
        "professional services": r"\bprofessional services\b",
    },
}
_COMPILED = {
    field: {term: re.compile(pattern, re.I) for term, pattern in terms.items()}
    for field, terms in _PATTERNS.items()
}


def _marketplace(parts: SplitResult, segments: list[str]) -> bool:
    subdomain = (parts.hostname or "").split(".")[0]
    return (
        subdomain in _MARKETPLACE
        or bool(set(segments) & _MARKETPLACE)
        or subdomain in {"apps", "store"}
        and bool({"app", "apps"} & set(segments))
        or any(pair in {("app", "modules"), ("apps", "modules")} for pair in pairwise(segments))
    )


def route(address: str) -> tuple[str, str, bool] | None:
    """Classify an offering route and distinguish a catalog from a detail page."""
    parts = urlsplit(address)
    segments = [unquote(item).casefold() for item in parts.path.split("/") if item]
    if parts.query or set(segments) & _IGNORE or _marketplace(parts, segments):
        return None
    for index, segment in enumerate(segments):
        if kind := _ROOTS.get(segment):
            tail = segments[index + 1 :]
            if len(tail) > 5 or set(tail) & _LISTING_PAGES:
                return None
            return kind, tail[-1] if tail else "", any("partner" in item for item in tail)
    if not segments and parts.hostname and parts.hostname.startswith(("access.", "support.")):
        return "support", "", False
    return None


def priority(address: str) -> int:
    entry = route(address)
    if entry is None:
        return 0
    kind, slug, _ = entry
    return {"product": 104, "service": 102, "support": 100, "reference": 96}[kind] + (
        4 if not slug else 0
    )


def _safe_name(value: str) -> str:
    value = " ".join(value.split()).strip(" .")
    if (
        not _NAME.fullmatch(value)
        or len(value.split()) > 9
        or _CALL_TO_ACTION.search(value)
        or suspicious(value)
        or words(value) in _GENERIC
    ):
        return ""
    return value


def _phrases(doc: Document) -> list[str]:
    return [
        line
        for line in dict.fromkeys([doc.title, *doc.text.splitlines()])
        if not _NEGATED.search(line)
    ][:221]


def _mentions(line: str, name: str) -> bool:
    return f" {words(name)} " in f" {words(line)} "


def _title_name(doc: Document, vendor: str, slug: str) -> str:
    for part in _BRAND_SEP.split(doc.title):
        name = _safe_name(part)
        if not name or name_key(name) == name_key(vendor):
            continue
        slug_words = set(words(slug).split()) - {"html", "app", "product"}
        if slug_words & set(words(name).split()):
            return name
    return ""


def _candidate_names(doc: Document, vendor: str, lines: list[str]):
    """Yield a name only with evidence of an offering and the company's role."""
    parts = urlsplit(doc.url)
    segments = [unquote(item).casefold() for item in parts.path.split("/") if item]
    marketplace = _marketplace(parts, segments)
    for node in doc.records:
        types = node_types(node)
        if not types.intersection({"Product", "Service"}):
            continue
        provider = node.get("provider") or node.get("manufacturer")
        if not provider and not marketplace:
            provider = node.get("brand")
        provider = provider.get("name") if isinstance(provider, dict) else provider
        if isinstance(provider, str) and name_key(provider) == name_key(vendor):
            raw = node.get("name")
            if isinstance(raw, str) and (name := _safe_name(raw)):
                yield name, "service" if "Service" in types else "product", "provider", name

    if marketplace:
        return
    entry = route(doc.url)
    kind, slug, partner = entry if entry else ("", "", False)
    if not parts.path.strip("/") or kind == "service" and not partner:
        for line in lines:
            if match := _OWNED_SERVICE.search(line):
                yield match[1].title() + " services", "service", "provider", line
                break
    for name, pattern in _PRODUCTS.get(host(doc.url), {}).items():
        for line in lines:
            if subject(line, vendor) and re.search(pattern, line, re.I):
                yield name, "product", "provider", line
                break
    if not entry:
        return
    if kind == "support":
        for line in lines:
            if re.search(
                r"\b(?:technical support|customer portal|support portal|24[/'x]7 support)\b",
                line,
                re.I,
            ):
                yield "Customer support", "support", "provider", line
                return
        return
    if partner:
        token = r"\W+".join(re.escape(term) for term in words(slug).split())
        match = re.search(rf"\b{token}\b", doc.title, re.I)
        partner_name = _safe_name(match[0]) if token and match else ""
        for line in lines:
            if (
                _PARTNER.search(line)
                and f" {words(slug)} " in f" {words(line)} "
                and partner_name
                and not _mentions(partner_name, vendor)
            ):
                yield (
                    f"{partner_name} implementation services",
                    "service",
                    "implementation_partner",
                    line,
                )
                return
        return
    if kind == "reference":
        return
    if slug and (name := _title_name(doc, vendor, slug)):
        secure_product = kind == "product" and len(words(slug).split()) <= 8
        for line in doc.text.splitlines():
            if len(line) < 20 or _NEGATED.search(line) or not _OFFER.search(line):
                continue
            if _mentions(line, name) or (secure_product and _mentions(line, vendor)):
                if re.search(r"/(?:app|apps)/", parts.path) and _safe_name(slug.title()):
                    app = slug.removesuffix("-features").removesuffix("-overview")
                    name = app.replace("-", " ").title()
                owned_name = (
                    f"{vendor} {name}"
                    if kind == "product" and not _mentions(name, vendor)
                    else name
                )
                yield owned_name, kind, "provider", line
                break
    # A vendor's official product catalog explicitly labels its own offerings.
    # This also works with catalog pages archived before structured product metadata.
    if (
        not slug
        and kind == "product"
        and _mentions(doc.title, vendor)
        and len(doc.text.splitlines()) >= 3
    ):
        for line in doc.text.splitlines():
            if not line.casefold().startswith(vendor.casefold() + " "):
                continue
            label = words(line[len(vendor) :])
            if label in _GENERIC or label.startswith(("products ", "documentation ")):
                continue
            if (name := _safe_name(line)) and len(words(name).split()) >= len(
                words(vendor).split()
            ) + 1:
                yield name, "product", "provider", line
    if not slug and kind == "service" and _mentions(doc.title, vendor):
        if any(
            re.search(r"\b(?:our services|we offer|we provide)\b", line, re.I) for line in lines
        ):
            for line in lines:
                if name := _SERVICE_LABELS.get(words(line)):
                    yield name, "service", "provider", line


def inventory(
    documents: list[Document], website: str, vendor: str | None
) -> tuple[list[dict], dict]:
    if not vendor:
        return [], {}
    results = {}
    traces = defaultdict(list)
    for doc in sorted(documents, key=lambda item: item.url):
        if (
            not doc.attributed
            or doc.quarantined
            or doc.script_shell
            or doc.kind != "official"
            or not same_site(doc.url, website)
            or suspicious(doc.title + "\n" + doc.text)
        ):
            continue
        lines = _phrases(doc)
        candidates = list(dict.fromkeys(_candidate_names(doc, vendor, lines)))
        for name, kind, role, proof in candidates:
            if not _safe_name(name):
                continue
            key = f"{role}:{words(name)}"
            if key not in results and len(results) >= 80:
                continue
            row = results.setdefault(
                key,
                {
                    "name": name,
                    "kind": kind,
                    "role": role,
                    "sources": set(),
                    "facets": defaultdict(lambda: defaultdict(set)),
                },
            )
            row["sources"].add(doc.url)
            traces[key].append({"url": doc.url, "quote": proof[:380], "field": "offering"})
            # On a portfolio page, attach features only when the line names
            # this product. A single offering's detail page can use its body.
            dedicated = len(candidates) == 1 and bool(route(doc.url) and route(doc.url)[1])
            for field, patterns in _COMPILED.items():
                for term, pattern in patterns.items():
                    for line in lines:
                        if (
                            dedicated or kind == "support" or _mentions(line, name)
                        ) and pattern.search(line):
                            row["facets"][field][term].add(doc.url)
                            traces[key].append(
                                {"url": doc.url, "quote": line[:380], "field": field, "value": term}
                            )
                            break
    return [
        {
            "id": key,
            "name": row["name"],
            "kind": row["kind"],
            "role": row["role"],
            "sources": sorted(row["sources"]),
            "facets": {
                field: {term: sorted(urls) for term, urls in sorted(terms.items())}
                for field, terms in sorted(row["facets"].items())
            },
        }
        for key, row in sorted(results.items())
    ], {
        key: list({(r["url"], r["quote"], r["field"], r.get("value")): r for r in rows}.values())[
            :24
        ]
        for key, rows in sorted(traces.items())
    }

"""Cited, deterministic explanations built from screened company evidence."""

from __future__ import annotations

import re
from hashlib import sha256
from urllib.parse import urlsplit

from pheasant_gtm.catalog import words
from pheasant_gtm.evidence import subject
from pheasant_gtm.guard import suspicious
from pheasant_gtm.sources import host, plausible_prose, same_site
from pheasant_gtm.technology import TECHNOLOGY_CATEGORY

VERSION = 6

# These phrases describe what the company itself does. Technical keywords in a
# job, a customer case study or a third-party product mention cannot match.
_FRONTIER = "researches and develops frontier AI systems"
_ERP_MARKET = "serves small and midsized companies with business software"
_SAFETY = "studies how to make AI systems safer and more reliable"
_OPERATIONS = {
    _FRONTIER: (
        r"\b(?:build|develop|develops|building) frontier AI (?:systems|models)\b",
        r"\b(?:conduct|conducts|do) frontier AI research\b",
    ),
    _SAFETY: (
        r"\b(?:safe and reliable AI|reliable, interpretable, and steerable|"
        r"safety as a systematic science|interpretability and alignment research)\b",
    ),
    "provides enterprise resource planning software": (
        r"\b(?:our|offers?|provides?|we build) (?:cloud )?"
        r"(?:ERP software|enterprise resource planning software)\b|\bcloud ERP offers\b",
    ),
    _ERP_MARKET: (
        r"\b(?:business management solution|business software)\b",
        r"\b(?:built to help|for|helps?) small and midsized (?:companies|businesses)\b",
    ),
    "develops a suite of integrated applications for running a business": (
        r"\b(?:provide|offers?) a range of (?:easy to use )?business applications\b",
        r"\b(?:complete suite of tools|tightly integrated)\b",
    ),
    "develops a software delivery platform for teams and AI agents": (
        r"\bplatform where teams and their AI agents ship secure software\b",
    ),
    "provides tools for developers to integrate and use its AI models": (
        r"\b(?:our endpoints offer different ways to interact with our models|"
        r"guides on how to use our products and APIs)\b",
    ),
    "operates a platform for collaboration on models, datasets, and applications": (
        r"^the platform where the machine learning community collaborates on models, datasets, "
        r"and applications\.?$",
    ),
    "provides hybrid cloud software for Linux, AI, virtualization, and automation": (
        r"\bdelivers flexible hybrid cloud solutions\W+like Linux\W+AI, virtualization, "
        r"and automation\b",
    ),
    "provides freight forwarding and logistics services": (
        r"\b(?:offers?|provides?|our) (?:global )?freight forwarding and logistics services\b",
    ),
    "provides integrated logistics across ocean, land, and air transport": (
        r"\bby connecting ocean, land, and air, we help keep supply chains moving\b",
    ),
    "provides transportation, e-commerce, and business services": (
        r"\bprovides customers and businesses worldwide with a broad portfolio of transportation, "
        r"ecommerce, and business services\b",
    ),
    "provides enterprise AI for private and regulated deployments": (
        r"\b(?:our|we build|we deliver|we bring)\b.{0,70}\b(?:enterprise AI|AI platform)\b"
        r".{0,120}\b(?:private|sovereign|regulated)\b",
    ),
    "provides technology consulting across design, engineering, and AI": (
        r"\btechnology consultancy\b.{0,100}\bblending design, engineering and AI\b",
    ),
    "sells machinery and power systems": (
        r"\bsells (?:the majority of )?its machinery and power systems\b",
    ),
    "develops technology for industry, infrastructure, transport, and healthcare": (
        r"\btechnology company\W+focused on industry, infrastructure, transport, and healthcare\b",
    ),
    "provides technology consulting and systems integration services": (
        r"\b(?:offers?|provides?|our) technology consulting and systems integration\b",
    ),
    "works with clients on business and technology consulting projects": (
        r"\bbusiness and technology (?:company|consulting)\b",
        r"\bconsulting experience\b",
    ),
}
_PRODUCTS = {
    "anthropic.com": {
        "Claude": r"\b(?:tools like|our (?:AI )?(?:assistant|product|model)\s+|"
        r"build(?:s)?|develop(?:s)?)\s*Claude\b",
    },
    "acumatica.com": {"Acumatica Cloud ERP": r"\bAcumatica Cloud ERP (?:offers|safeguards)\b"},
    "gitlab.com": {"GitLab": r"\bthe company behind GitLab\b"},
    "odoo.com": {"Odoo": r"\bhere at Odoo, we have developed\b"},
    "redhat.com": {
        "Red Hat Enterprise Linux": r"\bRed Hat®? Enterprise Linux provides\b",
    },
}
_ABOUT = re.compile(
    r"(?:^|/)(?:about(?:[-_]us)?|who-we-are|our-company|company|corporate)(?:[./_-]|$)",
    re.I,
)
_COMPANY_NAME = re.compile(r"[\w][\w &.,'()\-]{0,100}")

_TECH_EVIDENCE = {
    "internal_use": "Sources describe company use.",
    "change_in_progress": "Sources describe implementation, migration, or expansion.",
    "hiring": "A hiring source mentions this technology.",
    "product_or_integration": "A product or integration source mentions this technology.",
    "public_website": "Detected in the public website markup.",
    "mentioned": "A source mentions this technology.",
}
_SIGNAL_EVIDENCE = {
    "capabilities_in_development": (
        "Hiring or published activity suggests this capability is developing."
    ),
    "service_need_hypotheses": "Research signals suggest a possible service need.",
}
_FIELD_EVIDENCE = {
    "identity": "Verified against the matched company website.",
    "metadata": "Reported in publisher metadata.",
    "structured": "Reported in publisher structured data.",
    "pattern": "Supported by a literal published statement.",
}


def first_party_pages(profile: dict, pages: dict, kinds=("official", "filing")):
    """Read admitted issuer pages, including a verified site's parent domain."""
    website = profile["website"]
    configured = profile.get("configured_domain")
    parent = f"https://{configured}/" if configured else website
    issuer = parent if same_site(website, parent) else website
    archived = {page["url"]: page for page in pages.values()}
    for row in profile["source_outcomes"]:
        address = row["url"]
        if (
            not row["attributed"]
            or row["quarantined"]
            or not same_site(address, issuer)
            or (
                row["kind"] not in kinds
                and not (row["kind"] == "external" and "official" in kinds and issuer != website)
            )
        ):
            continue
        page = archived.get(address)
        if page and plausible_prose(page["text"][:16384]) and not suspicious(page["text"]):
            yield row, page


def company_overview(profile: dict, pages: dict, name: str | None) -> dict:
    """Find separately sourced clauses in the company's own archived prose."""
    website = profile["website"]
    supported: dict[str, dict[int, set[str]]] = {}
    products: dict[str, set[str]] = {}
    registered = _PRODUCTS.get(host(website), {})
    for source, page in first_party_pages(profile, pages):
        address = page["url"]
        path = urlsplit(address).path
        if path.strip("/") and not _ABOUT.search(path) and source["kind"] != "filing":
            continue
        # The archive places the title on the first line. Headlines, navigation
        # labels and case study quotes alone cannot establish a company action.
        for ordinal, line in enumerate(page["text"].splitlines()[1:]):
            first_party = subject(line, name or profile.get("company")) or re.match(
                r"^our (?:mission|purpose|endpoints)\b", line, re.I
            )
            clear_lead = ordinal < 8 and line.casefold().startswith(
                ("the platform where ", "in-depth guides ")
            )
            if len(line) < 45 or not (first_party or clear_lead):
                continue
            if re.search(r"\b(?:we do not|does not|never|no longer)\b", line, re.I):
                continue
            for clause, patterns in _OPERATIONS.items():
                for position, pattern in enumerate(patterns):
                    if re.search(pattern, line, re.I):
                        supported.setdefault(clause, {}).setdefault(position, set()).add(address)
            for product, pattern in registered.items():
                if re.search(pattern, line, re.I):
                    products.setdefault(product, set()).add(address)
    label = name or profile.get("company") or ""
    clauses = []
    for i, clause in enumerate(_OPERATIONS):
        if i == 1 and products:
            names = [product for product in sorted(products) if words(product) != words(label)]
            if names:
                clauses.append(
                    (
                        "builds products such as " + _join(names),
                        set().union(*(products[p] for p in names)),
                    )
                )
        matches = supported.get(clause, {})
        if matches and len(matches) == len(_OPERATIONS[clause]):
            clauses.append((clause, set().union(*matches.values())))
    if not clauses and not products:
        return {}
    if not clauses:
        clauses.append(("offers " + _join(sorted(products)), set().union(*products.values())))
    label = label[0].upper() + label[1:] if _COMPANY_NAME.fullmatch(label) else "The company"
    return {
        "summary": f"{label} {_join([text for text, _ in clauses])}.",
        "mission": f"{label} {_SAFETY}." if _SAFETY in supported else None,
        "clauses": clauses,
        "products": products,
    }


def _join(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def offering_overview(company: str | None, inventory: list[dict]) -> dict:
    products = {item["name"]: set(item["sources"]) for item in inventory}
    clause = "offers " + _join(sorted(products))
    name = company if company and _COMPANY_NAME.fullmatch(company) else "The company"
    return {
        "summary": f"{name} {clause}.",
        "mission": None,
        "clauses": [(clause, set().union(*products.values()))],
        "products": products,
    }


def evidence_id(url: str) -> str:
    return sha256(url.encode()).hexdigest()[:24]


def item_details(key: str, entry: dict, values: list, source_urls) -> list[dict]:
    """Keep the type and strength of evidence attached to each published value."""
    details = []
    for item in entry.get("items") or [entry]:
        value = item["value"]
        if value not in values:
            continue
        grouped: dict[str, set[str]] = {}
        for citation in item["evidence"]:
            url = citation["url"]
            if url not in source_urls:
                continue
            description = (
                _TECH_EVIDENCE.get(citation.get("context"), _TECH_EVIDENCE["mentioned"])
                if key in {"technology_stack", "web_technologies"}
                else _SIGNAL_EVIDENCE.get(key, _FIELD_EVIDENCE.get(item.get("method", "")))
            )
            if fiscal := re.fullmatch(r"FY 20\d\d, [A-Z]{3}", citation.get("context") or ""):
                description = f"Reported amount for {fiscal[0]}."
            if not description:
                continue
            grouped.setdefault(description, set()).add(evidence_id(url))
        for description, ids in sorted(grouped.items()):
            details.append(
                {
                    "value": value,
                    "text": description,
                    "evidence_ids": sorted(ids),
                    **(
                        {"category": TECHNOLOGY_CATEGORY[value]}
                        if key == "technology_stack"
                        else {}
                    ),
                }
            )
    return details

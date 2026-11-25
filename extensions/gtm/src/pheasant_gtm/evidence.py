"""Evidence extraction, validation, deterministic resolution, and projection."""

from __future__ import annotations

import contextlib
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from pheasant_gtm.catalog import CRM_ONLY, FieldSpec, words
from pheasant_gtm.guard import suspicious
from pheasant_gtm.naics import titles as naics_titles
from pheasant_gtm.sources import Document, clean, date_value, host, node_types, same_site, url
from pheasant_gtm.technology import WEB_TECHNOLOGY, technologies

Candidate = dict[str, Any]
_AUTHORITY = {"filing": 5, "official": 4, "jobs": 3, "news": 2, "external": 1}
_ORG_TYPES = {
    "Organization",
    "Corporation",
    "LocalBusiness",
    "ProfessionalService",
    "Airline",
    "BankOrCreditUnion",
    "OnlineBusiness",
    "Store",
    "MedicalOrganization",
}
_THEMES = {
    "cloud modernization": ("cloud migration", "migrate to the cloud", "cloud modernization"),
    "data platform maturation": ("data platform", "data governance", "data warehouse"),
    "AI adoption": ("generative ai", "ai platform", "machine learning infrastructure"),
    "security maturation": ("security operations", "soc 2", "zero trust", "incident response"),
    "operating model scale-up": ("build out", "scale our team", "establish a new team"),
    "customer support expansion": (
        "expand customer support",
        "build a support team",
        "24/7 support",
    ),
    "systems integration": ("systems integration", "erp implementation", "crm migration"),
    "managed services": ("managed services", "outsourcing", "external implementation partner"),
    "industrial automation": (
        "industrial automation",
        "factory automation",
        "predictive maintenance",
    ),
    "supply chain digitization": (
        "supply chain visibility",
        "warehouse automation",
        "digital freight",
    ),
    "enterprise applications modernization": (
        "sap s/4hana",
        "cloud erp migration",
        "erp modernization",
    ),
}
_ACTIVITIES = {
    "AI models and developer APIs": (
        "foundation models",
        "large language models",
        "model api",
        "frontier ai",
    ),
    "AI model hosting and tooling": (
        "model hub",
        "inference endpoints",
        "machine learning platform",
    ),
    "ERP and business applications": ("enterprise resource planning", "cloud erp", "erp software"),
    "Developer and infrastructure tools": (
        "developer platform",
        "software delivery",
        "infrastructure as code",
        "devsecops platform",
        "infrastructure lifecycle management",
    ),
    "Cloud security and connectivity": (
        "zero trust",
        "network security",
        "content delivery network",
        "web application firewall",
    ),
    "Freight and logistics": (
        "freight forwarding",
        "ocean freight",
        "parcel delivery",
        "supply chain management",
    ),
    "Industrial equipment and automation": (
        "industrial automation",
        "control systems",
        "construction machinery",
        "construction and mining equipment",
        "industrial software",
    ),
    "Technology consulting and integration": (
        "technology consulting",
        "business and technology consulting",
        "systems integration",
        "digital transformation services",
    ),
    "Managed services and support": (
        "managed services",
        "professional services",
        "technical support",
    ),
}
_SERVICE = {
    "cloud modernization": "Cloud migration or modernization support",
    "data platform maturation": "Data engineering or governance support",
    "AI adoption": "AI platform implementation or evaluation support",
    "security maturation": "Security engineering or compliance advisory support",
    "customer support expansion": "Customer support enablement or operations support",
    "systems integration": "Systems integration or implementation support",
    "industrial automation": "Industrial integration or operational technology support",
    "supply chain digitization": "Logistics systems or supply chain integration support",
    "enterprise applications modernization": "ERP implementation or migration support",
}
_MONEY = re.compile(
    r"(?<!\w)(USD|CAD|AUD|EUR|GBP|CHF|JPY|INR|US\$|€|£)\s*"
    r"(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?:\s+(thousand|million|billion))?\b",
    re.I,
)
_CURRENCY = {"US$": "USD", "€": "EUR", "£": "GBP"}
_SCALE = {"thousand": 1000, "million": 10**6, "billion": 10**9}


def organizations(doc: Document) -> list[dict]:
    nodes = [node for node in doc.records if node_types(node) & _ORG_TYPES]
    return nodes


def name_key(value: str) -> str:
    return re.sub(
        r"\s+(incorporated|inc|corp|corporation|llc|ltd|limited|plc|gmbh)$", "", words(value)
    )


def site_identity(doc: Document, name: str | None) -> list[dict]:
    """Choose only organization nodes attributable to this site and target."""
    matches = []
    for node in organizations(doc):
        names = [node.get(key) for key in ("name", "legalName", "alternateName")]
        names = [entry for entry in names if isinstance(entry, str)]
        if not names or (name and not any(name_key(value) == name_key(name) for value in names)):
            continue
        site = node.get("url")
        if isinstance(site, str):
            try:
                if not same_site(url(site, doc.url), doc.url):
                    continue
            except ValueError:
                continue
        elif not name:
            continue
        matches.append(node)
    return matches


def _citation(doc: Document, quote: str) -> dict:
    return {
        "url": doc.url,
        "title": doc.title,
        "source": doc.kind,
        "origin": doc.origin or host(doc.url),
        "published": doc.published,
        "accessed_at": doc.fetched_at,
        "active_job_board": doc.active_board,
        "content_sha256": doc.content_sha256,
        "quote": clean(quote, 380),
    }


def candidate(
    key: str, value: Any, doc: Document, quote: str, method: str, context: str | None = None
) -> Candidate:
    return {
        "field": key,
        "value": value,
        "method": method,
        "context": context,
        "evidence": {**_citation(doc, quote), "context": context},
    }


def extract(doc: Document, website: str, name: str | None, as_of: str) -> list[Candidate]:
    if not doc.attributed:
        return []
    found: list[Candidate] = []
    first_party = doc.kind == "official" and same_site(doc.url, website)
    if first_party:
        for technology in doc.web_technologies:
            if technology in WEB_TECHNOLOGY:
                found.append(
                    candidate(
                        "web_technologies",
                        technology,
                        doc,
                        f"Website markup: {technology}",
                        "metadata",
                        "public_website",
                    )
                )
        for node in doc.records:
            if not node_types(node).intersection({"Product", "Service"}):
                continue
            provider = node.get("provider") or node.get("manufacturer") or node.get("brand")
            provider = provider.get("name") if isinstance(provider, dict) else provider
            offering = node.get("name")
            if not (
                name
                and isinstance(provider, str)
                and name_key(provider) == name_key(name)
                and isinstance(offering, str)
                and offering.strip()
            ):
                continue
            found.append(
                candidate(
                    "products_services",
                    offering,
                    doc,
                    f"{provider} offers {offering}",
                    "structured",
                )
            )
            if "Service" in node_types(node) and re.search(
                r"\b(?:support|managed services|professional services)\b", offering, re.I
            ):
                found.append(
                    candidate(
                        "support_services",
                        offering,
                        doc,
                        f"Service name: {offering}",
                        "structured",
                    )
                )
    for node in site_identity(doc, name) if first_party else []:
        code = str(node.get("naics", "")).strip()
        if code in naics_titles():
            found.append(candidate("naics_code", code, doc, f"NAICS: {code}", "structured"))
        for key, attr in (
            ("name", "name"),
            ("legal_name", "legalName"),
            ("about", "description"),
            ("phone", "telephone"),
            ("industry", "industry"),
        ):
            value = node.get(attr)
            if isinstance(value, str) and value.strip():
                found.append(candidate(key, value, doc, f"{attr}: {value}", "structured"))
        year = re.fullmatch(r"(\d{4})(?:-\d{2}-\d{2})?", str(node.get("foundingDate", "")))
        if year and 1500 <= int(year[1]) <= date.fromisoformat(as_of).year:
            found.append(
                candidate(
                    "founded_year", int(year[1]), doc, f"foundingDate: {year[0]}", "structured"
                )
            )
        employee = node.get("numberOfEmployees")
        if isinstance(employee, dict) and type(employee.get("value")) is int:
            found.append(
                candidate(
                    "employees",
                    employee["value"],
                    doc,
                    f"numberOfEmployees: {employee['value']}",
                    "structured",
                )
            )
        address = node.get("address")
        # A generic organization address may be a branch. Only a location
        # identified as headquarters (or a single corporate address) is used.
        if isinstance(address, dict) and (
            doc.url.rstrip("/") == website.rstrip("/")
            or "headquarter" in words(str(address.get("name", "")))
        ):
            for key, attr in (
                ("address", "streetAddress"),
                ("city", "addressLocality"),
                ("region", "addressRegion"),
                ("postal_code", "postalCode"),
                ("country", "addressCountry"),
            ):
                value = address.get(attr)
                if isinstance(value, str) and value:
                    output_key = (
                        "country_code"
                        if key == "country" and re.fullmatch("[A-Z]{2}", value)
                        else key
                    )
                    found.append(
                        candidate(output_key, value, doc, f"{attr}: {value}", "structured")
                    )
    if first_party and doc.url.rstrip("/") == website.rstrip("/"):
        if name and any(name_key(part) == name_key(name) for part in title_segments(doc.title)):
            found.append(candidate("name", name, doc, doc.title, "pattern"))
        for node in doc.records:
            if node.get("@type") != "PageMetadata":
                continue
            if node.get("description"):
                found.append(
                    candidate("about", node["description"], doc, node["description"], "metadata")
                )
            if name and node.get("site_name") and name_key(node["site_name"]) == name_key(name):
                found.append(
                    candidate("name", node["site_name"], doc, node["site_name"], "metadata")
                )
        found.extend(
            [
                candidate("domain", host(website), doc, f"Official site: {website}", "identity"),
                candidate("website", website, doc, f"Official site: {website}", "identity"),
            ]
        )
    if first_party:
        profiles = [
            entry
            for node in site_identity(doc, name)
            for entry in (
                node.get("sameAs") if isinstance(node.get("sameAs"), list) else [node.get("sameAs")]
            )
        ]
        profiles.extend(link for link, _ in doc.links)
        for entry in profiles:
            if not isinstance(entry, str):
                continue
            with contextlib.suppress(ValueError):
                address = url(entry)
                if host(address) == "linkedin.com" and re.match(
                    r"https?://[^/]+/company/[^/]+", address
                ):
                    found.append(candidate("linkedin_company", address, doc, address, "structured"))
                    handle = "/".join(urlsplit(address).path.strip("/").split("/")[:2])
                    found.append(candidate("linkedin_handle", handle, doc, address, "structured"))
                elif (
                    host(address) == "facebook.com"
                    and re.fullmatch(r"/[A-Za-z0-9._-]+/?", urlsplit(address).path)
                    and urlsplit(address).path.strip("/") not in {"share", "sharer.php", "login"}
                ):
                    found.append(candidate("facebook_company", address, doc, address, "structured"))
                elif host(address) in {"x.com", "twitter.com"} and re.fullmatch(
                    r"/[A-Za-z0-9_]{1,15}/?", urlsplit(address).path
                ):
                    found.append(
                        candidate(
                            "twitter_handle",
                            urlsplit(address).path.strip("/"),
                            doc,
                            address,
                            "structured",
                        )
                    )

    sections = passages(doc)
    if first_party or doc.kind == "filing":
        for passage in sections:
            code = re.search(r"\bNAICS\s*(?:code\s*)?[:#]?\s*([0-9]{6})\b", passage, re.I)
            if (
                code
                and code[1] in naics_titles()
                and (doc.kind == "filing" or subject(passage, name))
            ):
                found.append(candidate("naics_code", code[1], doc, passage, "pattern"))
            year = re.search(
                r"\b(?:founded|established)\s+in\s+(1[5-9]\d\d|20\d\d)\b", passage, re.I
            )
            if year and int(year[1]) <= date.fromisoformat(as_of).year and subject(passage, name):
                found.append(candidate("founded_year", int(year[1]), doc, passage, "pattern"))
            if subject(passage, name, allow_first_person=False):
                for template, value in (
                    (r"\b(?:is|became) (?:a )?publicly traded\b", True),
                    (r"\bis (?:a )?privately held\b", False),
                ):
                    if re.search(template, passage, re.I):
                        found.append(candidate("is_public", value, doc, passage, "pattern"))
            if subject(passage, name) or doc.kind == "filing":
                # An approximate value or a time period expressed as a range
                # cannot safely populate an exact count.
                headcount = re.search(
                    r"\b(?:we (?:had|have|employ)|company (?:had|has|employs))\s+"
                    r"([1-9]\d{0,2}(?:,\d{3})+|[1-9]\d*) (?:full-time )?employees\b",
                    passage,
                    re.I,
                )
                if headcount:
                    found.append(
                        candidate(
                            "employees",
                            int(headcount[1].replace(",", "")),
                            doc,
                            passage,
                            "pattern",
                        )
                    )
                found.extend(_financial_disclosure(doc, passage))
    if doc.expires and doc.expires < as_of:
        return found
    tech_seen = set()
    for passage in sections:
        if doc.kind == "jobs":
            context = "hiring requirement or role description"
        elif doc.kind == "official":
            context = "first-party mention"
        else:
            context = "published mention"
        if doc.kind in {"news", "external"} and not same_site(doc.url, website):
            if not subject(passage, name, allow_first_person=False):
                continue
        for technology, relation in technologies(
            passage,
            kind=doc.kind,
            path=urlsplit(doc.url).path,
            first_party=same_site(doc.url, website),
        ).items():
            if (technology, relation) in tech_seen:
                continue
            tech_seen.add((technology, relation))
            found.append(
                candidate("technology_stack", technology, doc, passage, "pattern", relation)
            )
        normalized = f" {words(passage)} "
        if first_party and business_activity_statement(passage, name):
            for activity, phrases in _ACTIVITIES.items():
                if any(f" {phrase} " in normalized for phrase in phrases):
                    found.append(
                        candidate("business_activities", activity, doc, passage, "pattern")
                    )
        for theme, phrases in _THEMES.items():
            if not any(phrase in passage.casefold() for phrase in phrases):
                continue
            if doc.kind == "jobs" or re.search(
                r"\b(?:launch\w*|build\w*|expand\w*|migrat\w*|invest\w*|moderniz\w*|implement\w*)\b",
                passage,
                re.I,
            ):
                found.append(
                    candidate(
                        "capabilities_in_development", theme, doc, passage, "pattern", context
                    )
                )
                if theme in _SERVICE:
                    found.append(
                        candidate(
                            "service_need_hypotheses",
                            _SERVICE[theme],
                            doc,
                            passage,
                            "pattern",
                            f"Possible need suggested by {theme} ({context})",
                        )
                    )
    return found


def business_activity_statement(text: str, company: str | None) -> bool:
    """A topic or referenced product is insufficient evidence of an offering."""
    if "?" in text or not subject(text, company):
        return False
    return bool(
        re.search(
            r"\b(?:offers?|provides?|builds?|develops?|delivers?|operates?|conducts?|"
            r"our (?:products?|services?|platform|software))\b",
            text,
            re.I,
        )
    )


def title_segments(title: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s+[|–—:-]\s+", title)]


def _financial_disclosure(doc: Document, passage: str) -> list[Candidate]:
    """Record a reported amount with its fiscal period and original currency."""
    text = passage.lower()
    if re.search(
        r"\b(?:estimat\w*|forecast\w*|expect\w*|guidance|projected|targeted|between)\b", text
    ):
        return []
    amounts = list(_MONEY.finditer(passage))
    if len(amounts) != 1:
        return []
    amount = amounts[0]
    if re.match(r"\s*(?:[-–]|to \d)", passage[amount.end() :]):
        return []
    currency = _CURRENCY.get(amount[1].upper(), amount[1].upper())
    number = Decimal(amount[2].replace(",", "") + (amount[3] or ""))
    number *= _SCALE.get((amount[4] or "").lower(), 1)
    if number > 10**18:
        return []
    value = int(number) if number == number.to_integral_value() else float(number)
    period = re.search(r"\b(?:fiscal year|fy)\s*(20\d{2})\b", text)
    revenue = re.search(r"\b(?:annual|full.year|total) revenue\b", text)
    if period and revenue and re.search(r"\b(?:reported|was|reached|totaled)\b", text):
        context = f"FY {period[1]}, {currency}"
        return [
            candidate("annual_revenue", value, doc, passage, "pattern", context),
            candidate("revenue_currency", currency, doc, passage, "pattern", context),
            candidate("revenue_fiscal_year", int(period[1]), doc, passage, "pattern", context),
        ]
    if re.search(r"\b(?:raised|reported) (?:a total of|cumulative funding of)\b", text):
        return [candidate("total_funding", value, doc, passage, "pattern", currency)]
    return []


def labeled_fields(doc: Document, specs: list[FieldSpec]) -> list[Candidate]:
    """Read fixed publisher labels using only literal prefix matching."""
    if not doc.attributed or doc.kind not in {"official", "filing"}:
        return []
    output = []
    for spec in specs:
        if not spec.labels or spec.derive_from or spec.inference:
            continue
        labels = {words(label) for label in spec.labels}
        for row in doc.text.splitlines():
            label, separator, raw = row.partition(":")
            if not separator or words(label) not in labels:
                continue
            raw = raw.strip()
            try:
                if spec.type == "integer" and re.fullmatch(r"\d+|\d{1,3}(?:,\d{3})+", raw):
                    value = int(raw.replace(",", ""))
                elif spec.type == "number" and re.fullmatch(r"\d+(?:\.\d+)?", raw):
                    value = float(raw)
                elif spec.type == "boolean" and raw.lower() in {"true", "false", "yes", "no"}:
                    value = raw.lower() in {"true", "yes"}
                else:
                    value = raw
                value = normalize(spec, value)
            except ValueError:
                continue
            output.append(candidate(spec.key, value, doc, row, "pattern"))
    return output


def subject(text: str, name: str | None, *, allow_first_person: bool = True) -> bool:
    return bool(name and name_key(name) and f" {name_key(name)} " in f" {words(text)} ") or (
        allow_first_person and bool(re.search(r"\b(?:we|our company)\b", text, re.I))
    )


def passages(doc: Document) -> list[str]:
    all_text = [doc.title, *doc.text.splitlines()]
    for record in doc.records:
        for key in ("description", "headline"):
            value = record.get(key)
            if isinstance(value, str):
                all_text.append(clean(re.sub(r"<[^>]{0,500}>", " ", value), 10000))
    output = []
    for part in all_text:
        # Splitting into short spans keeps evidence quotations near the match.
        pieces = re.split(r"(?<=[.!?])\s+|\n+", part)
        for piece in pieces:
            output.extend(clean(piece[i : i + 480], 480) for i in range(0, len(piece), 440))
    return list(dict.fromkeys(p for p in output if p))


def _fresh(evidence: dict, days: int | None, as_of: str) -> bool:
    stamp = date_value(evidence.get("published"))
    if stamp and stamp > as_of:
        return False
    if days is None:
        return True
    if not stamp and evidence.get("active_job_board"):
        stamp = date_value(evidence.get("accessed_at"))
    return bool(stamp and (date.fromisoformat(as_of) - date.fromisoformat(stamp)).days <= days)


def normalize(field: FieldSpec, value: Any) -> str | int | float | bool:
    if field.type == "boolean" and type(value) is bool:
        return value
    if field.type == "integer" and type(value) is int and 0 <= value <= 10**15:
        return value
    if field.type == "number" and type(value) in (int, float) and 0 <= value <= 10**18:
        return value
    if isinstance(value, str):
        if field.type == "text" and clean(value, 380):
            return clean(value, 380)
        if field.type == "enum":
            options = {words(option): option for option in field.options}
            if words(value) in options:
                return options[words(value)]
        if field.type == "url":
            return url(value)
        if field.type == "date" and date_value(value):
            return date_value(value)
    raise ValueError("value does not match its target field type or options")


def verify_proposals(
    proposals: list,
    docs: list[Document],
    specs: list[FieldSpec],
    selections: dict[str, list[dict]],
    method: str,
) -> list[Candidate]:
    inputs = {row["id"]: row for rows in selections.values() for row in rows}
    fields = {spec.key: spec for spec in specs}
    sources = {doc.url: doc for doc in docs}
    accepted = []
    for proposal in proposals[:120]:
        if not isinstance(proposal, dict) or set(proposal) - {
            "field",
            "passage_id",
            "quote",
            "value",
            "context",
        }:
            continue
        key = proposal.get("field")
        ref = proposal.get("passage_id")
        quote = proposal.get("quote")
        if not all(isinstance(item, str) for item in (key, ref, quote)):
            continue
        entry = inputs.get(ref)
        field = fields.get(key)
        if not field or not entry or entry not in selections.get(key, []) or field.derive_from:
            continue
        quote = clean(quote, 380)
        if len(quote) < 12 or quote not in entry["text"]:
            continue
        try:
            value = normalize(field, proposal.get("value"))
        except ValueError:
            continue
        # For factual fields require a verbatim value (or its numeric spelling).
        # A boolean is accepted only with an explicit source phrase.
        token = words(quote)
        if field.type == "boolean":
            literal = (
                ("publicly traded", "listed on") if value else ("privately held", "private company")
            )
            literal = literal if field.key == "is_public" else (str(value).lower(),)
            supported = any(f" {lexeme} " in f" {token} " for lexeme in literal)
            if (
                field.key == "is_public"
                and value
                and re.search(r"\b(?:not|never|no longer)\b", token)
            ):
                supported = False
        elif field.type in {"integer", "number"}:
            supported = bool(
                re.search(
                    r"(?<![\d.])" + re.escape(str(value)) + r"(?!\d|\.\d)",
                    quote.replace(",", ""),
                )
            )
        else:
            supported = bool(words(str(value)) and f" {words(str(value))} " in f" {token} ")
        if not field.inference and not supported:
            continue
        context = clean(proposal.get("context"), 300) or None
        if field.inference and not context:
            continue
        doc = sources[entry["url"]]
        if doc.quarantined or any(suspicious(part) for part in (quote, str(value), context or "")):
            continue
        if doc.kind == "jobs":
            context = f"Hiring signal. {context}" if context else "Hiring signal"
        accepted.append(candidate(key, value, doc, quote, method, context))
    return accepted


def _empty(status: str, reason: str) -> dict:
    return {"status": status, "reason": reason, "value": None, "evidence": []}


def eligible(spec: FieldSpec, results: dict) -> dict | None:
    for condition in spec.applies_when:
        upstream = results.get(condition.field, {})
        if upstream.get("status") not in {"observed", "derived"}:
            return _empty("blocked", f"Requires a resolved {condition.field}")
        if (
            type(upstream["value"]) is not type(condition.equals)
            or upstream["value"] != condition.equals
        ):
            return _empty("not_applicable", f"Condition on {condition.field} was not satisfied")
    return None


def _groups(
    spec: FieldSpec,
    candidates: list[Candidate],
    as_of: str,
    *,
    suggestions: bool = False,
) -> list[dict]:
    groups = defaultdict(list)
    for item in candidates:
        evidence = item["evidence"]
        max_age = spec.max_age_days
        if (
            spec.key == "technology_stack"
            and evidence["source"] == "official"
            and not evidence.get("published")
        ):
            max_age = None
        if evidence["source"] not in spec.sources or not _fresh(evidence, max_age, as_of):
            continue
        try:
            value = normalize(spec, item["value"])
        except ValueError:
            continue
        comparable = words(value) if isinstance(value, str) and spec.type == "text" else value
        groups[comparable].append({**item, "value": value})
    output = []
    for values in groups.values():

        def rank(item):
            evidence = item["evidence"]
            weight = {
                "identity": 4,
                "structured": 3,
                "pattern": 2,
                "ide": 1,
                "model": 1,
                "metadata": 0,
            }
            return (
                _AUTHORITY[evidence["source"]],
                evidence["published"] or "",
                weight[item["method"]],
            )

        ordered = sorted(values, key=lambda c: (rank(c), c["evidence"]["url"]), reverse=True)
        quotes = {}
        origins = set()
        fingerprints = set()
        for item in ordered:
            evidence = item["evidence"]
            relation = evidence.get("context") if spec.key == "technology_stack" else None
            quotes.setdefault((evidence["origin"], evidence["url"], relation), evidence)
            quote = words(evidence["quote"])
            keys = {
                key
                for key in (
                    evidence.get("content_sha256"),
                    quote if len(quote) >= 48 else None,
                )
                if key
            }
            if not fingerprints.intersection(keys):
                origins.add(evidence["origin"])
            fingerprints.update(keys)
        if not suggestions and len(origins) < spec.min_origins:
            continue
        top = ordered[0]
        authority, stamp, method_rank = rank(top)
        output.append(
            {
                "value": top["value"],
                "method": top["method"],
                "context": top["context"],
                "evidence": list(quotes.values())[:5],
                "independent_origins": len(origins),
                "rank": (authority, len(origins), stamp if spec.max_age_days else "", method_rank),
            }
        )
    return sorted(output, key=lambda g: (g["rank"], str(g["value"])), reverse=True)


def resolve(specs: list[FieldSpec], candidates: list[Candidate], as_of: str) -> dict:
    buckets = defaultdict(list)
    for candidate_item in candidates:
        buckets[candidate_item["field"]].append(candidate_item)
    results: dict = {}
    for spec in specs:
        ineligible = eligible(spec, results)
        if ineligible:
            results[spec.key] = ineligible
            continue
        if spec.derive_from:
            upstream = results[spec.derive_from]
            if upstream["status"] not in {"observed", "derived"}:
                results[spec.key] = _empty("blocked", f"Requires a resolved {spec.derive_from}")
                continue
            match = next((band for band in spec.bands if upstream["value"] <= band.through), None)
            results[spec.key] = (
                {
                    "status": "derived",
                    "value": match.value,
                    "evidence": upstream["evidence"],
                    "derived_from": spec.derive_from,
                }
                if match
                else _empty("missing", "Value is outside configured bands")
            )
            continue
        candidates = buckets[spec.key]
        proposals = _groups(
            spec,
            [item for item in candidates if item["method"] in {"model", "ide"}],
            as_of,
            suggestions=True,
        )
        choices = _groups(
            spec,
            [item for item in candidates if item["method"] not in {"model", "ide"}],
            as_of,
        )
        pending = {"proposals": proposals[:8]} if proposals else {}
        if not choices:
            results[spec.key] = (
                {**_empty("proposed", "Agent observations require human review"), **pending}
                if proposals
                else _empty("missing", "No attributable evidence met the field policy")
            )
            continue
        best = choices[0]
        for choice in choices:
            choice["priority"] = choice.pop("rank")
        if spec.multiple:
            limit = 50 if spec.key in {"technology_stack", "web_technologies"} else 20
            results[spec.key] = {
                "status": "inferred" if spec.inference else "observed",
                "value": [c["value"] for c in choices[:limit]],
                "evidence": best["evidence"],
                "items": choices[:limit],
                **pending,
            }
        elif len(choices) > 1 and best["priority"] == choices[1]["priority"]:
            results[spec.key] = {
                **_empty("conflict", "Equally ranked sources disagree"),
                "alternatives": choices[:6],
                **pending,
            }
        else:
            results[spec.key] = {
                "status": "inferred" if spec.inference else "observed",
                **best,
                "alternatives": choices[1:6],
                **pending,
            }
    return results


def crm_preview(specs: list[FieldSpec], results: dict) -> dict:
    properties, pending = {}, {}
    for spec in specs:
        if not spec.hubspot or spec.hubspot in CRM_ONLY or spec.inference:
            continue
        result = results[spec.key]
        if result["status"] == "proposed":
            pending[spec.hubspot] = [item["value"] for item in result["proposals"]]
        if result["status"] not in {"observed", "derived"}:
            continue
        requires_mapping = {"industry", "annualrevenue", "total_money_raised"}
        if spec.type == "enum" or spec.hubspot in requires_mapping or spec.multiple:
            pending[spec.hubspot] = result["value"]
        elif (
            spec.key not in {"name", "domain", "website"}
            and result.get("independent_origins", 0) < 2
        ):
            pending[spec.hubspot] = result["value"]
        else:
            properties[spec.hubspot] = result["value"]
    return {"properties": properties, "needs_mapping_or_review": pending}

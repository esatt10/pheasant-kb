"""Publish searchable context from code-owned data and corroborated facts."""

from __future__ import annotations

from typing import Any

from pheasant_gtm import field_report, sales
from pheasant_gtm.catalog import DEFAULT_FIELDS, FieldSpec, Scalar, words
from pheasant_gtm.evidence import _ACTIVITIES, _SERVICE, _THEMES, business_activity_statement
from pheasant_gtm.naics import titles as naics_titles
from pheasant_gtm.sources import date_value, host, root
from pheasant_gtm.tech_taxonomy import concept_id, technology_labels
from pheasant_gtm.technology import RELATIONS, TECHNOLOGY_CATEGORY, WEB_TECHNOLOGY

_VOCABULARIES = {
    "technology_stack": TECHNOLOGY_CATEGORY,
    "web_technologies": WEB_TECHNOLOGY,
    "business_activities": _ACTIVITIES,
    "capabilities_in_development": frozenset(_THEMES),
    "service_need_hypotheses": frozenset(_SERVICE.values()),
}
_FIRMOGRAPHICS = {"employees", "founded_year", "is_public", "annual_revenue", "revenue_fiscal_year"}
_HUBSPOT = {
    "name": "Name",
    "domain": "Company domain name",
    "website": "Website URL",
    "industry": "Industry",
    "industry_group": "Industry group",
    "employees": "Number of employees",
    "employee_range": "Employee range",
    "annual_revenue": "Annual revenue",
    "is_public": "Is public",
    "founded_year": "Year founded",
    "country_code": "Country/Region code",
    "web_technologies": "Web Technologies",
}
_MAPPED = {"industry", "industry_group", "employee_range", "annual_revenue", "web_technologies"}
_DEFAULT_BY_KEY = {spec.key: spec for spec in DEFAULT_FIELDS}


def _fact_values(spec: FieldSpec, entry: dict) -> list[Scalar]:
    raw = entry["value"]
    values = raw if isinstance(raw, list) else [raw]
    if spec.key in _VOCABULARIES:
        return sorted(
            {
                value
                for value in values
                if isinstance(value, str) and value in _VOCABULARIES[spec.key]
            }
        )
    if spec.key == "naics_code":
        return [value for value in values if isinstance(value, str) and value in naics_titles()]
    if spec.key == "country_code":
        return [
            value
            for value in values
            if isinstance(value, str)
            and len(value) == 2
            and value.isascii()
            and value.isupper()
            and value.isalpha()
        ]
    if spec.type == "enum":
        # Only the operator's configured options can become text in context.
        return [value for value in values if value in spec.options]
    official = spec.key in _FIRMOGRAPHICS and any(
        cite["source"] in {"official", "filing"} for cite in entry["evidence"]
    )
    if entry.get("independent_origins", 0) < 2 and not official:
        return []
    return [value for value in values if type(value) in {bool, int, float}]


_reference = field_report.evidence_id


def field_results(fields: dict, related: dict | None = None) -> dict:
    """Stable, analyst-only values and citations for the indexed revision."""

    def attributed(entry: dict) -> dict:
        return {
            **{
                key: entry[key]
                for key in (
                    "value",
                    "status",
                    "reason",
                    "method",
                    "independent_origins",
                    "derived_from",
                )
                if key in entry
            },
            "evidence": [
                {
                    key: cite.get(key)
                    for key in ("url", "title", "source", "published", "quote", "context")
                }
                for cite in entry.get("evidence", [])
            ],
        }

    related = related or {}
    return {
        key: {
            **attributed(entry),
            **{
                group: [attributed(choice) for choice in entry[group]]
                for group in ("items", "alternatives", "proposals")
                if entry.get(group)
            },
            **(
                {
                    "related": [
                        {
                            "id": row["id"],
                            "url": row["url"],
                            "title": row["title"],
                            "source": row["source"],
                            "published": row["published"],
                            "quote": row["text"],
                        }
                        for row in related[key]
                    ]
                }
                if key in related
                else {}
            ),
        }
        for key, entry in fields.items()
    }


def curated(
    specs: list[FieldSpec],
    profile: dict,
    company: str | None,
    pages: dict | None = None,
    inventory: list[dict] | None = None,
) -> dict:
    """The published, typed projection. Publisher prose stays in private evidence."""
    sources = {
        row["url"]: row
        for row in profile["source_outcomes"]
        if row["attributed"] and not row["quarantined"]
    }
    overview = field_report.company_overview(profile, pages or {}, company)
    buyers, recovered, sales_signals = sales.archived(profile, pages or {})
    offers_by_id = {item["id"]: item for item in inventory or ()}
    for item in recovered:
        existing = offers_by_id.setdefault(item["id"], item)
        existing["sources"] = sorted(set(existing["sources"]) | item["sources"])
    inventory = list(offers_by_id.values())
    if not overview and recovered:
        overview = field_report.offering_overview(company or profile.get("company"), recovered)
    evidence: dict[str, dict] = {}
    fields = {}

    def reference(address: str, *, field: str | None = None) -> None:
        source = sources.get(address)
        if source is None:
            return
        evidence_id = _reference(address)
        entry = evidence.setdefault(
            evidence_id,
            {
                "id": evidence_id,
                "kind": source["kind"],
                "publisher": host(address),
                "published": source["published"],
                "field_keys": [],
            },
        )
        if field is not None:
            entry["field_keys"].append(field)

    domain = host(profile["website"])
    classification = profile.get("classification")
    fallback = bool((profile["identity"].get("selected") or {}).get("fallback_url"))
    publisher_only = fallback and profile.get("configured_domain") not in {None, domain}
    reference(profile["website"])
    for entries in profile["signals"].values():
        for entry in entries:
            reference(entry["url"])
    for signal in sales_signals:
        for address in signal["sources"]:
            reference(address)
    offer_rows = []
    for item in inventory or ():
        if not (urls := set(item["sources"]) & sources.keys()):
            continue
        offer_rows.append(
            {
                **{key: item[key] for key in ("id", "name", "kind", "role")},
                "evidence_ids": sorted(_reference(address) for address in urls),
                **{
                    key: [
                        {
                            "value": value,
                            "evidence_ids": sorted(_reference(url) for url in addresses),
                        }
                        for value, addresses in terms.items()
                    ]
                    for key, terms in item["facets"].items()
                },
            }
        )
    offer_fields = {"products_services": {}, "customer_segments": buyers, "support_services": {}}
    for item in inventory or ():
        key = "support_services" if item["kind"] == "support" else "products_services"
        offer_fields[key].setdefault(item["name"], set()).update(item["sources"])
        for field, facet in (("customer_segments", "buyers"), ("support_services", "support")):
            for value, urls in item["facets"].get(facet, {}).items():
                offer_fields[field].setdefault(value, set()).update(urls)
    for spec in specs:
        entry = profile["fields"][spec.key]
        ready = entry["status"] in {"observed", "derived", "inferred"}
        values = _fact_values(spec, entry) if ready else []
        status = entry["status"]
        if spec.key == "business_activities" and values:
            supported = {
                item["value"]
                for item in entry.get("items", ())
                if any(
                    business_activity_statement(cite["quote"], profile.get("company") or company)
                    for cite in item["evidence"]
                )
            }
            values = [value for value in values if value in supported]
            if not values:
                status = "missing"
        if spec.key == "name" and company:
            values, status = [company], "derived" if not ready else status
        if ready and spec.key in {"domain", "website"}:
            values = (
                {
                    "domain": [domain],
                    "website": [root(profile["website"])],
                }[spec.key]
                if not publisher_only
                else []
            )
        if classification and spec.key in {"naics_code", "industry", "industry_group"}:
            label = classification[
                {"naics_code": "code", "industry": "industry", "industry_group": "sub_industry"}[
                    spec.key
                ]
            ]
            if label:
                values, status = [label], "derived"
        citations = {row["url"] for row in entry["evidence"]}
        detail = None
        if (
            overview
            and spec == _DEFAULT_BY_KEY.get(spec.key)
            and status in {"missing", "observed", "derived"}
        ):
            clauses = overview["clauses"]
            if spec.key == "products_services":
                clauses = [
                    (product, urls) for product, urls in sorted(overview["products"].items())
                ]
            elif spec.key == "description" and overview["mission"]:
                clauses = [
                    (text, urls)
                    for text, urls in clauses
                    if overview["mission"].endswith(f"{text}.")
                ]
            if (
                spec.key in {"about", "description", "business_activities", "products_services"}
                and clauses
            ):
                summary = overview["summary"]
                if spec.key == "products_services":
                    summary = (
                        "Named products include " + ", ".join(text for text, _ in clauses) + "."
                    )
                elif spec.key == "description":
                    summary = overview["mission"] or summary
                detail = {
                    "summary": summary,
                    "statements": [
                        {"text": text, "evidence_ids": sorted(_reference(url) for url in urls)}
                        for text, urls in clauses
                    ],
                }
                citations.update(url for _, urls in clauses for url in urls)
                if spec.key in {"about", "description"}:
                    values, status = [summary], "observed"
                elif spec.key == "products_services":
                    values, status = [text for text, _ in clauses], "observed"
        if (
            (observed := offer_fields.get(spec.key))
            and spec == _DEFAULT_BY_KEY.get(spec.key)
            and status in {"missing", "observed", "derived"}
        ):
            values = sorted(set(values) | observed.keys(), key=str.casefold)
            status = "observed"
            citations.update(url for urls in observed.values() for url in urls)
            detail = {
                **(detail or {}),
                "items": [
                    {
                        "value": value,
                        "text": "The company identifies this audience in its published material."
                        if spec.key == "customer_segments"
                        else "Documented in first-party offering evidence.",
                        "evidence_ids": sorted(_reference(url) for url in urls),
                    }
                    for value, urls in sorted(observed.items())
                ],
            }
        if (
            classification
            and classification["source"] == "reported"
            and spec.key in {"industry", "industry_group"}
        ):
            citations.update(row["url"] for row in profile["fields"]["naics_code"]["evidence"])
        for item in entry.get("items", []):
            if item["value"] in values:
                citations.update(row["url"] for row in item["evidence"])
        for address in sorted(citations):
            reference(address, field=spec.key)
        detail_items = field_report.item_details(spec.key, entry, values, sources.keys())
        if detail_items:
            detail = {**(detail or {}), "items": [*(detail or {}).get("items", ()), *detail_items]}
        fields[spec.key] = {
            "definition": spec.model_dump(mode="json"),
            "status": status,
            "value": (values if spec.multiple else values[0]) if values else None,
            "withheld": entry["value"] is not None and not values,
            "evidence_ids": sorted(
                _reference(address) for address in citations if address in sources
            ),
            **({"detail": detail} if detail else {}),
            **({"reason": entry["reason"]} if not values and entry.get("reason") else {}),
            **(
                {"related_passage_count": len(profile["related_passages"].get(spec.key, []))}
                if "related_passages" in profile
                else {}
            ),
        }
    technology = []
    for key in ("technology_stack", "web_technologies"):
        for item in profile["fields"].get(key, {}).get("items", []):
            name = item["value"]
            if name not in _VOCABULARIES[key] or name not in (fields[key]["value"] or []):
                continue
            relations: dict[str, set[str]] = {}
            for cite in item["evidence"]:
                relation = cite.get("context")
                if relation in RELATIONS and cite["url"] in sources:
                    relations.setdefault(relation, set()).add(_reference(cite["url"]))
            if relations:
                category = (
                    TECHNOLOGY_CATEGORY[name] if key == "technology_stack" else "Public website"
                )
                technology.append(
                    {
                        "name": name,
                        "category": category,
                        "category_id": concept_id(category),
                        "signals": [
                            {"relationship": relation, "evidence_ids": sorted(ids)}
                            for relation, ids in sorted(relations.items())
                        ],
                    }
                )
    hubspot = []
    for key, label in _HUBSPOT.items():
        field = fields.get(key)
        if field is None or field["value"] is None:
            continue
        status = (
            "mapping"
            if key in _MAPPED
            else "candidate"
            if key in profile["fields"]
            and any(
                spec.key == key and spec.hubspot in profile["hubspot"]["properties"]
                for spec in specs
            )
            else "review"
        )
        hubspot.append({"property": label, "value": field["value"], "status": status, "field": key})
    return {
        "company": company or domain,
        "domain": domain,
        **({"identity_method": "official_fallback"} if fallback else {}),
        "classification": classification,
        "technology": sorted(technology, key=lambda row: (row["category"], row["name"])),
        **({"offerings": offer_rows} if offer_rows else {}),
        **(
            {"offering_research": profile["offering_research"]}
            if "offering_research" in profile
            else {}
        ),
        "hubspot": hubspot,
        "signal_coverage": {kind: len(items) for kind, items in profile["signals"].items()},
        "sales_brief": sales.briefing(fields, offer_rows, sales_signals),
        **({"research": profile["research"]} if "research" in profile else {}),
        "fields": fields,
        "evidence": [evidence[key] for key in sorted(evidence)],
    }


def markdown(
    specs: list[FieldSpec], profile: dict[str, Any], company: str | None, publication: dict
) -> str:
    # The display label comes from the source's operator-owned configuration.
    # Normalize it to one Markdown-safe line.
    label = words(company or "").strip()
    official = host(profile["website"])
    lines = ["# GTM company context", "", f"Company domain: {official}"]
    if (profile["identity"].get("selected") or {}).get("fallback_url"):
        lines.append("Identity source: operator-supplied official fallback")
    if label:
        lines.append(f"Configured company: {label}")
    activity = publication["fields"].get("business_activities", {})
    overview = activity.get("detail", {}).get("summary")
    if overview:
        lines.extend(["", f"**What they do.** {overview}"])
        statements = activity["detail"]["statements"]
        refs = sorted({ref for item in statements for ref in item["evidence_ids"]})
        lines.append(f"Source references: {', '.join(refs)}")
    if classification := profile.get("classification"):
        lines.append(
            f"NAICS {classification['edition']}: {classification['code']} · "
            f"{classification['industry']}"
        )
        if classification["sub_industry"]:
            lines.append(f"Sub-industry: {classification['sub_industry']}")
    lines.extend(["", "## Sourced facts and research signals", ""])
    named_technology = set()
    for spec in specs:
        entry = publication["fields"][spec.key]
        if entry["status"] not in {"observed", "derived", "inferred"}:
            continue
        if spec.key in {"name", "domain", "website", "industry", "industry_group", "naics_code"}:
            continue
        if spec.key in {"about", "description"} and overview == entry["value"]:
            continue
        values = entry["value"] if spec.multiple else [entry["value"]]
        if values is None or values == [None] or not values:
            continue
        if spec.key in {"technology_stack", "web_technologies"}:
            named_technology.update(value for value in values if isinstance(value, str))
        # The names and excerpts of publishers never become context. A
        # digest still joins this line to the analyst-only evidence export.
        citations = entry["evidence_ids"]
        label = spec.key.replace("_", " ").capitalize()
        safely_rendered = ", ".join(" ".join(str(value).split()) for value in values)
        lines.append(f"- {label} ({entry['status']}): {safely_rendered}")
        if citations:
            lines.append(f"  Source references: {', '.join(citations)}")
    if categories := technology_labels(named_technology):
        lines.extend(["", "## Technology categories", ""])
        lines.extend(f"- {category}" for category in categories)
    if enriched := publication.get("enrichment", {}).get("fields"):
        lines.extend(["", "## Grounded company descriptions", ""])
        for field in enriched:
            label = field.replace("_", " ").capitalize()
            lines.extend([f"### {label}", ""])
            for item in publication["fields"][field]["detail"]["statements"]:
                lines.append(f"- {item['text']}")
                lines.append(f"  Source references: {', '.join(item['evidence_ids'])}")
            lines.append("")
    brief = publication.get("sales_brief", {})
    if brief.get("signals") or brief.get("questions"):
        lines.extend(["", "## Sales briefing", ""])
        for item in brief.get("signals", ()):
            lines.append(f"- {item['date']} · {item['text']}")
            lines.append(f"  Source references: {', '.join(item['evidence_ids'])}")
        for item in brief.get("questions", ()):
            lines.append(f"- Discovery question: {item['text']}")
            lines.append(f"  Source references: {', '.join(item['evidence_ids'])}")
    if offers := publication.get("offerings"):
        lines.extend(["", "## Products and services", ""])
        labels = {
            "provider": "Company offering",
            "implementation_partner": "Implementation partner",
        }
        for offer in offers:
            lines.append(f"- **{offer['name']}** · {labels[offer['role']]}")
            for key, label in (
                ("capabilities", "Capabilities"),
                ("buyers", "Buyers"),
                ("delivery", "Delivery"),
                ("support", "Support"),
            ):
                if entries := offer.get(key):
                    lines.append(f"  {label}: {', '.join(row['value'] for row in entries)}")
            lines.append(f"  Source references: {', '.join(offer['evidence_ids'])}")
    lines.extend(["", "## Coverage", ""])
    for kind, entries in profile["signals"].items():
        lines.append(f"- {kind.capitalize()} documents meeting the signal window: {len(entries)}")
    for status, count in sorted(profile["field_status_counts"].items()):
        lines.append(f"- {status.capitalize()} research fields: {count}")
    dated = [
        (kind, date, _reference(item["url"]))
        for kind, items in profile["signals"].items()
        for item in items
        if (date := date_value(item["published"]))
    ]
    if dated:
        lines.extend(["", "## Dated signals", ""])
        lines.extend(
            f"- {date} {kind}. Source reference: {ref}"
            for kind, date, ref in sorted(dated, key=lambda row: (row[1], row[2]), reverse=True)
        )
    return "\n".join(lines) + "\n"

"""Sales evidence from verified company pages and bounded discovery questions."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from urllib.parse import urlsplit

from pheasant_gtm.catalog import words
from pheasant_gtm.field_report import evidence_id, first_party_pages
from pheasant_gtm.sources import date_value, same_site

_SERVES = r"(?:built for|designed for|serves?|helps?|for)\s+"
_BUYERS = {
    "small and midsized businesses": r"(?:built to help|designed for|serves?|helps?|for)\s+"
    r"(?:the\s+)?small\s*(?:and|&)\s*(?:mid[- ]?sized|medium[- ]?sized)\s+(?:companies|businesses)",
    "enterprise teams": _SERVES + r"(?:global\s+)?"
    r"(?:enterprise (?:teams|customers|businesses|organizations)|enterprises)",
    "manufacturers": _SERVES + r"(?:discrete |industrial )?manufacturers",
    "retailers": _SERVES + r"(?:online |omnichannel )?retailers",
    "software developers": _SERVES + r"(?:software |application )?developers",
    "original equipment manufacturers": r"(?:sales|systems|engines)\b.{0,125}\bto end users and "
    r"original equipment manufacturers",
    "business and residential shippers": r"\b(?:offerings|services|shipping) for business and "
    r"residential package shipping\b",
}
_OWNED = {
    "cohere.com": {
        "North": (
            "product",
            r"\bNorth (?:\d+: Enterprise AI|is (?:our|Cohere)|brings enterprise AI)\b",
        ),
        "Embed": ("product", r"\bIntroducing Embed \d+\b.{0,70}\bembedding models\b"),
        "Rerank": ("product", r"\bIntroducing Rerank \d+\b.{0,70}\bdocument retrieval\b"),
    },
    "fedex.com": {
        "Express package shipping": (
            "service",
            r"\bFederal Express\b.{0,190}\bofferings for "
            r"business and residential package shipping\b",
        ),
        "Global freight forwarding": (
            "service",
            r"\bFedEx Logistics provides\b.{0,110}\bglobal freight forwarding\b",
        ),
        "Customs brokerage": (
            "service",
            r"\bFedEx Logistics provides\b.{0,190}\bcustoms brokerage\b",
        ),
    },
    "hashicorp.com": {
        "Terraform": ("product", r"\bTerraform is an infrastructure as code tool\b"),
        "Packer": (
            "product",
            r"\b(?:HashiCorp )?Packer is (?:an open.source |a free and open.source )?"
            r"tool for creating identical machine images\b",
        ),
    },
}
_AUDIENCE_CONTEXT = re.compile(
    r"(?:\b(?:our|we|platform|product|software|service|offerings|"
    r"solutions?|tools?|systems|sales|customers|shipping)\b|^(?:built|designed) for)",
    re.I,
)
_EXCLUDED = re.compile(
    r"(?:^|/)(?:blog|news|careers|jobs|customer.stories|case.studies|"
    r"customer.reviews|marketplace|partners|community)(?:/|$)",
    re.I,
)
_DENIAL = re.compile(
    r"\b(?:not designed for|not intended for|not for|no longer|discontinued|"
    r"does not serve|do not serve|supports? partners who)\b",
    re.I,
)
_DATES = re.compile(r"/(20\d{2})[/-](\d{2})[/-](\d{2})(?:/|-)")
_EVENTS = (
    (
        "SAP S/4HANA transition announced",
        r"\bwe are transitioning to SAP S/4HANA\b",
        "Has the announced transition completed? Which integrations still need work?",
    ),
    (
        "Inference reliability engineering published",
        r"\bour team investigated a suspected memory leak in vLLM\b",
        "Which inference reliability issues remain priorities after the investigation?",
    ),
)
_QUESTIONS = (
    (
        r"warehouse management|inventory tracking|barcode scanning",
        "Where do scanning, inventory visibility, or replenishment still require manual work?",
    ),
    (
        r"manufacturing execution|opcenter|teamcenter|product lifecycle",
        "Which plant or engineering workflows need to exchange data across MES, PLM, and ERP?",
    ),
    (
        r"implementation partner|implementation services",
        "Which customer use cases would benefit from joint implementation or co-selling?",
    ),
    (
        r"infrastructure as code|terraform",
        "Which workflows need an integration or a reusable infrastructure template?",
    ),
    (
        r"freight forwarding|transport services|shipping",
        "Which shipment, tracking, or customs workflows would benefit from better data exchange?",
    ),
)


def archived(profile: dict, pages: dict) -> tuple[dict, list[dict], list[dict]]:
    """Recover buyer, offering, and dated signal facts from publisher statements."""
    buyers = defaultdict(set)
    offers = {}
    signals = {}
    for source, page in first_party_pages(profile, pages, ("official", "filing", "news")):
        url = page["url"]
        path = urlsplit(url).path
        lines = [
            line
            for line in page["text"].splitlines()[1:]
            if len(line) >= 30 and not _DENIAL.search(line)
        ]
        if source["kind"] != "news" and not _EXCLUDED.search(path):
            for label, pattern in _BUYERS.items():
                if any(
                    _AUDIENCE_CONTEXT.search(line) and re.search(pattern, line, re.I)
                    for line in lines
                ):
                    buyers[label].add(url)
        for domain, catalog in _OWNED.items():
            if not same_site(url, f"https://{domain}/"):
                continue
            for name, (kind, pattern) in catalog.items():
                if any(re.search(pattern, line, re.I) for line in lines):
                    offer = offers.setdefault(
                        name,
                        {
                            "id": f"provider:{words(name)}",
                            "name": name,
                            "kind": kind,
                            "role": "provider",
                            "sources": set(),
                            "facets": {},
                        },
                    )
                    offer["sources"].add(url)
        stamp = date_value(source["published"])
        if not stamp and (matched := _DATES.search(path)):
            stamp = date_value("-".join(matched.groups()))
        if (
            not stamp
            or not 0
            <= (date.fromisoformat(profile["as_of"]) - date.fromisoformat(stamp)).days
            <= 365
        ):
            continue
        for label, pattern, question in _EVENTS:
            if any(re.search(pattern, line, re.I) for line in lines):
                key = (label, stamp)
                signal = signals.setdefault(
                    key,
                    {
                        "text": label,
                        "date": stamp,
                        "kind": "company publication",
                        "sources": set(),
                        "question": question,
                    },
                )
                signal["sources"].add(url)
    return dict(buyers), list(offers.values()), list(signals.values())


def briefing(fields: dict, offers: list[dict], signals: list[dict]) -> dict:
    recent = []
    questions = []
    for signal in sorted(signals, key=lambda row: (row["date"], row["text"]), reverse=True)[:3]:
        refs = sorted(evidence_id(url) for url in signal["sources"])
        recent.append(
            {**{key: signal[key] for key in ("text", "date", "kind")}, "evidence_ids": refs}
        )
        questions.append({"text": signal["question"], "evidence_ids": refs})
    for key, question in (
        (
            "capabilities_in_development",
            "Which capabilities are you building now, and where would outside support help?",
        ),
        (
            "customer_segments",
            "Which documented customer segment is the priority for this campaign or partnership?",
        ),
    ):
        field = fields.get(key, {})
        if field.get("value") and field.get("evidence_ids"):
            questions.append({"text": question, "evidence_ids": field["evidence_ids"][:4]})
    for pattern, question in _QUESTIONS:
        for offer in offers:
            names = (offer["name"], *(row["value"] for row in offer.get("capabilities", ())))
            if any(re.search(pattern, name, re.I) for name in names):
                questions.append({"text": question, "evidence_ids": offer["evidence_ids"][:4]})
                break
    return {"signals": recent, "questions": questions[:4]}

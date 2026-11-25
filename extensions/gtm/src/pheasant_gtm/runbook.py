"""Private, deterministic passage retrieval for deep company research."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urlsplit

from pheasant_gtm.catalog import FieldSpec, words
from pheasant_gtm.evidence import candidate, passages, subject
from pheasant_gtm.sources import Document, same_site

_TRACKS = (
    (
        "Company and leadership",
        (
            "about",
            "description",
            "legal_name",
            "founded_year",
            "headquarters",
            "city",
            "country",
            "employees",
            "leadership",
            "phone",
        ),
        (
            "company overview",
            "about us",
            "who we are",
            "headquarters",
            "employee count",
            "our mission",
        ),
    ),
    (
        "Products and customer support",
        ("products_services", "support_services", "business_activities"),
        (
            "products and services",
            "solutions",
            "technical support",
            "service level",
            "customer support",
        ),
    ),
    (
        "Investors and annual reports",
        ("annual_revenue", "revenue_fiscal_year", "is_public", "ticker"),
        (
            "annual report",
            "investor relations",
            "annual revenue",
            "financial results",
            "stock exchange",
        ),
    ),
    (
        "Technology and investment",
        ("technology_stack", "capabilities_in_development", "service_need_hypotheses"),
        (
            "engineering",
            "modernization",
            "implementation",
            "technology stack",
            "technical documentation",
        ),
    ),
)
_TERM_TRACK = {key: terms for _, keys, terms in _TRACKS for key in keys}
_FIELD_QUERIES = {
    "about": ("company overview", "who we are", "we build", "we help", "our customers"),
    "description": ("our mission", "our purpose", "we exist to", "mission statement"),
    "products_services": (
        "our products",
        "our solutions",
        "products and services",
        "product portfolio",
    ),
    "support_services": (
        "customer support",
        "support plan",
        "service level agreement",
        "technical support",
    ),
    "employees": (
        "employees worldwide",
        "total employees",
        "full time employees",
        "global workforce",
    ),
    "technology_stack": ("experience with", "we use", "technology stack", "architecture"),
}
_AUTHORITY = {"filing": 3, "official": 3, "jobs": 2, "news": 2, "external": 1}
_ABOUT = re.compile(
    r"(?:^|/)(?:about(?:[-_]us)?|who-we-are|our-company|company|corporate)(?:[./_-]|$)", re.I
)
_ABOUT_HEADING = re.compile(
    r"^(?:about us|about the company|who we are|company overview|our company)$", re.I
)
_MISSION_HEADING = re.compile(r"^(?:our )?(?:mission|purpose|mission statement)$", re.I)
_MISSION_STATEMENT = re.compile(r"\b(?:our mission is|we exist to)\b", re.I)
_PURPOSE_STATEMENT = re.compile(r"^our purpose is\b", re.I)
_NAMED_PURPOSE = re.compile(r"\b(?:whose|its stated) purpose is\b", re.I)


@dataclass(frozen=True)
class Passage:
    document: Document
    text: str
    normalized: str
    title: str
    lead: bool


class PassageIndex:
    """Inverted index owned by one run. Normalized documents are checkpointed separately."""

    def __init__(self, documents: list[Document]):
        self.documents = [doc for doc in documents if doc.attributed and not doc.quarantined]
        self.passages: list[Passage] = []
        self.postings: dict[str, set[int]] = defaultdict(set)
        for doc in self.documents:
            title = words(doc.title)
            for ordinal, text in enumerate(passages(doc)):
                normalized = words(text)
                if len(normalized) < 24:
                    continue
                index = len(self.passages)
                lead = ordinal < 3
                self.passages.append(Passage(doc, text, f" {normalized} ", f" {title} ", lead))
                tokens = set(normalized.split())
                if lead:
                    tokens.update(title.split())
                for term in tokens:
                    self.postings[term].add(index)

    def retrieve(
        self,
        spec: FieldSpec,
        terms: list[str],
        admissible: Callable[[FieldSpec, Document], bool],
    ) -> list[dict]:
        # Field terms drive matching. Track phrases improve ranking among matches.
        field_terms = tuple(dict.fromkeys([*terms, *_FIELD_QUERIES.get(spec.key, ())]))
        matches: set[int] = set()
        for phrase in field_terms:
            for token in phrase.split():
                matches.update(self.postings.get(token, ()))
        ranked = []
        for index in matches:
            row = self.passages[index]
            doc = row.document
            if not admissible(spec, doc):
                continue
            field_score = sum(
                3 if " " in term else 2 for term in field_terms if f" {term} " in row.normalized
            )
            title_score = sum(f" {term} " in row.title for term in field_terms)
            if not field_score and not (title_score and row.lead):
                continue
            track_score = sum(
                f" {term} " in row.normalized for term in _TERM_TRACK.get(spec.key, ())
            )
            ranked.append(
                (
                    (
                        field_score,
                        track_score,
                        _AUTHORITY.get(doc.kind, 0),
                        title_score,
                        doc.published or "",
                        doc.url,
                        -index,
                    ),
                    row,
                )
            )
        ranked.sort(key=lambda entry: entry[0], reverse=True)
        selected, seen = [], set()
        per_url: Counter = Counter()
        for _, row in ranked:
            doc = row.document
            if row.normalized in seen or per_url[doc.url] >= 2:
                continue
            seen.add(row.normalized)
            per_url[doc.url] += 1
            selected.append(
                {
                    "id": sha256((doc.url + "\n" + row.text).encode()).hexdigest()[:24],
                    "source": doc.kind,
                    "url": doc.url,
                    "title": doc.title,
                    "published": doc.published,
                    "text": row.text,
                }
            )
            if len(selected) == 6:
                break
        return selected


def gaps(results: dict, fields: list[FieldSpec]) -> list[tuple[str, tuple[str, ...]]]:
    enabled = {field.key for field in fields if not field.derive_from}
    missing = {key for key in enabled if results[key]["status"] in {"missing", "conflict"}}
    return [(label, terms) for label, keys, terms in _TRACKS if missing.intersection(keys)]


def discovery_score(target: str, terms: tuple[str, ...]) -> int:
    path = f" {words(urlsplit(target).path)} "
    return sum(
        3
        if f" {term} " in path
        else int(any(f" {word} " in path for word in term.split() if len(word) >= 5))
        for term in terms
    )


def company_statements(index: PassageIndex, name: str | None, website: str) -> list[dict]:
    """Read literal first-party statements from official corporate pages."""
    found: dict[str, tuple[tuple, dict]] = {}
    for doc in index.documents:
        path = urlsplit(doc.url).path
        if (
            doc.kind != "official"
            or not same_site(doc.url, website)
            or not (doc.url.rstrip("/") == website.rstrip("/") or _ABOUT.search(path))
        ):
            continue
        lines = doc.text.splitlines()
        for i, line in enumerate(lines):
            line = line.strip()
            if _MISSION_STATEMENT.search(line):
                field, excerpt, strength = "description", line, 4
            elif _NAMED_PURPOSE.search(line) and subject(line, name, allow_first_person=False):
                field, excerpt, strength = "description", line, 3
            elif _PURPOSE_STATEMENT.match(line):
                field, excerpt, strength = "description", line, 2
            elif _MISSION_HEADING.fullmatch(line) and i + 1 < len(lines):
                field, excerpt = "description", lines[i + 1].strip()
                if len(excerpt) < 40 and i + 2 < len(lines) and not excerpt.endswith((".", ":")):
                    excerpt = lines[i + 2].strip()
                if not subject(excerpt, name) and not re.match(r"^(?:we|our)\b", excerpt, re.I):
                    continue
                strength = 3 if "mission" in line.casefold() else 1
            elif _ABOUT_HEADING.fullmatch(line) and i + 1 < len(lines):
                field, excerpt = "about", lines[i + 1].strip()
                if not subject(excerpt, name) and not re.match(r"^(?:we|our)\b", excerpt, re.I):
                    continue
                strength = 2 if subject(excerpt, name, allow_first_person=False) else 1
            else:
                continue
            if 40 <= len(excerpt) <= 900:
                rank = (strength, -path.strip("/").count("/"), doc.url, -i)
                if field not in found or rank > found[field][0]:
                    evidence = candidate(
                        field, excerpt, doc, excerpt, "pattern", line if line != excerpt else None
                    )
                    found[field] = (rank, evidence)
    return [result for _, result in found.values()]

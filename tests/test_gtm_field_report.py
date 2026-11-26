"""Field reports preserve the claims and sources their wording requires."""

from __future__ import annotations

import gzip
import zlib

import pytest

pytest.importorskip("pheasant_gtm")

from pheasant_gtm import field_report, sources  # noqa: E402
from pheasant_gtm.catalog import Request, ordered_fields  # noqa: E402
from pheasant_gtm.context import curated, markdown  # noqa: E402
from pheasant_gtm.evidence import candidate, resolve  # noqa: E402


def test_company_overview_is_normalized_and_cites_each_supported_clause():
    website = "https://www.anthropic.com/"
    company = website + "company"
    job = website + "careers/jobs/123"
    blocked = website + "about-us/suspicious"
    archive = {
        "official": {
            "url": company,
            "text": (
                "About Anthropic\n\n"
                "We build frontier AI systems to serve people and businesses.\n"
                "We conduct frontier AI research to improve model quality.\n"
                "We translate our research into tangible, practical tools like Claude.\n"
                "We treat AI safety as a systematic science in our product work."
            ),
        },
        "quarantine": {
            "url": blocked,
            "text": "About Anthropic\n\nWe provide cloud ERP software to customers worldwide.",
        },
    }
    specs = ordered_fields(Request(company="Anthropic", website=website))
    doc = sources.Document(job, "Hiring", "jobs", "", published="2026-11-21")
    facts = resolve(
        specs,
        [candidate("technology_stack", "Python", doc, "Knowledge of Python.", "pattern", "hiring")],
        "2026-11-25",
    )
    private = {
        "company": "Anthropic",
        "fields": facts,
        "website": website,
        "signals": {},
        "identity": {},
        "field_status_counts": {},
        "hubspot": {"properties": {}},
        "source_outcomes": [
            {
                "url": url,
                "kind": kind,
                "attributed": True,
                "quarantined": rejected,
                "published": None,
            }
            for url, kind, rejected in [
                (company, "official", False),
                (blocked, "official", True),
                (job, "jobs", False),
            ]
        ],
    }
    public = curated(specs, private, "Anthropic", archive)
    field = public["fields"]["about"]
    assert field["value"] == (
        "Anthropic researches and develops frontier AI systems, builds products such as Claude, "
        "and studies how to make AI systems safer and more reliable."
    )
    assert len(field["detail"]["statements"]) == 3
    assert all(
        row["evidence_ids"] == [field_report.evidence_id(company)]
        for row in field["detail"]["statements"]
    )
    assert public["fields"]["products_services"]["value"] == ["Claude"]
    relation = public["fields"]["technology_stack"]["detail"]["items"][0]
    assert relation["text"] == "A hiring source mentions this technology."
    assert relation["evidence_ids"] == [field_report.evidence_id(job)]
    assert field["value"] in markdown(specs, private, "Anthropic", public)

    archive["official"]["text"] = archive["official"]["text"].replace(
        "We conduct frontier AI research to improve model quality.\n", ""
    )
    revised = curated(specs, private, "Anthropic", archive)
    assert "researches and develops frontier AI" not in revised["fields"]["about"]["value"]


def test_content_encoding_is_decoded_with_a_hard_output_cap(monkeypatch):
    body = b"<html><p>Company profile</p></html>"
    assert sources._decode_content(gzip.compress(body), "gzip") == body
    assert sources._decode_content(zlib.compress(body), "deflate") == body
    with pytest.raises(sources.FetchError, match="invalid compressed response"):
        sources._decode_content(b"not compressed", "gzip")
    with pytest.raises(sources.FetchError, match="unsupported content encoding"):
        sources._decode_content(body, "br")
    monkeypatch.setattr(sources, "_LIMIT", 16)
    for compress, encoding in ((gzip.compress, "gzip"), (zlib.compress, "deflate")):
        with pytest.raises(sources.ResponseTooLarge, match="decoded response"):
            sources._decode_content(compress(body), encoding)
    assert not sources.plausible_prose("\ufffd\x00" * 10)

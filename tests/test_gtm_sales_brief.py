from __future__ import annotations

import pytest

pytest.importorskip("pheasant_gtm")

from pheasant_gtm import field_report, sales  # noqa: E402


def archive(website, configured_domain, entries):
    outcomes = []
    pages = {}
    for address, kind, text in entries:
        outcomes.append(
            {
                "url": address,
                "kind": kind,
                "attributed": True,
                "quarantined": False,
                "published": None,
            }
        )
        pages[field_report.evidence_id(address)] = {
            "url": address,
            "text": f"Archived page\n{text}",
        }
    return {
        "website": website,
        "configured_domain": configured_domain,
        "source_outcomes": outcomes,
        "as_of": "2026-11-25",
    }, pages


def test_issuer_archive_recovers_audience_and_offer_after_same_domain_fallback():
    issuer = "https://www.fedex.com/en-us/about/company-structure.html"
    report, pages = archive(
        "https://careers.fedex.com/",
        "fedex.com",
        [
            (
                issuer,
                "external",
                "FedEx provides customers and businesses worldwide with a broad portfolio "
                "of transportation, ecommerce, and business services.\n"
                "Federal Express provides expedited and standard offerings for business and "
                "residential package shipping.\nFedEx Logistics provides customers with "
                "configurable global freight forwarding and customs brokerage.",
            ),
            (
                "https://vendor.example/company",
                "external",
                "FedEx Logistics provides services for manufacturers.",
            ),
            (
                "https://fedex.com/case-studies/partner",
                "official",
                "We offer our solutions for retailers.",
            ),
        ],
    )
    buyers, offers, _ = sales.archived(report, pages)
    assert buyers == {"business and residential shippers": {issuer}}
    assert {row["name"] for row in offers} == {
        "Express package shipping",
        "Global freight forwarding",
        "Customs brokerage",
    }
    assert field_report.company_overview(report, pages, "FedEx")["summary"] == (
        "FedEx provides transportation, e-commerce, and business services."
    )


def test_sales_brief_separates_dated_trigger_from_discovery_question():
    path = "https://www.maersk.com/news/articles/2026/01/09/financial-system-modernisation"
    report, pages = archive(
        "https://www.maersk.com/",
        "maersk.com",
        [
            (
                path,
                "news",
                "We inform customers that we are transitioning to SAP S/4HANA in February.",
            ),
            (
                "https://maersk.com/news/2020/01/01/older",
                "news",
                "We are transitioning to SAP S/4HANA in February 2020.",
            ),
            (
                "https://maersk.com/marketplace/partner",
                "official",
                "We offer a warehouse solution built for retailers.",
            ),
        ],
    )
    buyers, offers, signals = sales.archived(report, pages)
    assert not buyers and not offers
    display = sales.briefing({}, [], signals)
    refs = [field_report.evidence_id(path)]
    assert display["signals"] == [
        {
            "text": "SAP S/4HANA transition announced",
            "date": "2026-01-09",
            "kind": "company publication",
            "evidence_ids": refs,
        }
    ]
    assert display["questions"][0]["text"].startswith("Has the announced transition completed")
    assert display["questions"][0]["evidence_ids"] == refs

"""Company rows and revisioned publications for one GTM catalog source."""

from __future__ import annotations

import json
from typing import Any

DOCUMENT = "company-context.md"
BATCH_SIZE = 20


def path(company_id: str) -> str:
    return f"{company_id}/{DOCUMENT}"


def register(state: Any, source: str, company: str, settings: dict, interval: int | None) -> bool:
    payload = json.dumps(settings, sort_keys=True, separators=(",", ":"))
    return bool(
        state.execute_returning(
            "INSERT INTO gtm_catalog_companies(source_id,company_id,settings_json,refresh_seconds) "
            "VALUES(?,?,?,COALESCE(?,86400)) ON CONFLICT(source_id,company_id) DO UPDATE SET "
            "settings_json=excluded.settings_json, checked_at=NULL, "
            "refresh_seconds=COALESCE(?,gtm_catalog_companies.refresh_seconds) WHERE "
            "gtm_catalog_companies.settings_json<>excluded.settings_json OR "
            "gtm_catalog_companies.refresh_seconds<>"
            "COALESCE(?,gtm_catalog_companies.refresh_seconds) "
            "RETURNING company_id",
            (source, company, payload, interval, interval, interval),
        )
    )


def due(state: Any, source: str, now: float, *, refresh_all: bool = False) -> list[dict]:
    return state.rows(
        "SELECT company_id,settings_json,state_json,requested_at "
        "FROM gtm_catalog_companies WHERE source_id=? "
        "AND (? OR checked_at IS NULL OR checked_at+refresh_seconds<=? OR "
        "(revision IS NOT NULL AND NOT jsonb_exists(state_json::jsonb,'failed') AND "
        "NOT jsonb_exists(state_json::jsonb->'snapshot','web_pages'))) "
        "ORDER BY requested_at NULLS LAST,checked_at NULLS FIRST,company_id LIMIT ?",
        (source, refresh_all, now, BATCH_SIZE),
    )


def outdated_reports(state: Any, source: str, version: int) -> list[dict]:
    return state.rows(
        "SELECT c.company_id,c.settings_json,c.state_json,c.requested_at "
        "FROM gtm_catalog_companies c JOIN gtm_catalog_publications p "
        "ON (p.source_id,p.company_id,p.revision)=(c.source_id,c.company_id,c.revision) "
        "WHERE c.source_id=? AND c.checked_at IS NOT NULL AND c.requested_at IS NULL "
        "AND NOT jsonb_exists(c.state_json::jsonb, 'pending') "
        "AND NOT jsonb_exists(c.state_json::jsonb, 'failed') "
        "AND jsonb_exists(c.state_json::jsonb->'snapshot','web_pages') "
        "AND p.evidence_json::jsonb->>'field_report_version' IS DISTINCT FROM CAST(? AS text) "
        "ORDER BY c.company_id LIMIT ?",
        (source, version, BATCH_SIZE),
    )


def needs_publication(
    state: Any, source: str, company: str, *, indexed_revision: str | None = None
) -> bool:
    rows = state.rows(
        "SELECT checked_at IS NULL OR revision IS NULL OR "
        "(CAST(? AS text) IS NOT NULL AND revision IS DISTINCT FROM CAST(? AS text)) OR "
        "jsonb_exists(state_json::jsonb, 'pending') OR jsonb_exists(state_json::jsonb, 'failed') "
        "AS pending FROM gtm_catalog_companies WHERE source_id=? AND company_id=?",
        (indexed_revision, indexed_revision, source, company),
    )
    return not rows or rows[0]["pending"]


def checkpoint(state: Any, source: str, row: dict, record: dict) -> None:
    state.execute(
        "UPDATE gtm_catalog_companies SET state_json=? WHERE source_id=? AND company_id=? "
        "AND settings_json=?",
        (
            json.dumps(record, separators=(",", ":")),
            source,
            row["company_id"],
            row["settings_json"],
        ),
    )


def checked(state: Any, source: str, row: dict, now: float, *, failed: bool = False) -> None:
    state.execute(
        "UPDATE gtm_catalog_companies SET checked_at="
        "?-CASE WHEN ? THEN refresh_seconds-900 ELSE 0 END,requested_at=NULL "
        "WHERE source_id=? AND company_id=? AND settings_json=? "
        "AND requested_at IS NOT DISTINCT FROM CAST(? AS double precision)",
        (now, failed, source, row["company_id"], row["settings_json"], row["requested_at"]),
    )


def request_refresh(state: Any, source: str, company: str, *, deep: bool = False) -> bool:
    settings = ""
    if deep:
        target = "(settings_json::jsonb->'target')"
        settings = (
            f"settings_json=CASE WHEN {target}->>'depth'='deep' THEN settings_json ELSE "
            f"jsonb_set(settings_json::jsonb,'{{target}}',{target} || "
            f"jsonb_build_object('depth','deep','page_limit',"
            f"GREATEST(CAST({target}->>'page_limit' AS integer),80),'job_limit',"
            f"CASE WHEN {target}->>'job_limit'='0' THEN 0 "
            f"ELSE GREATEST(CAST({target}->>'job_limit' AS integer),36) END))::text END, "
        )
    return bool(
        state.execute_returning(
            "UPDATE gtm_catalog_companies SET checked_at=NULL, "
            + settings
            + "requested_at=EXTRACT(EPOCH FROM clock_timestamp()) "
            "WHERE source_id=? AND company_id=? RETURNING company_id",
            (source, company),
        )
    )


def prepare(
    state: Any,
    source: str,
    row: dict,
    revision: str,
    profile: dict,
    markdown: str,
    evidence: dict,
) -> None:
    company = row["company_id"]
    # The old published revision is still readable until the ordinary writer
    # commits the replacement artifact. Keep both through a crash or cancel.
    state.execute(
        "INSERT INTO gtm_catalog_publications VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING",
        (
            source,
            company,
            revision,
            json.dumps(profile, separators=(",", ":")),
            markdown,
            json.dumps(evidence, separators=(",", ":")),
        ),
    )
    state.execute(
        "UPDATE gtm_catalog_companies SET revision=? WHERE source_id=? AND company_id=? "
        "AND settings_json=?",
        (revision, source, company, row["settings_json"]),
    )
    state.execute(
        "DELETE FROM gtm_catalog_publications p WHERE p.source_id=? AND p.company_id=? "
        "AND p.revision<>? AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.source_id=p.source_id "
        "AND a.relative_path=? AND a.sha256=p.revision) AND NOT EXISTS "
        "(SELECT 1 FROM gtm_catalog_companies c WHERE c.source_id=p.source_id AND "
        "c.company_id=p.company_id AND c.revision=p.revision)",
        (source, company, revision, path(company)),
    )


def listings(state: Any, source: str) -> list[dict]:
    return state.rows(
        "SELECT company_id,revision FROM gtm_catalog_companies WHERE source_id=? "
        "AND revision IS NOT NULL ORDER BY company_id",
        (source,),
    )


def publication(state: Any, source: str, company: str, revision: str) -> dict:
    rows = state.rows(
        "SELECT profile_json,markdown,evidence_json FROM gtm_catalog_publications "
        "WHERE source_id=? AND company_id=? AND revision=?",
        (source, company, revision),
    )
    return dict(rows[0]) if rows else {}


def page_inventory(state: Any, source: str) -> dict[str, dict]:
    rows = state.rows(
        "SELECT company_id, (SELECT jsonb_object_agg(key, value - 'text') FROM "
        "jsonb_each(state_json::jsonb#>'{snapshot,web_pages}')) AS pages "
        "FROM gtm_catalog_companies WHERE source_id=? AND revision IS NOT NULL",
        (source,),
    )
    return {row["company_id"]: row["pages"] or {} for row in rows}


def page(state: Any, source: str, company: str, reference: str) -> dict:
    rows = state.rows(
        "SELECT jsonb_extract_path(state_json::jsonb, 'snapshot', 'web_pages', ?) AS page "
        "FROM gtm_catalog_companies WHERE source_id=? AND company_id=?",
        (reference, source, company),
    )
    return (rows[0]["page"] or {}) if rows else {}

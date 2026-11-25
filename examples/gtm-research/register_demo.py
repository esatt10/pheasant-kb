"""Register, sync and export a GTM catalog through Pheasant's HTTP API."""

from __future__ import annotations

import argparse
import json
import os
import time
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen


def write_export(path: Path, payload: dict) -> None:
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(payload, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def verified_profile(api, source: str, company_id: str) -> dict | None:
    prefix = f"/gtm/catalogs/{quote(source, safe='')}/companies/{quote(company_id, safe='')}"
    try:
        profile = api(prefix)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    revision = sha256(profile["markdown"].encode()).hexdigest()
    if (
        revision != profile["revision"]
        or profile["path"] != f"{company_id}/company-context.md"
        or profile["source_name"] != source
        or not profile["artifact_id"]
    ):
        raise RuntimeError(f"Indexed publication did not verify for {company_id}")
    if profile["publication_pending"]:
        return None
    return profile


def wait_for_index(api, source: str, since: str | None, deadline: float) -> dict:
    last_state = None
    while time.monotonic() < deadline:
        query = urlencode(
            {
                "timeout_seconds": min(25, max(0, deadline - time.monotonic())),
                **({"since": since} if since else {}),
            }
        )
        result = api(f"/sources/{quote(source, safe='')}/wait?{query}")
        state = result["state"]
        if state != last_state:
            print(f"Index {state}", flush=True)
            last_state = state
        if state == "finished":
            outcome = result.get("last_sync") or {}
            if outcome.get("status") not in {"healthy", "degraded"}:
                raise RuntimeError(f"Indexing ended with status {outcome.get('status')}")
            return result
        if state == "not_started":
            raise RuntimeError("The sync was not claimed by an indexer")
    raise TimeoutError(f"Timed out waiting for {source} to be indexed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument(
        "--manifest", type=Path, default=Path(__file__).with_name("demo-companies.json")
    )
    parser.add_argument(
        "--register-only", action="store_true", help="Register companies without starting a sync"
    )
    parser.add_argument(
        "--no-wait", action="store_true", help="Queue the sync and return immediately"
    )
    parser.add_argument(
        "--timeout", type=int, default=1800, help="Overall indexing timeout in seconds"
    )
    parser.add_argument(
        "--export", type=Path, help="Write the verified curated catalog bundle as JSON"
    )
    args = parser.parse_args()
    if args.export and (args.register_only or args.no_wait):
        parser.error("--export requires waiting for the indexed catalog")
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    base = args.base_url.rstrip("/")
    endpoint = urlsplit(base)
    if endpoint.scheme != "https" and not (
        endpoint.scheme == "http" and endpoint.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        parser.error("use HTTPS or a loopback HTTP URL")
    token = os.environ.get("PHEASANT_API_TOKEN")
    if not token:
        parser.error("set PHEASANT_API_TOKEN for the target Pheasant instance")

    def api(path: str, payload: dict | None = None) -> dict:
        request = Request(
            base + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json"},
        )
        request.add_unredirected_header("Authorization", f"Bearer {token}")
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    source = manifest["source"]
    company_ids = [company["id"] for company in manifest["companies"]]
    if not company_ids or len(set(company_ids)) != len(company_ids):
        parser.error("provide a non-empty list of unique company IDs")
    for company in manifest["companies"]:
        defaults = manifest.get("defaults", {})
        overrides = company.get("target", {})
        result = api(
            "/gtm/collections",
            {
                "name": source,
                "company_id": company["id"],
                "segment": company.get("segment"),
                "input_source": company.get("input_source", manifest.get("input_source")),
                "target": {
                    **defaults,
                    "company": company.get("name"),
                    "website": company.get("website"),
                    "naics_code": company.get("naics_code"),
                    "urls": company.get("urls", []),
                    **overrides,
                    "acquisition": {
                        **defaults.get("acquisition", {}),
                        **{
                            key: company[key]
                            for key in ("job_board_urls", "identity_fallback_urls")
                            if key in company
                        },
                        **overrides.get("acquisition", {}),
                    },
                },
                "sync_now": False,
            },
        )
        print(f"{result['status']:>10}  {company['id']}", flush=True)

    print(f"{base}/gtm?{urlencode({'catalog': source})}", flush=True)
    if args.register_only:
        return

    endpoint = f"/gtm/collections/{quote(source, safe='')}"
    deadline = time.monotonic() + args.timeout
    previous_attempts: set[str] = set()
    while True:
        since = api(endpoint)["last_indexed_at"]
        print(json.dumps(api(endpoint + "/sync", {})["sync"]), flush=True)
        if args.no_wait:
            return
        completion = wait_for_index(api, source, since, deadline)
        outcome = completion["last_sync"].get("gtm_catalog") or {}
        failed = set(outcome.get("failed_company_ids", [])) & set(company_ids)
        if failed:
            raise RuntimeError(f"Research failed for {', '.join(sorted(failed))}")
        attempted = set(outcome.get("attempted_company_ids", []))
        published = {key: verified_profile(api, source, key) for key in company_ids}
        pending = {key for key, value in published.items() if value is None}
        if not pending:
            break
        if not attempted - previous_attempts:
            raise RuntimeError(f"Indexing did not publish {', '.join(sorted(pending))}")
        previous_attempts.update(attempted)

    if args.export:
        write_export(
            args.export,
            {
                "schema": "pheasant.gtm.context.v1",
                "source_name": source,
                "technology_taxonomy": api("/gtm/technology-taxonomy"),
                "companies": [published[key] for key in sorted(company_ids)],
            },
        )
        print(f"Exported {len(published)} verified company revisions to {args.export}", flush=True)
    else:
        print(f"Verified {len(published)} indexed company revisions", flush=True)


if __name__ == "__main__":
    main()

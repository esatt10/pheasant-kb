"""Measure durable acknowledgment, visibility and physical cleanup identically on fleets.

Run inside an API container so its token and database URL stay in the process:
python /app/scripts/benchmark_mutations.py --base http://127.0.0.1:8765 --samples 5
Only the uniquely-named probe artifacts and source created here are removed.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument(
        "--wait-for-projection",
        action="store_true",
        help="Measure outbox completion and positive text retrieval before deleting",
    )
    args = parser.parse_args()
    token = os.environ.get("PHEASANT_API_TOKEN")
    if not token:
        token_file = os.environ.get("PHEASANT_API_TOKEN_FILE")
        if token_file:
            from pathlib import Path

            token = Path(token_file).read_text().strip()
    if not token:
        raise RuntimeError("API token is unavailable")

    import psycopg

    def connect():
        return psycopg.connect(
            os.environ["PHEASANT_DATABASE_URL"],
            autocommit=True,
            options="-c default_transaction_read_only=on -c statement_timeout=5000",
        )

    database = connect()

    def physical(sql: str, params: tuple) -> int:
        nonlocal database
        try:
            return database.execute(sql, params).fetchone()[0]
        except psycopg.OperationalError:
            # Cold indexing can outlast a network idle timeout. This probe
            # retries only a read, never an application mutation.
            database.close()
            database = connect()
            return database.execute(sql, params).fetchone()[0]

    prefix = "latency-probe-" + uuid.uuid4().hex[:10]

    def call(
        method: str,
        path: str,
        payload=None,
        raw: bytes | None = None,
        content_type: str = "application/json",
    ):
        data = (
            raw
            if raw is not None
            else json.dumps(payload).encode()
            if payload is not None
            else None
        )
        req = Request(
            args.base.rstrip("/") + path,
            data=data,
            method=method,
            headers={"Authorization": "Bearer " + token, "Content-Type": content_type},
        )
        try:
            with urlopen(req, timeout=args.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise RuntimeError(f"{method} {path}: {exc.code} {exc.read()[:500]!r}") from exc

    def wait(predicate):
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        raise TimeoutError("probe did not reach its visibility/cleanup barrier")

    rows = []
    artifact_ids = []
    source_created = False
    try:
        for sample in range(args.samples):
            started = time.monotonic()
            saved = call(
                "POST",
                "/artifacts",
                {
                    "title": f"{prefix}-{sample}",
                    "files": [
                        {"name": "probe.md", "content": f"# Probe\n\n{prefix} sample {sample}."}
                    ],
                },
            )
            acknowledged = time.monotonic()
            artifact_ids.append(saved["id"])
            wait(
                lambda saved=saved: (
                    call("GET", "/artifacts/" + saved["id"]).get("index_status") == "indexed"
                )
            )
            indexed = time.monotonic()
            folder = saved["folder"]
            completion = {}
            if args.wait_for_projection:
                wait(
                    lambda folder=folder: (
                        physical("SELECT COUNT(*) FROM artifact_outbox WHERE folder=%s", (folder,))
                        == 0
                    )
                )
                completion["projection_complete_s"] = time.monotonic() - started
                wait(
                    lambda folder=folder: any(
                        folder in str(hit)
                        for hit in call(
                            "POST", "/search", {"query": prefix, "mode": "text", "max_results": 100}
                        ).get("results", [])
                    )
                )
                completion["verified_searchable_s"] = time.monotonic() - started
            deletion_started = time.monotonic()
            call("DELETE", "/artifacts/" + saved["id"])
            deletion_ack = time.monotonic()
            assert call("GET", "/artifacts/" + saved["id"]) is None
            hits = call("POST", "/search", {"query": prefix, "mode": "text", "max_results": 100})
            assert not any(folder in str(hit) for hit in hits.get("results", []))
            excluded = time.monotonic()
            wait(
                lambda folder=folder: (
                    physical(
                        "SELECT COUNT(*) FROM artifacts WHERE source_id=%s "
                        "AND relative_path LIKE %s",
                        ("artifacts", folder + "/%"),
                    )
                    == 0
                )
            )
            cleaned = time.monotonic()
            artifact_ids.remove(saved["id"])
            row = {
                "operation": "artifact",
                "sample": sample,
                "save_ack_s": acknowledged - started,
                "searchable_s": indexed - started,
                "delete_ack_s": deletion_ack - deletion_started,
                "excluded_verified_s": excluded - deletion_started,
                "cleanup_s": cleaned - deletion_started,
                **completion,
            }
            rows.append(row)
            print(json.dumps(row), flush=True)

        boundary = "pheasant-probe-" + uuid.uuid4().hex
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="source_name"\r\n\r\n'
            f'{prefix}\r\n--{boundary}\r\nContent-Disposition: form-data; name="files"; '
            f'filename="probe.md"\r\nContent-Type: text/markdown\r\n\r\n'
            f"# Source probe\n\n{prefix}\r\n--{boundary}--\r\n"
        ).encode()
        call(
            "POST",
            "/sources/upload",
            raw=body,
            content_type="multipart/form-data; boundary=" + boundary,
        )
        source_created = True
        wait(lambda: physical("SELECT COUNT(*) FROM artifacts WHERE source_id=%s", (prefix,)) > 0)
        started = time.monotonic()
        call("DELETE", "/sources/" + prefix)
        acknowledged = time.monotonic()
        wait(lambda: physical("SELECT COUNT(*) FROM sources WHERE id=%s", (prefix,)) == 0)
        row = {
            "operation": "source",
            "delete_ack_s": acknowledged - started,
            "cleanup_s": time.monotonic() - started,
        }
        rows.append(row)
        source_created = False
        print(json.dumps(row), flush=True)
        print(json.dumps({"probe": prefix, "base": args.base, "samples": rows}), flush=True)
    finally:
        for artifact in artifact_ids:
            call("DELETE", "/artifacts/" + artifact)
        if source_created:
            call("DELETE", "/sources/" + prefix)
        database.close()


if __name__ == "__main__":
    main()

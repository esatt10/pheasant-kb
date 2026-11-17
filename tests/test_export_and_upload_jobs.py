"""Exports and uploads as background jobs: idempotent, watchable, never blocking.

What must hold:

1. **An export is a job.** It starts at once, runs in the background, is
   followed with ``GET /jobs/{id}/wait`` / MCP ``wait_for_job`` (or streamed
   from ``/jobs/{id}/events``), and its file is fetched with
   ``GET /jobs/{id}/download``.
2. **Idempotent.** The same export asked again while it runs returns that job;
   asked again once finished over unchanged content, the finished job and its
   file; over either surface. A changed document is a new export.
3. **An upload is a job too** (``background=true``): it answers once the files
   are received, the same drop while it lands returns the same job, and a file
   whose name already holds those bytes is not stored twice.
"""

from __future__ import annotations

import io
import json
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.ingestion.landing import store_upload
from pheasant.mcp_server.tools import PheasantTools
from pheasant.services import export_jobs, source_export


@pytest.fixture
def region(tmp_path: Path) -> dict[str, Any]:
    workspace = tmp_path / "workspace"
    (workspace / "records").mkdir(parents=True)
    (workspace / "README.md").write_text("# Readme\n\nWelcome.\n", encoding="utf-8")
    (workspace / "records" / "a.md").write_text("# A\n\nFirst record.\n", encoding="utf-8")
    (workspace / "records" / "b.md").write_text("# B\n\nSecond record.\n", encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "jobs",
                "defaults": "standalone",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "assistant": {"provider": "none"},
            "artifacts": {"index_on_save": False},
            "sources": [
                {
                    "name": "reference",
                    "type": "document_folder",
                    "path": str(workspace),
                    "include": ["**/*"],
                }
            ],
        }
    )
    tools = PheasantTools(config)
    tools.engine.sync_source("reference", "full")
    app = create_app(config, config_path=str(tmp_path / "pheasant.yaml"))
    with TestClient(app) as client:
        yield {
            "client": client,
            "tools": tools,
            "kb": config.knowledge_base_id,
            "workspace": workspace,
            "root": tmp_path,
        }
    tools.engine.close()
    app.state.engine.close()


def _wait(client: TestClient, job_id: str) -> dict[str, Any]:
    for _ in range(60):
        job = client.get(f"/jobs/{job_id}/wait", params={"timeout_seconds": 5}).json()
        if not job["timed_out"]:
            return job
    raise AssertionError(f"job {job_id} did not finish")


def test_an_export_is_a_job_followed_and_downloaded(region: dict[str, Any]) -> None:
    client = region["client"]
    started = client.post("/sources/reference/exports", json={"format": "zip"})
    assert started.status_code == 202, started.text
    job = started.json()
    assert job["kind"] == "export" and job["reused"] is None and job["job_id"] == job["id"]

    done = _wait(client, job["id"])
    assert done["status"] == "succeeded", done
    result = done["result"]
    assert result["documents"] == result["total"] == 3
    assert result["files"][0]["filename"] == "reference.zip"
    assert result["download"] == f"/jobs/{job['id']}/download"

    download = client.get(result["download"])
    assert download.status_code == 200
    names = set(zipfile.ZipFile(io.BytesIO(download.content)).namelist())
    assert {
        "files/README.md",
        "files/records/a.md",
        "files/records/b.md",
        "manifest.jsonl",
    } <= names
    # The job shows where every surface lists background work.
    assert any(j["id"] == job["id"] for j in client.get("/jobs").json()["jobs"])

    # Asked again over unchanged content: the finished job and its file.
    again = client.post("/sources/reference/exports", json={"format": "zip"}).json()
    assert again["id"] == job["id"] and again["reused"] == "ready"
    # A folder is a different export.
    folder = client.post("/sources/reference/exports", json={"path_prefix": "records"}).json()
    assert folder["id"] != job["id"]
    assert _wait(client, folder["id"])["result"]["files"][0]["filename"] == "reference-records.zip"


def test_asking_again_while_it_runs_returns_the_same_job_on_both_surfaces(
    region: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, tools = region["client"], region["tools"]
    release = threading.Event()
    real = source_export.export_source_file

    def slow(*args: Any, **kwargs: Any) -> dict[str, Any]:
        release.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr(source_export, "export_source_file", slow)
    first = client.post("/sources/reference/exports", json={"format": "jsonl"}).json()
    try:
        second = client.post("/sources/reference/exports", json={"format": "jsonl"}).json()
        assert second["id"] == first["id"] and second["reused"] == "running"
        over_mcp = tools.start_source_export(region["kb"], "reference", "jsonl")
        assert over_mcp["id"] == first["id"] and over_mcp["reused"] == "running"
        # Not ready yet: the download says so, and how to wait.
        early = client.get(f"/jobs/{first['id']}/download")
        assert early.status_code == 409 and "EXPORT_NOT_READY" in early.text
        waited = tools.wait_for_job(first["id"], timeout_seconds=0.2)
        assert waited["timed_out"] is True and waited["active"] is True
    finally:
        release.set()
    finished = tools.wait_for_job(first["id"], timeout_seconds=30)
    assert finished["timed_out"] is False and finished["status"] == "succeeded"
    assert finished["kind"] == "export"


def test_a_changed_document_is_a_new_export(region: dict[str, Any]) -> None:
    client = region["client"]
    first = client.post("/sources/reference/exports", json={"format": "jsonl"}).json()
    _wait(client, first["id"])
    (region["workspace"] / "records" / "a.md").write_text("# A\n\nChanged.\n", encoding="utf-8")
    region["tools"].engine.sync_source("reference", "incremental")
    second = client.post("/sources/reference/exports", json={"format": "jsonl"}).json()
    assert second["id"] != first["id"] and second["reused"] is None
    body = client.get(_wait(client, second["id"])["result"]["download"]).text
    assert "Changed." in body


def test_refusals_name_the_problem(region: dict[str, Any]) -> None:
    client = region["client"]
    unknown = client.get("/jobs/nope/download")
    assert unknown.status_code == 404 and "UNKNOWN_JOB" in unknown.text
    assert client.get("/jobs/nope/wait").status_code == 404
    refused = client.post("/sources/reference/exports", json={"path_prefix": "nowhere"})
    assert refused.status_code == 422 and "No document under 'nowhere'" in refused.text
    job = client.post("/sources/reference/exports", json={"format": "jsonl"}).json()
    done = _wait(client, job["id"])
    folder = (
        Path(region["root"]) / "exports" / export_jobs.SUBDIR / region["kb"] / done["result"]["key"]
    )
    for file in folder.glob("*.jsonl"):
        file.unlink()
    gone = client.get(done["result"]["download"])
    assert gone.status_code == 410 and "EXPORT_EXPIRED" in gone.text
    # And asking again rebuilds it rather than handing back a job with no file.
    rebuilt = client.post("/sources/reference/exports", json={"format": "jsonl"}).json()
    assert rebuilt["reused"] is None and _wait(client, rebuilt["id"])["status"] == "succeeded"


def test_one_job_streams_until_it_finishes(region: dict[str, Any]) -> None:
    client = region["client"]
    job = client.post("/sources/reference/exports", json={"format": "jsonl"}).json()
    events = []
    with client.stream("GET", f"/jobs/{job['id']}/events") as stream:
        for line in stream.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:])["job"])
    assert events and events[-1]["status"] == "succeeded" and not events[-1]["active"]


def test_storing_the_same_bytes_under_the_same_name_is_idempotent(tmp_path: Path) -> None:
    first = store_upload(tmp_path, "notes.md", b"# Notes\n")
    again = store_upload(tmp_path, "notes.md", b"# Notes\n")
    assert (first.unchanged, again.unchanged) == (False, True)
    assert again.path == first.path
    different = store_upload(tmp_path, "notes.md", b"# Other\n")
    assert different.filename == "notes-2.md" and not different.unchanged
    # A copy that already holds these bytes is found too.
    assert store_upload(tmp_path, "notes.md", b"# Other\n").path == different.path
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes-2.md", "notes.md"]


def test_an_upload_is_a_job_and_a_repeat_stores_nothing_twice(
    region: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = region["client"]
    files = [("files", ("one.md", b"# One\n\nUploaded.\n")), ("files", ("two.md", b"# Two\n"))]
    form = {"source_name": "drop", "background": "true"}

    from pheasant.ingestion import landing_service

    release = threading.Event()
    real_write = landing_service.LocalLandingZone.write

    def slow_write(self: Any, *args: Any, **kwargs: Any) -> Any:
        release.wait(30)
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(landing_service.LocalLandingZone, "write", slow_write)
    accepted = client.post("/sources/upload", files=files, data=form)
    assert accepted.status_code == 200, accepted.text
    job = accepted.json()
    assert (
        job["status"] == "accepted"
        and job["kind"] == "upload"
        and job["files"] == ["one.md", "two.md"]
    )
    try:
        repeat = client.post("/sources/upload", files=files, data=form).json()
        assert repeat["job_id"] == job["job_id"] and repeat["reused"] == "running"
    finally:
        release.set()
    done = _wait(client, job["job_id"])
    assert done["status"] == "succeeded", done
    assert [item["filename"] for item in done["result"]["stored"]] == ["one.md", "two.md"]

    # Landed and finished: the same files again store nothing new.
    later = _wait(client, client.post("/sources/upload", files=files, data=form).json()["job_id"])
    assert later["result"]["status"] == "unchanged"
    assert [item["filename"] for item in later["result"]["unchanged"]] == ["one.md", "two.md"]
    landed = Path(region["root"]) / "state" / "uploads" / "drop"
    assert sorted(p.name for p in landed.iterdir()) == ["one.md", "two.md"]

    # Without background the reply is the synchronous one it always was.
    sync = client.post(
        "/sources/upload",
        files=[("files", ("three.md", b"# Three\n"))],
        data={"source_name": "drop"},
    ).json()
    assert sync["status"] == "stored" and sync["stored"][0]["filename"] == "three.md"
    time.sleep(0)

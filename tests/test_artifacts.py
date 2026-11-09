"""Kept artifacts: `services.artifacts` and the one renderer every surface uses.

A composed SuiteScript lived only in the tab that asked for it: a refresh
took it away. Every composed answer is kept now, any answer or content can be
saved as a report, and the HTTP routes, the MCP tools and the downloads show
it through one renderer (`services/artifact_render.py`) in pheasant's style.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.mcp_server.tools import PheasantTools
from pheasant.services import ServiceContext, artifact_render, artifacts
from pheasant.services import assistant as assistant_service
from pheasant.services.errors import ArtifactNotFound

ANSWER = (
    "A scheduled script counts new and closed accounts over the last year [1].\n\n"
    "### `account_counts.js`\n\n```javascript\n/** @NScriptType ScheduledScript */\n"
    "define(['N/search'], (search) => ({ execute() { return 1; } }));\n```\n\n"
    "### `report.html`\n\n```html\n<html><head><title>r</title></head>"
    "<body><script>document.body.dataset.ran = 'yes'</script>Counts</body></html>\n```\n\n"
    "- The passages do not say which subsidiary to count; fill in SUBSIDIARY."
)
CITATIONS = [
    {
        "index": 1,
        "source_id": "netsuite-docs",
        "relative_path": "section_1.html",
        "heading_path": "N/search",
    },
    {"index": 2, "source_id": "netsuite-docs", "relative_path": "unused.html"},
]
DIAGRAM = {
    "type": "diagram",
    "status": "ok",
    "diagram": {"kind": "flow", "title": "Schedule", "nodes": [], "edges": []},
}


def _composed(question: str) -> dict[str, Any]:
    return {
        "question": question,
        "answer": ANSWER,
        "answer_mode": "llm",
        "route": {"intent": "compose"},
        "citations": CITATIONS,
        "steps": [],
        "visual": {"status": "none"},
    }


@pytest.fixture()
def composing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The answering workflow returns a composed answer, as a model would write it."""

    from pheasant.assistant import chat

    monkeypatch.setattr(chat, "answer_question", lambda question, **_k: _composed(question))


def test_a_composed_answer_is_kept_and_both_surfaces_read_the_same_artifact(
    loaded_config, composing: None
) -> None:
    app = create_app(config=loaded_config)
    client = TestClient(app)
    answer = client.post(
        "/assistant/chat", json={"question": "@compose SuiteScript for account counts"}
    ).json()
    kept = answer["artifact"]
    assert kept["kind"] == "files" and kept["title"] == "SuiteScript for account counts"
    assert [f["name"] for f in kept["files"]] == ["account_counts.js", "report.html"]
    assert [f["previewable"] for f in kept["files"]] == [False, True]

    over_http = client.get(f"/artifacts/{kept['id']}").json()
    tools = PheasantTools(loaded_config)
    tools.services = _context(app)
    over_mcp = tools.get_artifact(loaded_config.knowledge_base_id, kept["id"])
    assert over_http == over_mcp, "the two surfaces disagree about one artifact"
    payload = over_http["payload"]
    assert "fill in SUBSIDIARY" in payload["not_covered"]
    assert [c["index"] for c in payload["citations"]] == [1], "only the cited passage is kept"
    listed = client.get("/artifacts").json()["artifacts"]
    assert [a["id"] for a in listed] == [kept["id"]] and "payload" not in listed[0]


def test_an_artifact_outlives_the_process_that_kept_it(loaded_config, composing: None) -> None:
    """The point of keeping it: a refresh, a restart, another replica."""

    first = TestClient(create_app(config=loaded_config))
    kept = first.post("/assistant/chat", json={"question": "@compose a script"}).json()["artifact"]
    later = TestClient(create_app(config=loaded_config))
    assert later.get(f"/artifacts/{kept['id']}").json()["title"] == "a script"


def test_the_rendered_artifact_is_pheasant_styled_and_cannot_reach_out(
    loaded_config, composing: None
) -> None:
    client = TestClient(create_app(config=loaded_config))
    kept = client.post("/assistant/chat", json={"question": "@compose counts"}).json()["artifact"]

    page = client.get(f"/artifacts/{kept['id']}/view")
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["content-security-policy"].startswith("sandbox allow-scripts;")
    document = page.text
    # The policy is the first thing the document says, before anything it holds.
    assert document.index("Content-Security-Policy") < document.index("<style>")
    assert "default-src 'none'" in document and "connect-src" not in document
    assert "--accent:#5f6b45" in document, "not in pheasant's palette"
    assert '<pre class="hl"><code>' in document and 'class="k"' in document  # highlighted
    assert 'href="#source-1"' in document and 'id="source-1"' in document  # [1] is linked
    assert "unused.html" not in document

    code = client.get(f"/artifacts/{kept['id']}/view", params={"file": "report.html"}).text
    assert "<script" not in code and ">dataset<" in code, "a file's source is shown, never run"
    preview = client.get(
        f"/artifacts/{kept['id']}/view", params={"file": "report.html", "mode": "preview"}
    ).text
    # The file runs as its author wrote it, but under the policy, placed first.
    assert preview.index("Content-Security-Policy") < preview.index("<script>")
    assert "document.body.dataset.ran" in preview


def test_markdown_from_a_caller_is_text_and_an_svg_is_a_picture(loaded_config) -> None:
    app = create_app(config=loaded_config)
    client = TestClient(app)
    saved = client.post(
        "/artifacts",
        json={
            "title": "Notes",
            "markdown": "# Notes\n\n<script>alert(1)</script> and **bold**",
            "files": [
                {
                    "name": "../../etc/chart.svg",
                    "content": "<svg xmlns='http://www.w3.org/2000/svg'>"
                    "<script>alert(1)</script></svg>",
                },
            ],
        },
    ).json()
    assert saved["kind"] == "report"
    assert [f["name"] for f in saved["files"]] == ["etc/chart.svg"], "a path cannot climb out"
    document = client.get(f"/artifacts/{saved['id']}/view").text
    assert (
        "&lt;script&gt;alert(1)&lt;/script&gt;" in document and "<strong>bold</strong>" in document
    )
    svg = client.get(
        f"/artifacts/{saved['id']}/view", params={"file": "etc/chart.svg", "mode": "preview"}
    ).text
    assert 'src="data:image/svg+xml;base64,' in svg and "<script>alert" not in svg


def test_an_artifact_downloads_as_a_zip_markdown_html_or_one_file(
    loaded_config, composing: None
) -> None:
    client = TestClient(create_app(config=loaded_config))
    kept = client.post("/assistant/chat", json={"question": "@compose counts"}).json()["artifact"]

    archive = client.get(f"/artifacts/{kept['id']}/export", params={"format": "zip"})
    assert archive.headers["content-disposition"] == 'attachment; filename="counts.zip"'
    names = zipfile.ZipFile(io.BytesIO(archive.content)).namelist()
    assert names == ["account_counts.js", "report.html", "README.md", "counts.html"]
    markdown = client.get(f"/artifacts/{kept['id']}/export", params={"format": "markdown"}).text
    assert "## `account_counts.js`" in markdown and "## Sources" in markdown
    one = client.get(f"/artifacts/{kept['id']}/export", params={"file": "account_counts.js"})
    assert one.text.startswith("/** @NScriptType ScheduledScript */")
    assert one.headers["content-disposition"] == 'attachment; filename="account_counts.js"'


def test_an_answer_the_stream_kept_is_saved_as_a_report_with_its_diagram(
    loaded_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = {
        "question": "How are accounts closed?",
        "answer": "Accounts close when inactive [1].",
        "citations": CITATIONS[:1],
        "steps": [],
        "visual": DIAGRAM,
    }
    monkeypatch.setattr(assistant_service, "answer", lambda *_a, **_k: dict(answer))
    client = TestClient(create_app(config=loaded_config))
    body = {"question": answer["question"], "answer_id": "kept-for-report-01"}
    with client.stream("POST", "/assistant/chat/stream", json=body) as response:
        list(response.iter_lines())

    report = client.post("/artifacts", json={"answer_id": "kept-for-report-01"}).json()
    assert report["kind"] == "report" and report["title"] == "How are accounts closed?"
    assert report["has_visual"] is True
    document = client.get(f"/artifacts/{report['id']}/view").text
    # The diagram is drawn by pheasant's own view, hosted inside the report.
    frame = re.search(
        r'<iframe id="visual"[^>]*sandbox="allow-scripts"[^>]*srcdoc="([^"]*)"', document
    )
    assert frame and "pheasant knowledge view" in frame.group(1)
    data = re.search(r'<script type="application/json" id="visual-data">(.*?)</script>', document)
    assert json.loads(data.group(1))["visual"]["diagram"]["title"] == "Schedule"

    unfinished = client.post("/artifacts", json={"answer_id": "never-asked-0001"})
    assert unfinished.status_code == 404 and unfinished.json()["code"] == "UNKNOWN_ANSWER"


def test_an_artifact_is_read_back_by_its_owner_only_and_deleted_by_them(loaded_config) -> None:
    app = create_app(config=loaded_config)
    context = _context(app)
    saved = artifacts.save(
        context, artifacts.SaveRequest(title="Mine", markdown="text", principal="ada@example.com")
    )
    assert artifacts.get(context, saved["id"], "ada@example.com")["title"] == "Mine"
    with pytest.raises(ArtifactNotFound):
        artifacts.get(context, saved["id"], "grace@example.com")
    assert artifacts.list_artifacts(context, "grace@example.com")["artifacts"] == []

    client = TestClient(app)
    refused = client.delete(f"/artifacts/{saved['id']}", params={"principal": "grace@example.com"})
    assert refused.status_code == 404 and refused.json()["code"] == "UNKNOWN_ARTIFACT"
    assert (
        client.delete(f"/artifacts/{saved['id']}", params={"principal": "ada@example.com"}).json()[
            "deleted"
        ]
        == saved["id"]
    )
    with pytest.raises(ArtifactNotFound):
        artifacts.get(context, saved["id"], "ada@example.com")


def test_mcp_exports_and_views_through_the_same_renderer(loaded_config, composing: None) -> None:
    app = create_app(config=loaded_config)
    client = TestClient(app)
    kept = client.post("/assistant/chat", json={"question": "@compose counts"}).json()["artifact"]
    tools = PheasantTools(loaded_config)
    tools.services = _context(app)
    kb = loaded_config.knowledge_base_id

    viewed = tools.view_artifact(kb, kept["id"], file="account_counts.js")
    assert (
        viewed["html"]
        == client.get(f"/artifacts/{kept['id']}/view", params={"file": "account_counts.js"}).text
    )
    archive = tools.export_artifact(kb, kept["id"], format="zip")
    assert archive["filename"] == "counts.zip" and archive["base64"]
    assert tools.export_artifact(kb, kept["id"])["content"].startswith("# counts")


def test_compose_artifact_names_the_artifact_it_kept(loaded_config, composing: None) -> None:
    app = create_app(config=loaded_config)
    tools = PheasantTools(loaded_config)
    tools.services = _context(app)
    result = tools.compose_artifact(loaded_config.knowledge_base_id, "a script", wait=True)
    assert result["artifact_id"]
    assert (
        tools.get_artifact(loaded_config.knowledge_base_id, result["artifact_id"])["kind"]
        == "files"
    )


def test_the_policy_holds_in_every_document_the_renderer_makes() -> None:
    record = {
        "title": "t",
        "kind": "files",
        "created_at": "2026-11-09T00:00:00+00:00",
        "payload": {
            "files": [{"name": "a.md", "content": "# a"}, {"name": "b.py", "content": "x=1"}]
        },
    }
    documents = [
        artifact_render.overview(record),
        artifact_render.file_document(record, record["payload"]["files"][0], mode="preview"),
        artifact_render.file_document(record, record["payload"]["files"][1]),
        artifact_render.with_policy("<p>no head</p>"),
    ]
    for document in documents:
        assert artifact_render.DOCUMENT_POLICY in document


def _context(app: Any) -> ServiceContext:
    return ServiceContext(config=app.state.config, state=app.state.state, searcher=None, graph=None)


@pytest.mark.skipif(
    not __import__("os").environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip(),
    reason="set PHEASANT_TEST_POSTGRES_DSN to a throwaway database",
)
def test_both_kept_tables_round_trip_on_a_real_postgres(loaded_config) -> None:
    """`saved_artifacts` and `answer_streams` through the fleet's backend."""

    from pheasant.services import answer_resume
    from pheasant.sync.engine import SyncEngine

    loaded_config.storage.backend = "postgres"
    loaded_config.storage.dsn_env = "PHEASANT_TEST_POSTGRES_DSN"
    engine = SyncEngine(loaded_config)
    try:
        context = ServiceContext(config=loaded_config, state=engine.state, searcher=None)
        kept = artifacts.keep_composed(
            context, "@compose counts", "ada@example.com", _composed("q")
        )
        assert kept is not None
        listed = artifacts.list_artifacts(context, "ada@example.com")["artifacts"]
        assert kept["id"] in [item["id"] for item in listed]
        assert artifacts.get(context, kept["id"], "ada@example.com")["payload"]["files"]
        assert artifacts.list_artifacts(context, None)["artifacts"] == [] or all(
            item["id"] != kept["id"]
            for item in artifacts.list_artifacts(context, None)["artifacts"]
        )
        artifacts.delete(context, kept["id"], "ada@example.com")

        recorder = answer_resume.start(context, "pg-answer-000001", "ada@example.com")
        recorder.step({"name": "plan"})
        recorder.answered({"answer": "done", "visual": {"status": "none"}})
        read = answer_resume.read(context, "pg-answer-000001", "ada@example.com")
        assert read["status"] == "done" and read["steps"] == [{"name": "plan"}]
    finally:
        engine.close()

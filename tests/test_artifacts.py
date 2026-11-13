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
def composing(monkeypatch: pytest.MonkeyPatch, loaded_config) -> None:
    """The answering workflow returns a composed answer, and the region auto-saves it."""

    from pheasant.assistant import chat

    loaded_config.artifacts.auto_save = True

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
    assert names[:3] == ["account_counts.js", "report.html", "README.md"]
    assert names[-1] == "counts.html"
    assert any(name.startswith("evidence/ev-") and name.endswith(".md") for name in names)
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


def test_every_session_sees_an_artifact_filed_under_whoever_kept_it(loaded_config) -> None:
    """Kept is knowledge: visible to everyone the region serves, organized by owner, then time."""

    app = create_app(config=loaded_config)
    context = _context(app)
    saved = artifacts.save(
        context, artifacts.SaveRequest(title="Mine", markdown="text", principal="ada@example.com")
    )
    assert re.fullmatch(
        r"\d{4}/\d{2}/\d{2}/ada@example\.com/no-session/mine--[0-9a-f]{24}", saved["folder"]
    )
    assert saved["owner"] == "ada@example.com" and saved["source"] == "artifacts"
    # Another person, another session: the same artifact.
    assert artifacts.get(context, saved["id"], "grace@example.com")["title"] == "Mine"
    listed = artifacts.list_artifacts(context, "grace@example.com")["artifacts"]
    assert [item["id"] for item in listed] == [saved["id"]]
    assert artifacts.list_artifacts(context, owner_name="grace@example.com")["artifacts"] == []

    client = TestClient(app)
    assert client.delete(f"/artifacts/{saved['id']}").json()["deleted"] == saved["id"]
    gone = client.get(f"/artifacts/{saved['id']}")
    assert gone.status_code == 404 and gone.json()["code"] == "UNKNOWN_ARTIFACT"


def test_a_kept_artifact_is_knowledge_searchable_linked_to_its_evidence_and_removable(
    loaded_config,
) -> None:
    """The source, the index, the graph and removal: what makes it knowledge."""

    from pheasant.sync.engine import SyncEngine

    engine = SyncEngine(loaded_config)
    try:
        engine.sync_source("architecture-notes", "full")
        context = ServiceContext(
            config=loaded_config, state=engine.state, searcher=None, engine=engine
        )
        result = _composed("@compose account counts")
        result["citations"] = [
            {"index": 1, "source_id": "architecture-notes", "relative_path": "sync-engine.md"}
        ]
        result["answer"] = ANSWER.replace("[1]", "[1] zephyrine")
        kept = artifacts.keep_composed(context, "@compose account counts", None, result, save=True)
        folder = kept["folder"]
        assert "/shared/no-session/" in folder and kept["source"] == "artifacts"

        # A landing-zone source, registered like the drop zone's.
        source = next(s for s in loaded_config.sources if s.name == "artifacts")
        assert source.type.value == "document_folder"
        assert engine.sync_source("artifacts", "incremental").indexed_artifacts == 3
        paths = {
            row["relative_path"]
            for row in engine.state.rows(
                "SELECT relative_path FROM artifacts WHERE source_id='artifacts'"
            )
        }
        assert paths == {
            f"{folder}/{name}" for name in ("README.md", "account_counts.js", "report.html")
        }

        # Searchable from the overall index, with no source named.
        client = TestClient(create_app(config=loaded_config))
        hits = client.post("/search", json={"query": "zephyrine", "max_results": 5}).json()
        assert any(":artifacts:" in str(hit.get("node_id")) for hit in hits["results"]), hits

        # The evidence is an edge: README -> the document it cites.
        graph = engine.serving_graph()
        readme = f"file:artifacts:{folder}/README.md:branch=none"
        targets = {target for _s, target, _e in graph.out_edges(readme)}
        cited = next(t for t in targets if t.startswith("file:architecture-notes:sync-engine.md"))
        assert cited
        walked = client.get(
            "/graph/neighbors", params={"node_id": readme, "depth": 1, "max_nodes": 50}
        ).json()
        assert any(n.get("node_id", n.get("id")) == cited for n in walked["neighbors"]), walked

        # Removable like any other document: deleting takes its files out of the index.
        assert artifacts.delete(context, kept["id"])["removed_files"]
        left = engine.state.rows("SELECT id FROM artifacts WHERE source_id='artifacts'")
        assert left == []
    finally:
        engine.close()


def test_removing_an_artifacts_index_page_retires_the_artifact(loaded_config) -> None:
    """The taxonomy view's Remove, @remove and archive_documents act on artifacts too."""

    from pheasant.services import archive
    from pheasant.sync.engine import SyncEngine

    engine = SyncEngine(loaded_config)
    try:
        context = ServiceContext(
            config=loaded_config, state=engine.state, searcher=None, engine=engine
        )
        saved = artifacts.save(context, artifacts.SaveRequest(title="Notes", markdown="# Notes"))
        engine.sync_source("artifacts", "incremental")
        archive.archive_documents(
            context, archive.ArchiveRequest(documents=[f"artifacts/{saved['folder']}/README.md"])
        )
        with pytest.raises(ArtifactNotFound):
            artifacts.get(context, saved["id"])
    finally:
        engine.close()


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
            context, "@compose counts", "ada@example.com", _composed("q"), save=True
        )
        assert kept is not None
        listed = artifacts.list_artifacts(context, "ada@example.com")["artifacts"]
        assert kept["id"] in [item["id"] for item in listed]
        assert artifacts.get(context, kept["id"], "ada@example.com")["payload"]["files"]
        # Everyone sees it; filtering by owner narrows to one person's.
        assert kept["id"] in [i["id"] for i in artifacts.list_artifacts(context)["artifacts"]]
        assert kept["id"] not in [
            i["id"] for i in artifacts.list_artifacts(context, owner_name="nobody")["artifacts"]
        ]
        artifacts.delete(context, kept["id"], "ada@example.com")

        recorder = answer_resume.start(context, "pg-answer-000001", "ada@example.com")
        recorder.step({"name": "plan"})
        recorder.answered({"answer": "done", "visual": {"status": "none"}})
        read = answer_resume.read(context, "pg-answer-000001", "ada@example.com")
        assert read["status"] == "done" and read["steps"] == [{"name": "plan"}]
    finally:
        engine.close()


def test_citations_in_a_run_are_each_linked() -> None:
    """Answers cite in runs ("[11][7]"); the first of a pair was left as text."""

    rendered = artifact_render.markdown_html(
        "Deployments run on a schedule [11][7] [3].", {3, 7, 11}
    )
    for index in (11, 7, 3):
        assert f'href="#source-{index}"' in rendered, index
    assert artifact_render.markdown_html("[9] is not a source", {3}).startswith("<p>[9]")


def test_an_artifact_kept_before_artifacts_were_knowledge_is_filed_once(loaded_config) -> None:
    """A row with no folder (kept by an earlier build) lands in the source when listed."""

    from pheasant.sync.engine import SyncEngine

    engine = SyncEngine(loaded_config)
    try:
        context = ServiceContext(
            config=loaded_config, state=engine.state, searcher=None, engine=engine
        )
        payload = {"answer": "# Old\n\nkept before", "files": [], "citations": []}
        engine.state.execute(
            "INSERT INTO saved_artifacts(id, kb_id, owner, kind, title, payload_json, created_at, "
            "updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                "a" * 24,
                loaded_config.knowledge_base_id,
                "ada@example.com",
                "report",
                "Old report",
                json.dumps(payload),
                "2026-11-08T21:00:00+00:00",
                "2026-11-08T21:00:00+00:00",
            ),
        )
        [listed] = artifacts.list_artifacts(context)["artifacts"]
        assert listed["folder"] == "2026/11/08/ada@example.com/no-session/old-report--" + "a" * 24
        engine.sync_source("artifacts", "incremental")
        indexed = engine.state.rows(
            "SELECT relative_path FROM artifacts WHERE source_id='artifacts'"
        )
        assert [row["relative_path"] for row in indexed] == [f"{listed['folder']}/README.md"]
        # Once: the row now holds its folder.
        assert artifacts.get(context, "a" * 24)["folder"] == listed["folder"]
    finally:
        engine.close()


def test_an_unnamed_html_block_is_drawn_not_shown_as_code() -> None:
    """A compose that writes its report as an unnamed html block gets a page, not source."""

    from pheasant.assistant.artifact_files import artifact_payload

    page = "<html><body><h1>Advantage</h1><script>document.title='x'</script></body></html>"
    answer = f"Here it is.\n\n```html\n{page}\n```"
    parsed = artifact_payload({"answer": answer, "answer_mode": "llm"})
    assert [f["name"] for f in parsed["files"]] == ["block-1.html"]
    assert artifact_render.previewable("block-1.html")

    # One kept before this, named with no extension, draws by its language.
    legacy = {"name": "block-1", "language": "html", "content": page}
    record = {"title": "t", "kind": "files", "payload": {"files": [legacy]}}
    assert artifact_render.previewable("block-1", "html")
    assert '<iframe class="html-frame" sandbox="allow-scripts"' in artifact_render.overview(record)
    drawn = artifact_render.file_document(record, legacy, mode="preview")
    assert drawn.index("Content-Security-Policy") < drawn.index("<h1>Advantage</h1>")


def test_search_and_answers_take_artifacts_in_out_or_alone_on_both_surfaces(
    loaded_config,
) -> None:
    """The `artifacts` toggle, like `memory`: include (default), exclude, only."""

    from pheasant.sync.engine import SyncEngine

    engine = SyncEngine(loaded_config)
    try:
        engine.sync_source("architecture-notes", "full")
        context = ServiceContext(
            config=loaded_config, state=engine.state, searcher=None, engine=engine
        )
        result = _composed("@compose account counts")
        result["citations"] = [
            {"index": 1, "source_id": "architecture-notes", "relative_path": "sync-engine.md"}
        ]
        result["answer"] = ANSWER.replace("[1]", "[1] zephyrine sync engine")
        artifacts.keep_composed(context, "@compose account counts", None, result, save=True)
        engine.sync_source("artifacts", "incremental")
    finally:
        engine.close()

    kb = loaded_config.pheasant.name
    client = TestClient(create_app(config=loaded_config))
    tools = PheasantTools(loaded_config)

    def sources(payload: dict) -> set[str]:
        from pheasant.search.criteria import source_of

        return {source_of(hit) for hit in payload["results"]} - {""}

    def http(**criteria: Any) -> dict:
        response = client.post("/search", json={"query": "zephyrine sync engine", **criteria})
        assert response.status_code == 200, response.text
        return response.json()

    def mcp(**criteria: Any) -> dict:
        return tools.search_context(kb, "zephyrine sync engine", **criteria)

    for search in (http, mcp):
        assert sources(search()) == {"artifacts", "architecture-notes"}
        excluded = search(artifacts="exclude")
        assert sources(excluded) == {"architecture-notes"}
        assert excluded["criteria"]["exclude_sources"] == ["artifacts"]
        assert sources(search(artifacts=False)) == {"architecture-notes"}
        assert sources(search(artifacts="only")) == {"artifacts"}

    batch = client.post(
        "/search/batch", json={"queries": ["zephyrine", "sync engine"], "artifacts": "exclude"}
    ).json()
    assert "artifacts" not in sources(batch)
    assert "artifacts" not in sources(
        tools.search_context_batch(kb, ["zephyrine", "sync engine"], artifacts="exclude")
    )

    # One refusal, spelled once, on both surfaces.
    for bad in (
        {"artifacts": "sometimes"},
        {"artifacts": "only", "source_name": "architecture-notes"},
    ):
        refused = client.post("/search", json={"query": "sync", **bad})
        assert refused.status_code == 422, refused.text
        with pytest.raises(ValueError) as raised:
            tools.search_context(kb, "sync", **bad)
        assert str(raised.value) in refused.text

    # Every retrieval an answer runs honours it, the extractive one included.
    def cited(**criteria: Any) -> set[str]:
        answer = client.post(
            "/assistant/chat", json={"question": "zephyrine sync engine", **criteria}
        ).json()
        return {str(c.get("source_id")) for c in answer["citations"]}

    assert "artifacts" in cited()
    assert "artifacts" not in cited(artifacts="exclude")
    assert cited(artifacts="only") == {"artifacts"}
    asked = tools.ask_knowledge_base(kb, "zephyrine sync engine", artifacts="exclude")
    assert "artifacts" not in {str(c.get("source_id")) for c in asked["citations"]}


def test_a_file_kept_with_no_extension_is_renamed_once_and_keeps_its_evidence(
    loaded_config,
) -> None:
    """``block-1`` kept before blocks were named becomes ``block-1.html``, in the index too."""

    from pheasant.sync.engine import SyncEngine

    engine = SyncEngine(loaded_config)
    try:
        engine.sync_source("architecture-notes", "full")
        context = ServiceContext(
            config=loaded_config, state=engine.state, searcher=None, engine=engine
        )
        result = _composed("@compose an html report")
        result["citations"] = [
            {"index": 1, "source_id": "architecture-notes", "relative_path": "sync-engine.md"}
        ]
        result["answer"] = "A report [1].\n\n```html\n<html><body>Advantage</body></html>\n```"
        kept = artifacts.keep_composed(context, "@compose an html report", None, result, save=True)
        # As one was kept before: the block's file has no extension.
        record = artifacts._load(context, kept["id"])
        record["payload"]["files"][0]["name"] = "block-1"
        artifacts._save_payload(context, kept["id"], record["payload"])
        artifacts._publish(context, record)
        landed = next((loaded_config.pheasant.state_path / "uploads").rglob("block-1.html"))
        landed.unlink()
        engine.sync_source("artifacts", "incremental")

        def indexed() -> set[str]:
            return {
                row["relative_path"].rsplit("/", 1)[-1]
                for row in engine.state.rows(
                    "SELECT relative_path FROM artifacts WHERE source_id='artifacts'"
                )
            }

        # A file with no extension is not indexed at all: the folder showed no report.
        assert indexed() == {"README.md"}

        listed = artifacts.list_artifacts(context)["artifacts"]
        assert [f["name"] for f in listed[0]["files"]] == ["block-1.html"]
        assert listed[0]["files"][0]["previewable"]
        engine.sync_source("artifacts", "incremental")
        assert indexed() == {"README.md", "block-1.html"}
        # Its evidence is the artifact's own, untouched.
        again = artifacts._load(context, kept["id"])
        assert again["payload"]["citations"] == record["payload"]["citations"]
        assert artifacts.list_artifacts(context)["artifacts"][0]["files"][0]["name"] == (
            "block-1.html"
        )
    finally:
        engine.close()

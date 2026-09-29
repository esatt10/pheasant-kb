"""The role-split fleet, as processes, with the assistant features driven through it.

``docker-compose.scale.yml`` is the fleet's shape: Postgres, NATS JetStream, an
``indexer`` that is the sole writer of ``/state``, a ``graph`` service that owns
graph reads, ``api`` replicas that mount ``/state`` read-only and hold no graph,
and gRPC preparation ``worker``\\ s. This runs that shape without Docker — every
tier a ``pheasant serve --role …`` process, every config generated from the
shipped ``fleet.yaml`` with only hosts, ports, paths and the model ``base_url``
changed — so it can run wherever a container runtime cannot, and against the
code in the checkout rather than a published image.

What it keeps from the Compose file, because each is a place a feature can
work in one container and fail in the fleet:

* **``/state`` is read-only to ``api`` and ``graph``.** When run as root they run
  as a separate user that can read the indexer's state and write none of it.
* **Graph reads cross the network.** ``api`` replicas answer graph questions
  through the ``graph`` service (``graph.query_service_url``), never a resident
  graph of their own.
* **Four distinct tokens**: API, graph service, worker, ingestion.
* **Two api replicas**, which must agree with each other.
* **Remote preparation** through a gRPC worker holding only the worker token.

Needs: ``PHEASANT_DATABASE_URL`` (a throwaway Postgres database) and
``PHEASANT_NATS_URL`` (a NATS server with JetStream). The model is
``fake_openai.py``, so no key is needed and every reply is deterministic.

    python process_fleet.py --work /tmp/fleet [--python .venv/bin/python] [--keep]

Exit status 0 only when every check passes; the report names each one.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
FIXTURE_IMAGE = REPO / "tests" / "fixtures" / "sample_workspace" / "images" / "diagram.png"
PORTS = {"indexer": 18771, "graph": 18772, "api1": 18773, "api2": 18774, "llm": 18779}
WORKER_PORT = 18766
READONLY_USER = "pheasant-ro"


# --------------------------------------------------------------------------- setup


def corpus(root: Path) -> None:
    docs, images = root / "docs", root / "images"
    docs.mkdir(parents=True, exist_ok=True)
    images.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE_IMAGE, images / "topology.png")
    (docs / "design.md").write_text(
        "# Deployment design\n\nThe deploy pipeline routes each release through a router "
        "to three regions over HTTP.\n\n![Deploy topology](../images/topology.png)\n\n"
        "The router imports the region client and retries on failure.\n",
        encoding="utf-8",
    )
    (docs / "rollbacks.md").write_text(
        "# Rollbacks\n\nA rollback restores the previous release. The deploy pipeline keeps "
        "the last three releases so a rollback never rebuilds.\n",
        encoding="utf-8",
    )
    (docs / "release.md").write_text(
        "# Release process\n\nBuild the image, run the tests, then promote it through the "
        "deploy pipeline. See [rollbacks](rollbacks.md) and [design](design.md).\n",
        encoding="utf-8",
    )


def configs(work: Path, llm: str) -> dict[str, Path]:
    fleet = yaml.safe_load((REPO / "deploy" / "compose" / "fleet.yaml").read_text())
    state, workspace = work / "state", work / "workspace"
    exports, memory = work / "exports", work / "memory"
    fleet["pheasant"].update(
        state_path=str(state), workspace_root=str(workspace), exports_path=str(exports)
    )
    fleet["server"]["host"] = "127.0.0.1"
    for section in ("embeddings",):
        fleet["search"][section]["base_url"] = llm
    fleet["search"]["vector_store"]["path"] = str(state / "vectors")
    for section in ("captioner", "transcriber"):
        fleet["ingestion"][section]["base_url"] = llm
    fleet["assistant"]["base_url"] = llm
    fleet["ingestion"]["landing_service_url"] = f"http://127.0.0.1:{PORTS['indexer']}"
    fleet["graph"]["query_service_url"] = f"http://127.0.0.1:{PORTS['graph']}"
    nats = os.environ["PHEASANT_NATS_URL"]
    fleet["sync"]["queue"]["nats_servers"] = [nats]
    fleet["sync"]["concurrency"]["remote_worker_urls"] = [f"grpc://127.0.0.1:{WORKER_PORT}"]
    fleet["sync"]["concurrency"]["remote_worker_enabled"] = True
    fleet["sync"]["watcher"]["enabled"] = False
    fleet["security"]["allow_workspace_roots"] = [
        str(workspace),
        str(memory),
        str(state),
        str(exports),
    ]
    fleet["sources"][0]["path"] = str(memory)
    # A mounted corpus, as the fleet's operators add one: documents and the
    # image one of them shows.
    fleet["sources"].append(
        {
            "name": "handbook",
            "type": "document_folder",
            "path": str(workspace / "handbook"),
            "include": ["**/*.md", "**/*.png"],
        }
    )
    written = {}
    for role, port in PORTS.items():
        if role == "llm":
            continue
        data = json.loads(json.dumps(fleet))
        data["server"]["port"] = port
        path = work / f"fleet.{role}.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        written[role] = path
    worker = yaml.safe_load((REPO / "deploy" / "compose" / "worker.yaml").read_text())
    worker["pheasant"].update(
        state_path=str(work / "worker-state"),
        workspace_root=str(workspace),
        exports_path=str(work / "worker-exports"),
    )
    path = work / "worker.yaml"
    path.write_text(yaml.safe_dump(worker, sort_keys=False))
    written["worker"] = path
    return written


class Fleet:
    def __init__(self, work: Path, python: str) -> None:
        self.work, self.python = work, python
        self.procs: dict[str, subprocess.Popen] = {}
        self.tokens = {
            name: secrets.token_urlsafe(24) for name in ("api", "graph", "worker", "ingestion")
        }
        self.readonly_uid: int | None = None
        if os.geteuid() == 0:
            import pwd

            try:
                self.readonly_uid = pwd.getpwnam(READONLY_USER).pw_uid
            except KeyError:
                subprocess.run(
                    ["useradd", "--system", "--no-create-home", READONLY_USER], check=True
                )
                self.readonly_uid = pwd.getpwnam(READONLY_USER).pw_uid

    def env(self, *, worker: bool = False) -> dict[str, str]:
        # Nothing ambient: a tier gets exactly the secrets its Compose service
        # gets. The UI bundle's location is the one non-secret passed through.
        base = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("PHEASANT_", "OPENAI", "ANTHROPIC", "GEMINI"))
        }
        if os.environ.get("PHEASANT_UI_DIST") and not worker:
            base["PHEASANT_UI_DIST"] = os.environ["PHEASANT_UI_DIST"]
        base["PHEASANT_INDEX_WORKER_TOKEN"] = self.tokens["worker"]
        if worker:
            return base
        base.update(
            PHEASANT_DATABASE_URL=os.environ["PHEASANT_DATABASE_URL"],
            PHEASANT_API_TOKEN=self.tokens["api"],
            PHEASANT_GRAPH_SERVICE_TOKEN=self.tokens["graph"],
            PHEASANT_INGESTION_SERVICE_TOKEN=self.tokens["ingestion"],
            OPENAI_API_KEY="fake-key-for-the-stub",
        )
        return base

    def start(self, name: str, argv: list[str], *, readonly: bool, worker: bool = False) -> None:
        log = open(self.work / f"{name}.log", "w")
        kwargs: dict[str, Any] = {}
        if readonly and self.readonly_uid is not None:
            kwargs["user"] = self.readonly_uid
        self.procs[name] = subprocess.Popen(
            argv,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=self.env(worker=worker),
            cwd=self.work,
            start_new_session=True,
            **kwargs,
        )

    def stop(self) -> None:
        for proc in self.procs.values():
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for proc in self.procs.values():
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)

    def alive(self) -> list[str]:
        return [name for name, proc in self.procs.items() if proc.poll() is not None]


def wait_port(port: int, seconds: float = 90) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), 1).close()
            return
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"nothing listening on {port}")


# --------------------------------------------------------------------------- http


def http(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: Any = None,
    raw: bool = False,
    timeout: float = 120,
) -> tuple[int, Any, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            return (
                response.status,
                payload if raw else json.loads(payload or b"null"),
                {k.lower(): v for k, v in response.headers.items()},
            )
    except urllib.error.HTTPError as error:
        payload = error.read()
        try:
            parsed = json.loads(payload)
        except ValueError:
            parsed = payload.decode(errors="replace")
        return error.code, parsed, {k.lower(): v for k, v in error.headers.items()}


def stream(url: str, token: str, body: dict) -> list[dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    events = []
    with urllib.request.urlopen(request, timeout=300) as response:
        for line in response:
            line = line.decode().strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


# --------------------------------------------------------------------------- checks


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        print(
            f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""), flush=True
        )
        return bool(ok)

    @property
    def failed(self) -> list[str]:
        return [name for name, ok, _ in self.rows if not ok]


def found(payload: dict) -> set[str]:
    """Source paths a search reached, whatever kind of hit carried them.

    A graph `relationship` hit names its artifact by `node_id` and has no
    `relative_path`, so both are read.
    """
    paths = set()
    for hit in payload.get("results") or []:
        if hit.get("relative_path"):
            paths.add(hit["relative_path"])
        node = str(hit.get("node_id") or "")
        if node.startswith("file:"):
            paths.add(node.split(":", 2)[2].rsplit(":branch=", 1)[0])
    return paths


def ids(payload: dict) -> list[str]:
    return [str(r.get("chunk_id") or r.get("node_id")) for r in payload.get("results") or []]


def run_checks(fleet: Fleet, report: Report) -> None:
    api = [f"http://127.0.0.1:{PORTS['api1']}", f"http://127.0.0.1:{PORTS['api2']}"]
    token = fleet.tokens["api"]
    llm = f"http://127.0.0.1:{PORTS['llm']}"

    # Indexing reaches the api tier through Postgres and the graph service.
    deadline = time.monotonic() + 240
    hits: dict = {}
    while time.monotonic() < deadline:
        status, hits, _ = http(
            "POST",
            f"{api[0]}/search",
            token=token,
            body={"query": "deploy topology router", "max_results": 5},
        )
        if status == 200 and {"docs/design.md", "images/topology.png"} <= found(hits):
            break
        time.sleep(2)
    report.check(
        "the handbook is indexed and searchable from an api replica",
        {"docs/design.md", "images/topology.png"} <= found(hits),
        str(found(hits)),
    )

    status, _, _ = http("POST", f"{api[0]}/search/batch", body={"queries": ["deploy"]})
    report.check("the api tier refuses a request without the token", status == 401, str(status))

    batch = [
        http(
            "POST",
            f"{a}/search/batch",
            token=token,
            body={"queries": ["deploy pipeline", "rollback", "router"], "max_results": 4},
        )
        for a in api
    ]
    report.check("batch search answers on both replicas", all(b[0] == 200 for b in batch))
    report.check(
        "both replicas return the same merged context",
        ids(batch[0][1]) == ids(batch[1][1]) and batch[0][1]["counts"] == batch[1][1]["counts"],
        json.dumps(batch[0][1].get("counts")),
    )

    # A short answer through the agentic workflow the fleet configures.
    answers = [
        http(
            "POST",
            f"{a}/assistant/chat",
            token=token,
            body={"question": "show me the deploy topology image"},
        )[1]
        for a in api
    ]
    first = answers[0]
    report.check(
        "an image question is routed to the corpus's image",
        (first.get("route") or {}).get("visual") == "image",
        json.dumps(first.get("route")),
    )
    figures = first.get("figures") or []
    report.check(
        "the answer carries the figure the design doc embeds (via the graph service)",
        any(f.get("relative_path") == "images/topology.png" for f in figures),
        json.dumps([f.get("relative_path") for f in figures]),
    )
    report.check(
        "invented [42] and [fig:9] markers were verified away",
        "[42]" not in first.get("answer", "") and "[fig:9]" not in first.get("answer", ""),
        first.get("answer", "")[:160].replace("\n", " "),
    )
    report.check(
        "both replicas answer identically",
        [c["node_id"] for c in answers[0].get("citations", [])]
        == [c["node_id"] for c in answers[1].get("citations", [])]
        and answers[0].get("figures") == answers[1].get("figures"),
    )
    visual = first.get("visual") or {}
    report.check(
        "the image visual lists the figure",
        visual.get("type") == "images" and visual.get("status") == "ok",
        json.dumps(visual)[:120],
    )

    # The planner overrules the length rule on a question no rule reads.
    rollback = http(
        "POST",
        f"{api[1]}/assistant/chat",
        token=token,
        body={"question": "what happens on a rollback?"},
    )[1]
    route = rollback.get("route") or {}
    report.check(
        "the planner may overrule an unpinned depth",
        route.get("depth") == "medium" and route.get("decided_by", {}).get("depth") == "planner",
        json.dumps(route),
    )

    # A long answer: outline, parallel sections, stitched with headings.
    long = http(
        "POST",
        f"{api[0]}/assistant/chat",
        token=token,
        body={"question": "write a comprehensive report on the deploy pipeline"},
    )[1]
    names = [s["name"] for s in long.get("steps") or []]
    report.check(
        "a long answer is outlined and written in sections",
        "outline" in names and "sections" in names and "### " in long.get("answer", ""),
        ", ".join(names),
    )

    # A follow-up, streamed, with a diagram pinned: text first, picture after.
    history = [{"question": "what happens on a rollback?", "answer": rollback.get("answer", "")}]
    events = stream(
        f"{api[1]}/assistant/chat/stream",
        token,
        {"question": "and what about it for the router?", "history": history, "visual": "diagram"},
    )
    kinds = [e["type"] for e in events]
    answer_event = next((e for e in events if e["type"] == "answer"), {})
    visual_event = next((e for e in events if e["type"] == "visual"), {})
    report.check(
        "a follow-up is searched in context",
        any(e.get("name") == "context" for e in events if e["type"] == "step")
        and "regarding" in (answer_event.get("answer", {}).get("search_question") or ""),
        answer_event.get("answer", {}).get("search_question") or "no search_question",
    )
    report.check(
        "the stream sends the answer before the visual",
        kinds.index("answer") < kinds.index("visual") if "visual" in kinds else False,
        " > ".join(kinds[-3:]),
    )
    drawn = visual_event.get("visual") or {}
    inferred = [n for n in (drawn.get("diagram") or {}).get("nodes", []) if n.get("inferred")]
    report.check(
        "the diagram is grounded, and its unsourced element is marked inferred",
        drawn.get("status") == "ok" and [n["label"] for n in inferred] == ["Unsourced step"],
        json.dumps(drawn.get("grounding")),
    )

    # Visualize one passage, by id.
    design = "file:handbook:docs/design.md:branch=none"
    status, made, _ = http(
        "POST",
        f"{api[0]}/assistant/visual",
        token=token,
        body={"request": "the deployment design", "node_ids": [design]},
    )
    report.check(
        "visualize a named passage",
        status == 200 and (made.get("visual") or {}).get("status") == "ok",
        f"{status} {json.dumps(made)[:120]}",
    )

    # Media: written by the indexer, served by both read-only replicas.
    image = "file:handbook:images/topology.png:branch=none"
    for index, base in enumerate(api, start=1):
        status, body, headers = http("GET", f"{base}/media?node_id={image}", token=token, raw=True)
        report.check(
            f"api{index} serves the image from the media store",
            status == 200
            and body == FIXTURE_IMAGE.read_bytes()
            and headers.get("content-type") == "image/png"
            and headers.get("x-content-type-options") == "nosniff",
            f"{status} {headers.get('content-type')}",
        )
    stored = list((fleet.work / "state" / "media").rglob("*.png"))
    report.check("the indexer wrote the media store", len(stored) == 1, str(stored))
    status, body, _ = http("GET", f"{api[0]}/media?node_id={design}", token=token)
    report.check(
        "a document is not media",
        status == 404 and body.get("code") == "UNKNOWN_MEDIA",
        str(status),
    )

    # The MCP surface through a real client.
    report.check(
        "MCP: the app, its tools and image content through a real client",
        *asyncio.run(mcp_checks(api[0], token, image)),
    )

    stats = http("GET", f"{llm}/stats")[1]
    report.check(
        "the model was asked to plan, rewrite, outline, write sections and draw",
        all(
            stats.get(k, 0) >= 1
            for k in (
                "planner",
                "rewrite",
                "outline",
                "section",
                "diagram",
                "answer",
                "embeddings",
                "caption",
            )
        ),
        json.dumps(stats),
    )

    # An edit, through the fleet's own path: a replica queues the sync, the
    # indexer commits rows, the graph service picks up the new generation, and
    # both replicas stop showing a figure the page no longer contains.
    page = fleet.work / "workspace" / "handbook" / "docs" / "design.md"
    page.write_text(
        "# Deployment design\n\nThe deploy pipeline routes each release through "
        "a router to three regions over HTTP.\n\nThe diagram was removed.\n",
        encoding="utf-8",
    )
    status, queued, _ = http(
        "POST", f"{api[0]}/sync/handbook", token=token, body={"mode": "incremental", "wait": False}
    )
    report.check(
        "a replica queues a sync for the indexer",
        status == 200,
        f"{status} {json.dumps(queued)[:120]}",
    )
    question = {
        "question": "what does the deployment design say about the router?",
        "workflow": "simple",
    }

    # A figure the answer cites *directly* (the image's caption matched) is
    # still a figure of itself; what must go is the page embedding it.
    def embedded_by_page(figures: list[dict] | None) -> list[str]:
        return sorted(
            {via for f in figures or [] for via in f.get("embedded_in") or [] if "design.md" in via}
        )

    deadline, remaining, reindexed = time.monotonic() + 240, None, False
    while time.monotonic() < deadline:
        remaining = [
            embedded_by_page(
                http("POST", f"{a}/assistant/chat", token=token, body=question)[1].get("figures")
            )
            for a in api
        ]
        text = http(
            "POST",
            f"{api[1]}/search",
            token=token,
            body={"query": "diagram was removed", "mode": "text", "max_results": 3},
        )[1]
        reindexed = "The diagram was removed" in json.dumps(text)
        if reindexed and remaining == [[], []]:
            break
        time.sleep(3)
    report.check(
        "after an edit drops the image link, no replica shows it as the page's figure",
        reindexed and remaining == [[], []],
        f"reindexed={reindexed} embedded_in={json.dumps(remaining)[:160]}",
    )

    # The UI, against a replica, through the hosted MCP App.
    ui = ui_check(api[0], token)
    if ui is not None:
        report.check(
            "UI: an inline figure, and a diagram redrawn as a timeline in the hosted MCP App",
            *ui,
        )

    problems = []
    for name in ("indexer", "graph", "api1", "api2", "worker"):
        text = (fleet.work / f"{name}.log").read_text(errors="replace")
        for marker in ("Traceback", "Read-only file system", "PermissionError"):
            if marker in text:
                problems.append(f"{name}: {marker}")
    report.check(
        "no tier logged a traceback or a write to read-only state",
        not problems,
        "; ".join(problems),
    )


async def mcp_checks(base: str, token: str, image: str) -> tuple[bool, str]:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=120
    ) as http_client:
        async with Client(
            streamable_http_client(f"{base}/mcp/", http_client=http_client)
        ) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            meta_ok = all(
                (tools[n].meta or {}).get("ui", {}).get("resourceUri")
                == "ui://pheasant/knowledge-view.html"
                for n in ("ask_knowledge_base", "create_visual", "get_image")
            )
            read = await client.read_resource("ui://pheasant/knowledge-view.html")
            result = await client.call_tool(
                "get_image", {"knowledge_base": "pheasant-scalable", "node_id": image}
            )
            block = next((b for b in result.content if b.type == "image"), None)
            image_ok = (
                block is not None and base64.b64decode(block.data) == FIXTURE_IMAGE.read_bytes()
            )
            batch = await client.call_tool(
                "search_context_batch",
                {"knowledge_base": "pheasant-scalable", "queries": ["router", "rollback"]},
            )
            ok = (
                meta_ok
                and image_ok
                and read.contents[0].mime_type == "text/html;profile=mcp-app"
                and not batch.is_error
            )
            return ok, f"meta={meta_ok} image={image_ok} batch={not batch.is_error}"


def ui_check(base: str, token: str) -> tuple[bool, str] | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  SKIP  UI (playwright not installed)")
        return None
    chromium = os.environ.get("PHEASANT_CHROMIUM", "/opt/pw-browsers/chromium")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium if Path(chromium).exists() else None)
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        # Init scripts run in every frame, including the sandboxed MCP App view,
        # where touching storage throws — which is the sandbox working. Guard it.
        page.add_init_script(
            "try { sessionStorage.setItem('pheasant.api.token', " + json.dumps(token) + ") }"
            " catch (e) {}"
        )
        page.goto(base + "/")
        box = page.get_by_label("Ask a question")
        box.wait_for(timeout=30000)

        def ask(question: str, answers: int) -> None:
            box.fill(question)
            box.press("Enter")
            page.wait_for_function(
                f"document.querySelectorAll('.msg__meta').length >= {answers}", timeout=120000
            )

        # A figure the answer names is shown inline, fetched through the
        # token-guarded /media; the gallery below does not repeat it.
        ask("show me the deploy topology image", 1)
        inline = page.locator(".answer-figure img").first
        inline.wait_for(timeout=60000)
        inline_src = inline.get_attribute("src") or ""
        repeated = page.locator(".msg").last.locator("iframe.mcp-app-frame").count()

        # A drawn diagram in the hosted MCP App, then "Redraw as → Timeline":
        # the frame asks its host for create_visual, the host calls
        # /assistant/visual with the token, and the same passages come back in
        # another shape.
        ask("draw a diagram of the release process", 2)
        frame = page.frame_locator("iframe.mcp-app-frame").last
        frame.locator("svg .node").first.wait_for(timeout=60000)
        frame.get_by_role("button", name="Timeline").click()
        frame.locator("svg .marker").first.wait_for(timeout=60000)
        redrawn = frame.locator("h1").first.text_content() or ""
        page.screenshot(
            path=str(Path(os.environ.get("FLEET_SHOTS", "/tmp")) / "fleet-ui.png"), full_page=True
        )
        browser.close()
        return (
            inline_src.startswith("blob:") and repeated == 0 and not errors,
            f"inline={inline_src[:5]} gallery_frames={repeated} redrawn={redrawn!r} "
            f"errors={errors}",
        )


# --------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--keep", action="store_true", help="leave the fleet running")
    args = parser.parse_args()
    for name in ("PHEASANT_DATABASE_URL", "PHEASANT_NATS_URL"):
        if not os.environ.get(name):
            parser.error(f"set {name}")

    work = args.work.resolve()
    if work.exists():
        shutil.rmtree(work)
    for sub in ("state", "exports", "memory", "worker-state", "worker-exports"):
        (work / sub).mkdir(parents=True)
    corpus(work / "workspace" / "handbook")
    # Writable by the tiers that write them in the Compose file, and nothing
    # else: /state belongs to the indexer, /memory and /exports are shared.
    for sub in ("memory", "exports"):
        os.chmod(work / sub, 0o777)
    for path in [work, *work.rglob("*")]:
        if path.is_dir():
            os.chmod(path, os.stat(path).st_mode | 0o755)
        else:
            os.chmod(path, os.stat(path).st_mode | 0o644)

    llm = f"http://127.0.0.1:{PORTS['llm']}/v1"
    files = configs(work, llm)
    fleet = Fleet(work, args.python)
    report = Report()
    pheasant = [args.python, "-m", "pheasant"]
    try:
        fleet.start(
            "llm", [args.python, str(HERE / "fake_openai.py"), str(PORTS["llm"])], readonly=False
        )
        wait_port(PORTS["llm"])
        fleet.start(
            "worker",
            [
                *pheasant,
                "worker",
                "-c",
                str(files["worker"]),
                "--transport",
                "grpc",
                "--port",
                str(WORKER_PORT),
                "--max-workers",
                "2",
            ],
            readonly=True,
            worker=True,
        )
        # db-init, as the Compose file runs it: the schema before any tier.
        subprocess.run(
            [
                args.python,
                "-c",
                "from pathlib import Path; from pheasant.cli import "
                f"_engine; _engine(Path({str(files['indexer'])!r})).close()",
            ],
            check=True,
            env=fleet.env(),
            cwd=work,
        )
        fleet.start(
            "indexer",
            [*pheasant, "serve", "-c", str(files["indexer"]), "--role", "indexer"],
            readonly=False,
        )
        wait_port(PORTS["indexer"])
        fleet.start(
            "graph",
            [*pheasant, "serve", "-c", str(files["graph"]), "--role", "graph"],
            readonly=True,
        )
        wait_port(PORTS["graph"])
        for name in ("api1", "api2"):
            fleet.start(
                name, [*pheasant, "serve", "-c", str(files[name]), "--role", "api"], readonly=True
            )
        for name in ("api1", "api2"):
            wait_port(PORTS[name])
        if args.keep:
            # For poking at a kept fleet by hand; never printed.
            secrets_path = work / "tokens.json"
            secrets_path.write_text(json.dumps(fleet.tokens))
            os.chmod(secrets_path, 0o600)
        dead = fleet.alive()
        if dead:
            report.check("every tier started", False, ", ".join(dead))
        else:
            report.check(
                "every tier started",
                True,
                "api/graph read-only as "
                + (READONLY_USER if fleet.readonly_uid else "the same user (not root)"),
            )
            run_checks(fleet, report)
    except Exception as exc:  # the report is the product; a crash is a failed check
        report.check("the run completed", False, f"{type(exc).__name__}: {exc}")
    finally:
        if not args.keep:
            fleet.stop()
    print(f"\n{len(report.rows) - len(report.failed)}/{len(report.rows)} checks passed")
    if report.failed:
        print("FAILED: " + "; ".join(report.failed))
        print(f"logs: {work}/*.log")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())

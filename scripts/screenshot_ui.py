"""Regenerate the UI screenshots the README shows.

Repeatable on purpose. A screenshot checked in once and never regenerated is a
picture of a UI that no longer exists — the same rot `sync_version.py` exists
to prevent for image tags — so this seeds a region, drives a real browser
against the real built UI, and overwrites `docs/assets/ui/*.png`.

    npm --prefix ui ci && npm --prefix ui run build
    python scripts/screenshot_ui.py

Everything it shows is produced by the product: the corpus is indexed by the
ordinary pipeline, the memory records are written through `memory_write`, and
the proposals in the Memory tab are mined from real searches this script
performs. Nothing is mocked for the camera.
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
TARGET = REPO / "docs" / "assets" / "ui"
VIEWPORT = {"width": 1440, "height": 900}
#: The Notebook with four tiles open needs a desktop-width window; at 1440 the
#: chat column narrows to a word a line, which is not what anyone works in.
WIDE_VIEWPORT = {"width": 1920, "height": 1080}
#: Below this the Graph shot is a subselection rather than a network.
MIN_GRAPH_NODES = 50

#: A corpus small enough to seed in a second and real enough to search. The
#: vocabulary mismatch is deliberate: the docs say `pheasant-flock`, the
#: searches below say `router`, and that is exactly the pattern
#: `alias-cooccurrence-v1` is built to notice — so the Memory tab's proposals
#: are genuinely mined rather than staged.
#: Enough vocabulary mismatch to produce a review queue worth showing. The
#: Memory tab's job at scale is triage, and a screenshot of three proposals
#: does not show whether the page can do it.
TEAM_WORDS = {
    "router": "pheasant-flock",
    "watcher": "filewatch",
    "runbook": "kestrel",
}

CORPUS = {
    "deploy/rollout.md": (
        "# Rollout\n\n"
        "The pheasant-flock service coordinates every rollout and canary.\n"
        "Promotion waits on the health check before traffic shifts.\n"
    ),
    "deploy/canary.md": (
        "# Canary\n\nCanary steps are driven by the pheasant-flock service before promotion.\n"
    ),
    "runbooks/kestrel.md": (
        "# Kestrel Runbook\n\n"
        "The filewatch daemon restarts nightly at 0300 UTC.\n"
        "Escalate to the on-call rota if it fails twice in a row.\n"
    ),
    "guides/onboarding.md": (
        "# Onboarding\n\nRead the handbook, request access, then pair for a week.\n"
    ),
}

SEARCHES = [
    ("router rollout coordination", "sess-ada"),
    ("router canary promotion", "sess-ada"),
    ("runbook escalation rota", "sess-ada"),
    ("watcher restart schedule", "sess-ada"),
    ("router rollout", "sess-bo"),
    ("router canary steps", "sess-bo"),
    ("runbook oncall", "sess-bo"),
    ("watcher nightly", "sess-bo"),
    ("promotion health gate", "sess-bo"),
    ("promotion health gate", "sess-ada"),
    # Questions the corpus cannot answer. The most useful proposals in the
    # queue: what the region keeps being asked and keeps failing.
    ("vault seal rotation", "sess-ada"),
    ("vault seal rotation", "sess-bo"),
    ("quarterly capacity forecast", "sess-ada"),
    ("quarterly capacity forecast", "sess-bo"),
]

MEMORIES = [
    ("The staging cluster lives in us-east-2.", "org", "infra"),
    ("Rollouts are frozen on Fridays after 1400 UTC.", "org", "deploy"),
    ("Ada owns the kestrel on-call rota.", "org", "people"),
]

#: Typed proof, recorded through `POST /evaluation/evidence` the way an agent
#: or a UI would. Deliberately a *mixture*: accepts, a selection, a rejection
#: and a downstream success, over some queries and not others.
#:
#: The gaps are the point. A screenshot where every query is evidenced would
#: show a coverage figure of 100% and hide the number that actually matters --
#: the plane's central claim is that a score means nothing without the share of
#: queries it was computed over, and a demo that quietly evidences everything
#: is a demo of a system that never has to say so.
EVIDENCE = [
    ("router rollout coordination", "deploy/rollout.md", "explicit_accept"),
    ("router rollout coordination", "guides/onboarding.md", "explicit_reject"),
    ("router canary promotion", "deploy/canary.md", "explicit_accept"),
    ("runbook escalation rota", "runbooks/kestrel.md", "downstream_success"),
    ("watcher restart schedule", "runbooks/kestrel.md", "selected"),
    ("router rollout", "deploy/rollout.md", "selected"),
    ("promotion health gate", "deploy/rollout.md", "cited"),
]


def _free_port() -> int:
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _fetch_benchmark(root: Path) -> dict | None:
    """Materialize SciFact beside the narrative corpus. ``None`` if unavailable.

    Two corpora in one region, because they demonstrate different things and
    neither substitutes for the other.

    The handbook is a *narrative*: a handful of runbooks whose repeated,
    related questions are what memory formation keys on. It is the right
    corpus for the notebook and memory screenshots and a useless one for
    retrieval measurement — with nine documents and a top-10, every query is
    served whatever the ranking does.

    SciFact is a *benchmark*: 395 scientific abstracts, a quarter of them real
    PDFs, and 60 claims whose supporting documents a domain expert annotated.
    Those annotations are the known-positives, so the evaluation and tuning
    numbers measure retrieval rather than measuring this script's opinion of
    what a good answer looks like — which is what they did while the fixture
    was hand-written.

    Skipped, loudly, when the fetch is not possible: the screenshots that do
    not depend on it should still regenerate on a machine with no network.
    """

    import subprocess

    corpus = root / "workspace" / "scifact"
    try:
        subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "fetch_benchmark_corpus.py"),
                "--cache",
                str(REPO / ".benchmark-cache"),
                "--out",
                str(corpus),
            ],
            check=True,
            cwd=REPO,
        )
    except Exception as exc:  # noqa: BLE001 - a missing benchmark is not fatal
        print(f"  benchmark corpus unavailable ({exc}); tuning panels will be thin")
        return None
    index = json.loads((corpus.parent / "benchmark.json").read_text(encoding="utf-8"))
    index["path"] = str(corpus)
    return index


def _seed(root: Path, benchmark: dict | None = None) -> Path:
    workspace = root / "workspace" / "handbook"
    for name, body in CORPUS.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    for name in ("state", "exports", "memory"):
        (root / name).mkdir(parents=True, exist_ok=True)

    config = {
        "pheasant": {
            "name": "handbook",
            "defaults": "standalone",
            "description": "Engineering handbook, runbooks and deploy notes.",
            "state_path": str(root / "state"),
            "workspace_root": str(root / "workspace"),
            "exports_path": str(root / "exports"),
        },
        "storage": {"graph_snapshots": False},
        "search": {"embeddings": {"provider": "stub"}},
        # On, with no provider: the assistant answers extractively -- the top
        # retrieved passages with their citations. That is a real answer from
        # a real index, and it means the screenshot needs no API key and no
        # network, which is what makes it reproducible.
        "assistant": {"enabled": True, "provider": "none"},
        "sync": {"watcher": {"enabled": False}, "scheduler": {"enabled": False}},
        "observability": {
            "interactions": {
                "enabled": True,
                "flush_batch_size": 1,
                # Every search, because a demo region has no traffic to spare:
                # sampling down would leave the live-health panel below its
                # own minimum and photograph an empty state.
                "stage_sample_rate": 1.0,
            }
        },
        "memory": {
            "steering_enabled": True,
            "formation": {"enabled": True, "min_observations": 2, "min_sessions": 2},
        },
        # `max_results: 2` against a corpus this size is what makes *rank*
        # matter: a top-10 over nine documents returns everything, so every
        # query is `served` whatever the ranking does and the stage histogram
        # is a flat bar with nothing to say.
        "tuning": {"enabled": True, "max_results": 2, "minimum_paired_queries": 2},
        # The evaluation plane, sized for a demo corpus. The sufficiency floors
        # exist to stop a *production* region publishing a number from four
        # data points; at this scale they would only stop the page measuring
        # anything at all, and a screenshot of "not enough evidence" in every
        # tile shows nothing about the page.
        #
        # `promotion.enabled` stays False, which is the shipped default and the
        # honest one to photograph: the run computes candidate decisions and
        # applies none of them.
        "evaluation": {
            "enabled": True,
            "proof": {
                "minimum_eligible_queries": 3,
                "minimum_evidenced_queries": 2,
                "minimum_independent_interactions": 2,
                "maximum_single_query_proof_share": 1.0,
            },
            "cohorts": {"anchor_minimum_queries": 5},
        },
        "sources": [
            {
                "name": "handbook",
                "type": "markdown_folder",
                "path": str(workspace),
                "include": ["**/*.md"],
                "sync": {"on_startup": False},
            },
            {
                "name": "agent-memory",
                "type": "memory",
                "path": str(root / "memory"),
                "sync": {"on_startup": False},
            },
        ],
    }
    if benchmark:
        # A second source, not a replacement. The two corpora answer different
        # questions and a real region has several sources anyway.
        config["sources"].insert(
            1,
            {
                "name": "scifact",
                "type": "markdown_folder",
                "path": benchmark["path"],
                # Both formats: the PDFs are the point of writing a quarter of
                # the corpus as PDFs, and excluding them would leave the
                # extraction path unexercised by the numbers.
                "include": ["**/*.md", "**/*.pdf"],
                "sync": {"on_startup": False},
            },
        )
        # With ~400 documents, the demo-scale floors are no longer doing the
        # work they were lowered to do, and `max_results: 2` is no longer the
        # only thing making rank matter.
        config["tuning"]["max_results"] = 5
        config["tuning"]["minimum_paired_queries"] = 5
        config["evaluation"]["cohorts"]["anchor_minimum_queries"] = 10
    path = root / "pheasant.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def _drive_benchmark(client, engine, benchmark: dict) -> None:
    """Ask the benchmark's claims, and record its expert annotations as proof.

    The judgements are **not** invented here. Each one is a domain expert
    having read that abstract and marked the sentences that support or
    contradict the claim — which is precisely what pheasant's proof taxonomy
    calls ``explicit_accept``: somebody looked, and said so.

    A CONTRADICT annotation is recorded as a positive too, deliberately.
    Finding the paper that refutes a claim is a correct answer to "what does
    the literature say about this", and scoring it as a negative would teach
    the region to hide disagreement — which is the opposite of what a
    knowledge base is for.
    """

    import pheasant.evaluation as evaluation

    artifacts = {
        str(row["relative_path"]): str(row["id"])
        for row in engine.state.rows("SELECT id, relative_path FROM artifacts")
    }
    queries = [entry["query"] for entry in benchmark["queries"]]
    # Three passes in different sessions: repetition is what the rolling
    # cohort and the live-health sample count are built from.
    for index in range(3):
        for query in queries:
            client.post(
                "/search",
                json={"query": query, "mode": "hybrid", "max_results": 5},
                headers={
                    "X-Pheasant-Session": f"scifact-{index}",
                    "X-Pheasant-Principal": "user:reviewer",
                },
            )
    recorded = 0
    for entry in benchmark["evidence"]:
        target = artifacts.get(entry["path"])
        if not target:
            continue
        client.post(
            "/evaluation/evidence",
            json={
                "query": entry["query"],
                "target_id": target,
                "event_type": entry["event_type"],
                "principal": "user:reviewer",
                "session_id": "scifact-0",
                "interaction_id": f"scifact-{entry['doc_id']}",
            },
        )
        recorded += 1
    _ = evaluation
    print(f"  benchmark: {len(queries)} claims asked, {recorded} expert judgements recorded")


def _drive(app, benchmark: dict | None = None) -> None:
    """Put real content in front of the camera, through the real surfaces."""

    from fastapi.testclient import TestClient

    from pheasant.memory.formation import run_candidate_rules, run_session_digests

    engine = app.state.engine
    engine.sync_source("handbook", "full")
    if benchmark:
        engine.sync_source("scifact", "full")

    artifacts = {
        str(row["relative_path"]): str(row["id"])
        for row in engine.state.rows("SELECT id, relative_path FROM artifacts")
    }

    # One client for the whole drive. The MCP SDK's session manager runs its
    # lifespan exactly once per app instance, so a second `TestClient(app)`
    # here raises -- which is also why `main` serves a *fresh* app rather than
    # this one.
    with TestClient(app) as client:
        for text, scope, subject in MEMORIES:
            client.post("/memory", json={"text": text, "scope": scope, "subject": subject})
        # Three passes over the same questions, in different sessions. Not
        # padding: a region gets asked the same things repeatedly, that
        # repetition is what memory formation keys on, and it is what takes
        # the live-health panel past its own minimum-sample floor. Below that
        # floor the panel correctly refuses to publish a rate, which is right
        # in production and photographs as an empty state.
        for pass_index in range(3):
            for query, session in SEARCHES:
                client.post(
                    "/search",
                    json={"query": query, "mode": "hybrid", "max_results": 8},
                    headers={
                        "X-Pheasant-Session": f"{session}-{pass_index}" if pass_index else session,
                        "X-Pheasant-Principal": "user:ada",
                    },
                )
        if benchmark:
            _drive_benchmark(client, engine, benchmark)
        # Typed proof, over the same endpoint an agent posts to.
        for query, path, event in EVIDENCE:
            target = artifacts.get(path)
            if not target:
                continue
            client.post(
                "/evaluation/evidence",
                json={
                    "query": query,
                    "target_id": target,
                    "event_type": event,
                    "principal": "user:ada",
                    "session_id": "sess-ada",
                    "interaction_id": f"shot-{path}-{event}",
                },
            )
        app.state.interaction_buffer.flush()

    run_session_digests(engine)
    run_candidate_rules(engine)
    engine.sync_source("agent-memory", "full")
    _evaluate(engine)
    _tune(engine)


def _evaluate(engine) -> None:
    """Run a real batch through the same call the endpoint and the CLI make.

    Nothing is written into the metric tables directly, so what the page
    renders is what a run actually produced -- including the tiles it had to
    report as unmeasured.
    """

    import pheasant.evaluation as evaluation

    outcome = evaluation.run(engine)
    passed = sum(1 for gate in outcome.gates if gate.passed)
    print(f"  evaluation: {outcome.status}, {passed}/{len(outcome.gates)} gates passed")
    if outcome.status not in ("completed", "truncated"):
        raise SystemExit(f"the seeded evaluation run ended as {outcome.status}")


def _tune(engine) -> None:
    """One real tuning batch over the seeded region.

    Real in the same sense the evaluation run is: it replays the searches the
    script performed, attributes each miss to the stage that lost it, and
    reaches whatever decision the evidence supports. The gate panel in the
    README is often a *refusal* for that reason — a demo corpus has no holdout
    cohort, so a genuine improvement cannot be confirmed and is not promoted.
    """

    import pheasant.tuning as tuning

    outcome = tuning.run(engine, force=True)
    decision = outcome.decision.outcome if outcome.decision else "none"
    print(f"  tuning: {outcome.status}, {outcome.trials_run} trials, decision {decision}")
    if outcome.status != "completed":
        raise SystemExit(f"the seeded tuning batch ended as {outcome.status}")


def _shoot(port: int, shots: dict[str, str]) -> None:
    """Navigate the way a person does: load once, then click.

    Deep links now work -- a 404 falls back to the UI shell -- so `goto` would
    reach most pages. Two reasons this still clicks. `/memory`, `/sources` and
    `/graph` are *also* API routes, and FastAPI resolves those before the
    static mount, so a hard load of one photographs a JSON body regardless of
    the fallback. And clicking is what a person does, so it exercises the
    router the way the app is actually used.
    """
    from playwright.sync_api import sync_playwright

    TARGET.mkdir(parents=True, exist_ok=True)
    # An explicit executable when one is provisioned: a pinned Playwright and
    # a pre-installed browser can disagree about the build number, and
    # downloading one in CI is both slow and a network dependency this repo
    # does not otherwise have.
    provisioned = Path("/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    launch: dict[str, object] = {}
    if provisioned.exists():
        launch["executable_path"] = str(provisioned)

    with sync_playwright() as play:
        browser = play.chromium.launch(**launch)
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)
        page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
        page.wait_for_timeout(1500)

        # Ask something, so the notebook shows an answer rather than an empty
        # composer. A demo that demos.
        composer = page.locator(".composer textarea")
        composer.click()
        composer.fill("What restarts nightly, and who should be paged if it fails?")
        composer.press("Enter")
        page.wait_for_timeout(4000)

        for name, link in shots.items():
            if link:
                page.get_by_role("link", name=link, exact=True).click()
            else:
                page.get_by_role("link", name="Notebook", exact=True).click()
            # The panes hydrate from several queries; a settled network is not
            # the same as a settled layout.
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1200)
            if name == "memory":
                # Open the layers. A screenshot of collapsed rows shows the
                # triage view and hides the thing that makes a proposal
                # reviewable at all: what was asked, what came back, the
                # spans behind it, and what is behind one of those keys.
                page.get_by_role("button", name="Show evidence").first.click()
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(600)
                page.get_by_role("button", name="Trace").first.click()
                page.wait_for_timeout(500)
                # Layer 4, and the reason to drive it here rather than assert
                # it in a unit test: this is the only place the whole chain
                # runs against a real corpus -- a search records a result id,
                # a rule carries that id into a candidate, and the panel
                # resolves it back to the indexed text. A mock of
                # /nodes/content would prove none of that.
                keys = page.get_by_title("the content-addressed ids this call returned")
                if keys.count():
                    keys.first.click()
                    page.wait_for_timeout(400)
                    content_key = page.locator(".sidepanel__key")
                    if content_key.count():
                        content_key.first.click()
                        page.wait_for_load_state("networkidle")
                        page.wait_for_timeout(600)
                        resolved = page.locator(".sidepanel__resolved pre").count()
                        print(f"  panel: key selected, resolved={resolved}")
                        if not resolved:
                            raise SystemExit(
                                "the selected content key did not resolve to text -- "
                                "the recorded result ids and /nodes/content disagree"
                            )
                else:
                    raise SystemExit("no content keys on the trace -- nothing to select")
            if name == "graph":
                # Expand before photographing.
                #
                # Arriving from the Notebook, the canvas is still filtered to
                # the nodes that one answer surfaced -- which is the right
                # default there ("what did this answer touch") and the wrong
                # picture of a *knowledge graph*: four nodes, a couple of
                # edges, and no structure. The shot that belongs beside the
                # word "graph" is the graph.
                #
                # So: clear the answer filter, ask for the whole thing, and
                # lay it out with force rather than concentric rings -- rings
                # order nodes by degree and say nothing about what is joined
                # to what, while a force layout is where clusters and hubs
                # actually become visible.
                clear = page.get_by_role("button", name="Clear ✕")
                if clear.count():
                    clear.first.click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(600)
                page.get_by_role("button", name="Show all", exact=True).click()
                page.get_by_label("Graph layout").select_option("cose")
                page.wait_for_load_state("networkidle")
                # A force layout is iterative: the network is on screen long
                # before it has settled, and a shot taken early is a picture
                # of a simulation mid-tick. This is the one page whose wait is
                # about the physics rather than the fetch.
                page.wait_for_timeout(9000)
                drawn = page.locator(".graph-badge").first.inner_text()
                print(f"  graph: {drawn}")
                # The badge is the node count the canvas actually drew, so it
                # is the one thing that distinguishes "the whole graph" from
                # "the four nodes an answer touched" -- which is exactly the
                # regression this branch exists to prevent, and exactly the
                # kind a screenshot fails silently at.
                count = int("".join(ch for ch in drawn if ch.isdigit()) or 0)
                if count < MIN_GRAPH_NODES:
                    raise SystemExit(
                        f"the graph canvas drew {count} nodes -- expanding it did not "
                        f"take, and the shot would be a subselection again"
                    )
            if name == "evaluation":
                # Open one metric. A page of tiles shows the health vector and
                # hides the thing that makes a number arguable: the formula,
                # the numbers substituted into it, what was excluded and why,
                # and the sentence saying what it does *not* support.
                tile = page.locator(".eval-tiles__item").first
                tile.click()
                # The calculation is fetched, not read out of the report, so a
                # settled network is what to wait for rather than a timer.
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(800)
                if not page.locator(".eval-detail").count():
                    raise SystemExit(
                        "clicking a health tile opened no calculation -- the page cannot "
                        "resolve an aggregate to the formula behind it"
                    )
                pass
            out = TARGET / f"{name}.png"
            page.screenshot(path=str(out))
            print(f"  {out.relative_to(REPO)}  ({out.stat().st_size // 1024} KiB)")
            if name == "evaluation":
                # After the page shot, never before: scrolling to the gates
                # moves the viewport, and scrolling back is unreliable because
                # the scroll container is the layout's main pane rather than
                # the window. Shooting the page first leaves it framed on the
                # header, which is where the title and the Run button are.
                #
                # The gates get their own frame because they are the part of
                # the page that is deliberately *not* a score: a crop of the
                # tiles alone shows the vector and hides the thing that sits
                # outside its arithmetic.
                gates = page.locator("section.eval-section").filter(has_text="Hard gates").first
                if not gates.count():
                    raise SystemExit("no gates section on the page")
                gates.scroll_into_view_if_needed()
                page.wait_for_timeout(400)
                shot = TARGET / "evaluation-gates.png"
                gates.screenshot(path=str(shot))
                print(f"  {shot.relative_to(REPO)}  ({shot.stat().st_size // 1024} KiB)")
        browser.close()


def _shoot_tuning(port: int) -> None:
    """The tuning plane's three panels, each cropped to what it is showing.

    Separate from the full-page shot because these are the pieces a reader
    needs to see close up: the stage histogram is the finding, the sweeps are
    the evidence, and the gates are why a real improvement was refused.
    """

    from playwright.sync_api import sync_playwright

    provisioned = Path("/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    launch: dict[str, object] = {}
    if provisioned.exists():
        launch["executable_path"] = str(provisioned)

    panels = {
        "tuning-health": 'section:has(h2:text("Live pipeline health"))',
        "tuning-diagnosis": 'section:has(h2:text("Where retrieval loses documents"))',
        "tuning-sweeps": 'section:has(h2:text("Parameter sweeps"))',
        "tuning-decision": 'section:has(h2:text("Decision"))',
        "tuning-config": 'section:has(h2:text("What this region ranks with"))',
    }
    with sync_playwright() as play:
        browser = play.chromium.launch(**launch)
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
        page.wait_for_timeout(1200)
        page.get_by_role("link", name="Tuning", exact=True).click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2500)
        # One explanation opened before the diagnosis is photographed. The
        # whole point of the catalog is that a measure and its meaning are a
        # click apart, and a screenshot of the collapsed state shows a "?"
        # rather than the thing the "?" is for.
        opener = page.locator(
            'section:has(h2:text("Where retrieval loses documents")) .explain > summary'
        )
        if opener.count():
            opener.first.click()
            page.wait_for_timeout(400)

        for name, selector in panels.items():
            panel = page.locator(selector)
            if not panel.count():
                # Loud rather than quiet: a panel that vanished because its
                # endpoint changed shape should fail the run, not silently
                # leave a stale image in the README.
                raise SystemExit(f"the tuning page has no {name!r} panel to photograph")
            panel.first.scroll_into_view_if_needed()
            page.wait_for_timeout(400)
            out = TARGET / f"{name}.png"
            panel.first.screenshot(path=str(out))
            print(f"  {out.relative_to(REPO)}  ({out.stat().st_size // 1024} KiB)")
        if errors:
            raise SystemExit(f"the tuning page raised: {errors}")
        browser.close()


def _shoot_taxonomy(port: int) -> None:
    """The Notebook's tiles with the taxonomy view open, then rearranged.

    The first frame is the default layout -- sources, the taxonomy view in its
    own column, the chat, the graph -- with the runbook the answer cites open
    in the viewer, which is what the view is for: the passage and the file it
    lives in side by side. The second is the same session after a person has
    folded the sources to their spine and moved the taxonomy view above the
    graph, by keyboard on its grip, which is the same move a drag makes.
    """

    from playwright.sync_api import sync_playwright

    provisioned = Path("/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    launch: dict[str, object] = {}
    if provisioned.exists():
        launch["executable_path"] = str(provisioned)

    with sync_playwright() as play:
        browser = play.chromium.launch(**launch)
        page = browser.new_page(viewport=WIDE_VIEWPORT, device_scale_factor=2)
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
        page.wait_for_timeout(1200)

        composer = page.locator(".composer textarea")
        composer.click()
        composer.fill("What restarts nightly, and who should be paged if it fails?")
        composer.press("Enter")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)

        page.get_by_role("button", name="Taxonomy", exact=True).click()
        # The view loads lazily and lists the sources before it is clickable:
        # a row found mid-hydration is replaced under the click.
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        rows = page.locator(".browser__rows")
        for name in ("handbook", "runbooks", "kestrel.md"):
            rows.locator(".browser-row", has_text=name).first.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(700)
        if not page.locator(".browser__viewer").filter(has_text="Kestrel Runbook").count():
            raise SystemExit("the taxonomy view did not open the runbook in its viewer")
        page.wait_for_timeout(800)
        out = TARGET / "taxonomy.png"
        page.screenshot(path=str(out))
        print(f"  {out.relative_to(REPO)}  ({out.stat().st_size // 1024} KiB)")

        page.get_by_role("button", name="Collapse Sources", exact=True).click()
        page.wait_for_timeout(400)
        grip = page.locator('[data-tile-grip="taxonomy"]')
        grip.focus()
        # inner -> center -> right (appended below the graph), then up above it.
        for key in ("ArrowRight", "ArrowRight", "ArrowUp"):
            page.keyboard.press(key)
            page.wait_for_timeout(300)
        if not page.locator('[data-column="right"] > [data-tile="taxonomy"]').count():
            raise SystemExit("the taxonomy tile did not reach the right column")
        page.wait_for_timeout(800)
        out = TARGET / "taxonomy-tiles.png"
        page.screenshot(path=str(out))
        print(f"  {out.relative_to(REPO)}  ({out.stat().st_size // 1024} KiB)")
        if errors:
            raise SystemExit(f"the Notebook raised: {errors}")
        browser.close()


def _shoot_run_states(port: int, config) -> None:
    """Photograph the two states a batch reaches when a process goes away.

    These are the states the durable run row exists for, and neither can be
    staged by driving the UI: one is a batch mid-flight and the other is a
    container that stopped. Both are produced here by writing the *same rows*
    the runner writes -- `open_run` and `heartbeat_run` for the first,
    `reclaim_interrupted_runs` for the second -- rather than by mocking an API
    response, so what is photographed is what the page renders for real state.

    Deliberately last: it leaves the region with an interrupted run, which is
    not the state the other screenshots want behind them.
    """

    from playwright.sync_api import sync_playwright

    from pheasant.evaluation import store as evaluation_store
    from pheasant.evaluation.runner import reclaim_interrupted_runs
    from pheasant.persistence.paths import StatePaths
    from pheasant.persistence.state_store import StateStore

    paths = StatePaths.from_config(config)
    state = StateStore.from_config(config, paths.sqlite)
    provisioned = Path("/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    launch: dict[str, object] = {}
    if provisioned.exists():
        launch["executable_path"] = str(provisioned)

    try:
        snapshot = evaluation_store.latest_run(state, config.knowledge_base_id)
        snapshot_id = str(snapshot["snapshot_id"]) if snapshot else "kb-demo"
        with sync_playwright() as play:
            browser = play.chromium.launch(**launch)
            page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)

            # --- a batch in flight -----------------------------------------
            # Real timestamps, not decorative ones. A cosmetic future clock
            # made the heartbeat un-staleable, so the reclaim below found
            # nothing -- which is the product behaving correctly on data that
            # could not happen.
            from pheasant.evaluation.contracts import utc_now

            now = utc_now()
            evaluation_store.open_run(
                state,
                run_id="run-inflight",
                kb_id=config.knowledge_base_id,
                snapshot_id=snapshot_id,
                started_at=now,
                mode="current_state",
                config_digest="demo",
                owner="indexer-0:31",
                total_units=36,
            )
            evaluation_store.heartbeat_run(
                state,
                run_id="run-inflight",
                now=now,
                phase="replay",
                detail="anchor/B3",
                completed_units=15,
            )
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            page.wait_for_timeout(1200)
            page.get_by_role("link", name="Effectiveness", exact=True).click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1200)
            if not page.locator(".eval-progress__bar").count():
                raise SystemExit("no progress bar for a running batch")
            out = TARGET / "evaluation-running.png"
            page.locator(".eval-progress").first.screenshot(path=str(out))
            print(f"  {out.relative_to(REPO)}  ({out.stat().st_size // 1024} KiB)")

            # --- the container stopped -------------------------------------
            # Reclaimed by the product, not by an UPDATE written here: the
            # point of the picture is what `reclaim_interrupted_runs` leaves
            # behind, including how far the batch got.
            time.sleep(2)
            reclaimed = reclaim_interrupted_runs(
                state, config.knowledge_base_id, stale_after_seconds=1
            )
            if "run-inflight" not in reclaimed:
                raise SystemExit("the stalled run was not reclaimed; nothing to photograph")
            # Back to `/` and click, never `reload()`. A reload would work now
            # that deep links fall back to the shell, but clicking is what a
            # person does and it keeps this identical to `_shoot`.
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            page.wait_for_timeout(1200)
            page.get_by_role("link", name="Effectiveness", exact=True).click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1200)
            if not page.locator(".eval-progress__note").count():
                raise SystemExit("no explanation rendered for an interrupted run")
            out = TARGET / "evaluation-interrupted.png"
            page.locator(".eval-progress").first.screenshot(path=str(out))
            print(f"  {out.relative_to(REPO)}  ({out.stat().st_size // 1024} KiB)")
            browser.close()
    finally:
        state.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true", help="Leave the seeded region on disk for inspection."
    )
    parser.add_argument(
        "--no-benchmark",
        action="store_true",
        help=(
            "Skip the SciFact corpus. The retrieval and tuning panels then run "
            "over the nine-document narrative corpus, where every query is "
            "served whatever the ranking does — useful for a fast local run, "
            "and not what should be published."
        ),
    )
    parser.add_argument(
        "--only",
        choices=["all", "taxonomy"],
        default="all",
        help="Regenerate one group of screenshots; the region is seeded the same either way.",
    )
    args = parser.parse_args()

    dist = REPO / "ui" / "dist"
    if not (dist / "index.html").exists():
        raise SystemExit(
            "ui/dist is missing. Build it first:\n  npm --prefix ui ci && npm --prefix ui run build"
        )

    import uvicorn

    from pheasant.api.app import create_app
    from pheasant.config.schema import PheasantConfig

    root = Path(tempfile.mkdtemp(prefix="pheasant-shots-"))
    try:
        benchmark = None if args.no_benchmark else _fetch_benchmark(root)
        config_path = _seed(root, benchmark)
        config = PheasantConfig.model_validate(
            yaml.safe_load(config_path.read_text(encoding="utf-8"))
        )
        # Two app instances over one state directory, on purpose: the MCP
        # SDK's session manager can only run its lifespan once per instance,
        # and seeding through a TestClient consumes that. The served app is a
        # fresh one reading what the first wrote -- which is also a fair
        # reflection of what a browser sees, since it is a cold process.
        _drive(create_app(config, config_path=str(config_path)), benchmark)
        app = create_app(config, config_path=str(config_path))

        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)
        else:
            raise SystemExit("the server did not start")

        print(f"Serving the seeded region on 127.0.0.1:{port}")
        if args.only == "all":
            _shoot(
                port,
                {
                    "notebook": "Notebook",
                    "memory": "Memory",
                    "evaluation": "Effectiveness",
                    "tuning": "Tuning",
                    "sources": "Sources",
                    "graph": "Graph",
                },
            )
            _shoot_tuning(port)
        _shoot_taxonomy(port)
        if args.only == "all":
            _shoot_run_states(port, config)
        server.should_exit = True
        thread.join(timeout=10)
        print(json.dumps({"screenshots": sorted(p.name for p in TARGET.glob("*.png"))}))
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

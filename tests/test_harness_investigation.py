"""Acceptance fixtures A01-A04: grounding, search coverage, direct reads, long summaries."""
# ruff: noqa: E501, F811

from __future__ import annotations

import os
import re

import pytest

from tests.harness_support import build_region, events, git_repo, region, write_tree  # noqa: F401

pytest.importorskip("pheasant_harness")

from pheasant_harness import investigate  # noqa: E402
from pheasant_harness.common import Unsupported  # noqa: E402


def _bm25_searches(metrics) -> float:
    return sum(
        metrics.REGISTRY.value("pheasant_retrieval_lexical_total", index="pg_textsearch", path=path)
        or 0.0
        for path in ("index", "widened", "unfenced")
    )


def _bm25_failures(metrics) -> float:
    return (
        metrics.REGISTRY.value(
            "pheasant_retrieval_lexical_total", index="tsvector", path="bm25_failed"
        )
        or 0.0
    )


def test_a01_stale_index_yields_to_the_current_revision(tmp_path, monkeypatch):
    """The index holds an old policy and a deleted API; the runtime cites what is there now."""
    sources = "\n  - name: ops-kb\n    type: markdown_folder\n    path: \"{workspace}/kb\"\n    include: ['**/*.md', '**/*.py']\n"
    r = build_region(tmp_path, monkeypatch, sources)
    from pheasant.telemetry import metrics

    metrics.register_default_metrics("test")  # the server registers these at start
    before, failures = _bm25_searches(metrics), _bm25_failures(metrics)
    kb = write_tree(
        r.workspace / "kb",
        {
            "discount-policy.md": "# Discount policy\n\nOrders with a discount above 10% need review.\n",
            "billing.py": "def legacy_charge(order):\n    return order.total\n",
        },
    )
    r.tools.sync_source(r.config.knowledge_base_id, "ops-kb")
    # The world moves on; the index does not.
    write_tree(
        kb,
        {
            "discount-policy.md": "# Discount policy\n\nOrders with a discount above 15% need review.\n",
            "billing.py": "def charge(order, currency):\n    return round(order.total, 2)\n",
        },
    )
    reg = r.register(kb)
    status = r.run(
        workload="answer",
        question="What discount needs review and which function charges an order?",
        inputs=[reg],
        identifiers=["discount above"],
        symbols=["charge"],
    )
    assert status["state"] == "succeeded", status["result"]
    detail = r.detail(status["run_id"])
    discovered = [s for s in detail["steps"] if s["action"] == "discover"][0]
    stale = [
        c for c in discovered["observation"]["candidates"] if c["path"] == "discount-policy.md"
    ]
    assert stale and stale[0]["indexed_digest"] != detail["revisions"][reg]["revision"]
    answer = r.text(status["result"]["candidates"]["primary"])
    assert "15%" in answer and "10%" not in answer
    assert "`charge` is defined at `billing.py`" in answer and "legacy_charge" not in answer
    assert "## Discrepancies" in answer and "older copy of `discount-policy.md`" in answer
    cited = {c["criterion_id"]: c["outcome"] for c in detail["verifications"]}
    assert cited == {
        "citations-resolve": "pass",
        "claims-grounded": "pass",
        "coverage-stated": "pass",
    }
    # Discovery ran on the region's default lexical index: BM25 from pg_textsearch
    # on PostgreSQL 17, not a stock-Postgres fallback.
    assert r.config.storage.lexical_index == "pg_textsearch"
    assert _bm25_searches(metrics) > before
    assert _bm25_failures(metrics) == failures  # never fell back to tsvector
    indexes = r.harness.state.rows(
        "SELECT indexdef FROM pg_indexes WHERE tablename='chunks_fts' AND indexdef ILIKE '%bm25%'"
    )
    assert indexes, "chunks_fts has no pg_textsearch BM25 index"


def _coverage_tree(root):
    write_tree(
        root,
        {
            ".gitignore": "build/\n",
            "build/out.txt": "SECRET_KEY = ignored\n",
            ".env": "SECRET_KEY=hidden\n",
            "dist/app.min.js": "var SECRET_KEY='generated';\n",
            "blob.bin": b"\x00\x01SECRET_KEY\x00",
            "a.py": "SECRET_KEY = 1\nprint(SECRET_KEY)  # SECRET_KEY twice on a line\n",
            "b.py": "x = 'SECRET_KEY'\n",
            "c.md": "No key here.\n",
            "d.py": "SECRET_KEY\nSECRET_KEY\nSECRET_KEY\n",
        },
    )
    os.symlink(root / "missing-target", root / "dangling.py")
    os.symlink("/etc/hostname", root / "escape.py")


def _all_pages(resolver, revision, pattern, options):
    pages, cursor = [], None
    while True:
        page = investigate.grep(resolver, revision, pattern, options, cursor)
        pages.append(page)
        cursor = page["next_cursor"]
        if not cursor:
            return pages


def test_a02_capped_grep_resumes_exactly_and_accounts_for_what_it_skipped(region):
    root = region.workspace / "repo"
    _coverage_tree(root)
    reg_id = region.register(root)
    revision = region.harness.registry.resolve(reg_id, "local")
    resolver = region.harness.registry.resolver(region.harness.registry.get(reg_id))
    pages = _all_pages(resolver, revision, "SECRET_KEY", {"max_matches": 2})
    matches = [(m["path"], m["line"], m["column"]) for p in pages for m in p["matches"]]
    expected = [
        ("a.py", 1, 1),
        ("a.py", 2, 7),
        ("a.py", 2, 22),
        ("b.py", 1, 6),
        ("d.py", 1, 1),
        ("d.py", 2, 1),
        ("d.py", 3, 1),
        ("dist/app.min.js", 1, 5),
    ]
    assert matches == expected  # nothing skipped, nothing duplicated across pages
    assert all(p["outcome"] == "partial" for p in pages[:-1]) and pages[-1]["outcome"] == "success"
    assert "No count or absence follows" in pages[0]["coverage"]["statement"]
    final = pages[-1]["coverage"]
    assert final["complete"] and final["matches_scanned"] == 8 and final["files_with_matches"] == 4
    assert (
        final["hidden_skipped"] == 2
        and final["ignored_skipped"] == 1
        and final["binary_skipped"] == 1
    )
    assert final["generated_scanned"] == 1
    assert {u["path"] for u in final["unreadable"]} == {"dangling.py", "escape.py"}
    assert final["statement"].startswith(
        "8 occurrences of 'SECRET_KEY' in 4 of 5 scanned text files"
    )
    # An absence statement names exactly the scope it covers.
    absent = investigate.grep(resolver, revision, "NOPE_KEY", {"generated": "skip"})
    assert absent["coverage"]["statement"].startswith(
        "No occurrences of 'NOPE_KEY' in these 4 scanned"
    )
    assert (
        "1 generated" in absent["coverage"]["statement"]
        and "2 unreadable" in absent["coverage"]["statement"]
    )
    with pytest.raises(Exception, match="different revision or query"):
        investigate.grep(resolver, revision, "OTHER", {"max_matches": 2}, pages[0]["next_cursor"])


def test_a02_the_same_contract_holds_on_a_pinned_git_commit(region):
    root = region.workspace / "gitrepo"
    first = git_repo(root, {"a.py": "TOKEN = 1\n", "b.py": "TOKEN\nTOKEN\n"})
    git_repo(root, {"a.py": "nothing\n"}, "second")
    reg_id = region.register(root)
    old = region.harness.registry.resolve(reg_id, "local", first)
    new = region.harness.registry.resolve(reg_id, "local")
    assert old.revision == first and new.revision != first and not old.time_bound
    resolver = region.harness.registry.resolver(region.harness.registry.get(reg_id))
    assert (
        sum(len(p["matches"]) for p in _all_pages(resolver, old, "TOKEN", {"max_matches": 1})) == 3
    )
    assert (
        sum(len(p["matches"]) for p in _all_pages(resolver, new, "TOKEN", {"max_matches": 1})) == 2
    )
    assert investigate.compare(resolver, old, new)["changed"] == ["a.py"]


def test_a03_no_discovery_direct_read_and_an_honest_coverage_limit(region):
    known = write_tree(
        region.workspace / "runbook",
        {"restart.md": "# Restart\n\nRestart the api with `make restart-api`.\n"},
    )
    archive = write_tree(
        region.workspace / "archive", {f"memo-{i}.md": f"memo {i}\n" for i in range(5)}
    )
    known_id = region.register(known)
    pointer = region.register(archive, operations=["resolve", "read"])
    with pytest.raises(Unsupported, match="enumerate is not offered"):
        region.harness.registry.require(pointer, "local", "enumerate")
    status = region.run(
        workload="answer",
        question="How do I restart the api, and does any memo in the archive mention restarts?",
        inputs=[known_id, pointer],
        identifiers=["restart-api"],
        discovery="off",
    )
    assert status["state"] == "succeeded", status["result"]
    detail = region.detail(status["run_id"])
    assert not [s for s in detail["steps"] if s["action"] == "discover"]
    answer = region.text(status["result"]["candidates"]["primary"])
    assert "make restart-api" in answer
    assert "`archive` is registered for resolve, read only" in answer
    assert "nothing here is a statement about its whole content" in answer


def test_a04_long_packet_keeps_the_appendix_exception_through_compaction(region):
    filler = "\n".join(
        f"Section {i}: routine handling applies to standard orders." for i in range(1, 260)
    )
    packet = write_tree(
        region.workspace / "packet",
        {
            "policy-v1.md": "# Shipping policy v1\n\nStatus: superseded by policy-v2.md\nEffective: 2025-01-01\n"
            "Free shipping over 50 USD.\n",
            "policy-v2.md": "# Shipping policy v2\n\nEffective: 2026-03-01\nFree shipping over 75 USD.\n"
            f"{filler}\n\n## Appendix C\n\n- Exception: orders shipped to Alaska are excluded from "
            "free shipping.\n",
        },
    )
    reg = region.register(packet)
    coordinator = region.coordinator("worker-a")
    status = coordinator.start(
        __import__("pheasant_harness.models", fromlist=["RunRequest"]).RunRequest(
            workload="summarize",
            question="What shipping terms apply now, and with what exceptions?",
            inputs=[reg],
            context_chars=600,
        ),
        actor="local",
        client_key="a04",
    )
    run_id = status["run_id"]
    # Lose the first worker after its first committed round; another finishes from the journal.
    coordinator.tick(run_id, max_rounds=3)
    region.harness.runs.expire_lease(run_id)
    final = region.coordinator("worker-b").drive(run_id)
    assert final["state"] == "succeeded", final["result"]
    assert events(region, run_id, "ledger.compacted")
    detail = region.detail(run_id)
    reopened = [s for s in detail["steps"] if s["args"].get("purpose") == "reopen before quoting"]
    assert reopened and all(s["status"] == "succeeded" for s in reopened)
    summary = region.text(final["result"]["candidates"]["primary"])
    assert "effective 2026-03-01" in summary and "`policy-v1.md` is superseded" in summary
    exception = [line for line in summary.splitlines() if "Alaska" in line][0]
    evidence = {e["id"]: e for e in detail["evidence"]}
    cited = evidence[re.search(r"\[(E[0-9a-f]{8})\]", exception).group(1)]
    assert cited["locator"]["path"] == "policy-v2.md" and "Alaska" in cited["extract"]
    assert cited["locator"]["lines"][0] == cited["locator"]["lines"][1]  # the exact reopened line
    assert "## Conflicts" in summary and "Processed 2 of 2 entries" in summary


def test_an_answer_quotes_the_matched_sentence_inside_its_citation(region):
    docs = write_tree(
        region.workspace / "manual",
        {
            "exceptions.md": "# Manual\n\nEffective: 2026-02-01\n\n"
            "Orders with a discount above 15% require an exception badge. The discount is "
            "compared before rounding.\n"
        },
    )
    status = region.run(
        workload="answer",
        question="When is an exception badge required?",
        inputs=[region.register(docs)],
        identifiers=["exception badge"],
        discovery="off",
    )
    assert status["state"] == "succeeded", status["result"]
    answer = region.text(status["result"]["candidates"]["primary"])
    assert "require an exception badge [E" in answer and "compared before rounding" not in answer

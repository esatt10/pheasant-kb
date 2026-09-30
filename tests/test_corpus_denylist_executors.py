"""The corpus denylist holds whichever executor prepares the files.

`readiness.corpus_denylist` is enforcement: a listed path must never become
retrievable. The thread executor refused it before reading; the *process*
executor's entry point skipped the check and went straight to the sha256
test, so the same file under ``file_executor: process`` was indexed. The
refusal is decided on the caller's thread now, before any executor is handed
the item, and this runs every local executor against the same corpus.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.registry.source_registry import SourceRegistry
from pheasant.sync.engine import SyncEngine


@pytest.mark.parametrize("executor", ["thread", "process"])
@pytest.mark.parametrize("workers", [1, 4])
def test_a_denylisted_file_is_refused_by_every_executor(
    tmp_path: Path, executor: str, workers: int
) -> None:
    docs = tmp_path / "workspace" / "docs"
    docs.mkdir(parents=True)
    for index in range(6):
        (docs / f"note{index}.md").write_text(f"# Note {index}\n\nordinary text {index}\n")
    (docs / "answer_key.md").write_text("# Answers\n\nTHE BENCHMARK ANSWERS\n")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "denylist",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path / "workspace"),
                "exports_path": str(tmp_path / "exports"),
            },
            "readiness": {"corpus_denylist": ["answer_key.md"]},
            "sync": {"concurrency": {"file_executor": executor, "max_parallel_files": workers}},
            "sources": [{"name": "docs", "type": "markdown_folder", "path": str(docs)}],
        }
    )
    engine = SyncEngine(config)
    try:
        engine.paths.ensure()
        engine.state.migrate()
        SourceRegistry(engine.config, engine.state).initialize()
        result = engine.sync_source("docs", "full")
        assert result.indexed_artifacts == 6
        assert [item["relative_path"] for item in result.details["refused"]] == ["answer_key.md"]
        held = engine.state.rows(
            "SELECT relative_path FROM artifacts WHERE relative_path=?", ("answer_key.md",)
        )
        assert held == []
        # Nor is its text reachable through the index.
        hits = engine.state.rows(
            "SELECT chunk_id FROM chunks_fts WHERE text LIKE ?", ("%ANSWERS%",)
        )
        assert hits == []
    finally:
        engine.close()

"""Every cited passage can be opened where it is.

A passage found through the graph arm is cited by its chunk *node*, which used
to carry neither ``chunk_id`` nor ``relative_path``. The web UI opens a
citation by source, path and chunk, so those chips opened nothing: with the
taxonomy view showing, clicking one switched the panel to the Node tab and the
document the reader had open disappeared.

Found in a browser, by clicking each chip of one answer in turn: the first
opened and highlighted, the next two did not.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.assistant.citation_targets import locate
from pheasant.config.schema import PheasantConfig

CHUNK = "chunk:notes:birds/pheasants.md:sha256=8a16:chunk=0010"


def test_a_chunk_node_names_its_own_file_and_passage():
    found = locate(
        {"node_id": CHUNK, "chunk_id": None, "relative_path": None, "source_id": "notes"}
    )
    assert found["chunk_id"] == CHUNK and found["relative_path"] == "birds/pheasants.md"
    # A path may contain ':' (and even look like a digest marker's start).
    odd = "chunk:notes:a:b/c:sha256.md:sha256=ff:chunk=0001"
    assert locate({"node_id": odd, "source_id": "notes"})["relative_path"] == "a:b/c:sha256.md"


def test_what_is_already_named_or_is_not_a_document_is_left_alone():
    text_hit = {
        "node_id": "file:notes:a.md:branch=none",
        "chunk_id": "chunk:notes:a.md:sha256=1:chunk=0000",
        "relative_path": "a.md",
        "source_id": "notes",
    }
    assert locate(dict(text_hit)) == text_hit
    for untouched in (
        {"node_id": "heading:notes:a.md:intro", "source_id": "notes"},
        {"node_id": CHUNK, "source_id": None},
        {"node_id": CHUNK, "source_id": "other"},  # a chunk of another source's id
        {"node_id": CHUNK, "source_id": "notes", "memory": {"scope": "org"}},
    ):
        assert locate(dict(untouched)) == untouched


def _corpus(root: Path) -> None:
    """The two files the bug was found on: one long enough to have many chunks."""

    for name in ("ws/notes/birds", "ws/notes/fish", "state", "exports"):
        (root / name).mkdir(parents=True)
    sections = []
    for n in range(1, 9):
        body = " ".join(f"Filler sentence {n}.{k} about nothing in particular." for k in range(40))
        sections.append(f"## Section {n}\n\n{body}")
    filler = "\n\n".join(sections)
    roosting = (
        "## Roosting\n\nA pheasant roosts in trees at night to avoid foxes, "
        "and forages on the ground by day."
    )
    appendix = filler.replace("Section", "Appendix")
    (root / "ws/notes/birds/pheasants.md").write_text(
        f"# Pheasants\n\n{filler}\n\n{roosting}\n\n{appendix}", encoding="utf-8"
    )
    (root / "ws/notes/fish/trout.md").write_text(
        "# Trout\n\nTrout hold in cold, fast water behind rocks and feed on drifting insects.\n",
        encoding="utf-8",
    )


def test_every_passage_an_answer_cites_can_be_opened(tmp_path: Path):
    _corpus(tmp_path)
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "kb",
                "defaults": "standalone",
                "workspace_root": str(tmp_path / "ws"),
                "state_path": str(tmp_path / "state"),
                "exports_path": str(tmp_path / "exports"),
            },
            "assistant": {"provider": "none"},
            "sources": [
                {
                    "name": "notes",
                    "type": "document_folder",
                    "path": "notes",
                    "include": ["**/*.md"],
                }
            ],
        }
    )
    config.artifacts.index_on_save = False
    app = create_app(config, config_path=str(tmp_path / "pheasant.yaml"))
    with TestClient(app) as client:
        assert client.post("/sync/notes").status_code == 200
        answer = client.post(
            "/assistant/chat", json={"question": "where do pheasants roost at night?"}
        ).json()
        by_node = [c for c in answer["citations"] if str(c["node_id"]).startswith("chunk:")]
        # The case this guards is in the answer: passages cited by their node.
        assert by_node, answer["citations"]
        for citation in by_node:
            assert citation["chunk_id"] == citation["node_id"], citation
            assert citation["relative_path"] == "birds/pheasants.md", citation
            # ...and the viewer finds that passage in that file.
            opened = client.get(
                "/documents/content",
                params={
                    "source": citation["source_id"],
                    "path": citation["relative_path"],
                    "chunk_id": citation["chunk_id"],
                },
            )
            assert opened.status_code == 200, opened.text
            assert opened.json()["focus"]["chunk_id"] == citation["chunk_id"]
    app.state.engine.close()

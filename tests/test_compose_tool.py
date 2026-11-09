"""compose_artifact: compose over MCP as a followable job with a compact result.

The operation is `services.assistant.answer` with intent "compose" (driven in
tests/test_compose_answers.py and tests/test_decisions_api.py); what is tested
here is what the MCP tool adds — the job, the artifact payload, the refusals.
"""

from __future__ import annotations

import time

import pytest

from pheasant.jobs import JobRegistry
from pheasant.mcp_server.compose_tools import ComposeTools, artifact_payload

ANSWER = (
    "This builds a resource group and a vault.\n\n"
    "### `providers.tf`\n\n```hcl\nterraform {}\n```\n\n"
    '**main.tf**\n```hcl\n# [1]\nresource "azurerm_resource_group" "rg" {}\n```\n\n'
    "```\nunnamed\n```\n\n"
    "- The passages do not give the vault's SKU; fill in var.sku."
)
RESULT = {
    "answer": ANSWER,
    "answer_mode": "llm",
    "route": {"intent": "compose"},
    "server_processing_ms": 123456,
    "citations": [
        {
            "index": 1,
            "source_id": "tf-core",
            "relative_path": "r/resource_group.md",
            "heading_path": "Example Usage",
            "snippet": "x" * 5000,
        },
        {"index": 2, "source_id": "tf-core", "relative_path": "r/unused.md"},
    ],
    "steps": [
        {"name": "classify", "detail": "composed artifact"},
        {"name": "history_rewrite", "detail": "skipped"},
        {"name": "review", "detail": "nothing to correct"},
    ],
    "runtime_timings": {"big": "y" * 50_000},
}


def test_the_payload_is_the_artifact_without_the_evidence() -> None:
    payload = artifact_payload(RESULT)

    assert [f["name"] for f in payload["files"]] == ["providers.tf", "main.tf", "block-3"]
    assert payload["files"][1]["content"].startswith("# [1]\nresource")
    assert payload["files"][0]["language"] == "hcl" and payload["files"][2]["language"] is None
    assert payload["summary"] == "This builds a resource group and a vault.", "no file heading"
    assert payload["not_covered"] == "- The passages do not give the vault's SKU; fill in var.sku."
    assert payload["citations"] == [
        {"index": 1, "source": "tf-core", "path": "r/resource_group.md", "section": "Example Usage"}
    ]
    assert [s["step"] for s in payload["trace"]] == ["classify", "review"]
    assert payload["server_seconds"] == 123.5
    assert "runtime_timings" not in payload and len(str(payload)) < 2000


class _Facade(ComposeTools):
    def __init__(self, result: dict) -> None:
        self.jobs = JobRegistry()
        self.result = result
        self.asked: list[dict] = []

    def _require_knowledge_base(self, knowledge_base):
        if knowledge_base != "kb":
            raise ValueError(f"Unknown knowledge base: {knowledge_base}")

    def ask_knowledge_base(self, knowledge_base, question, **kwargs):
        on_step = kwargs.pop("on_step", None)
        if on_step is not None:
            from types import SimpleNamespace

            on_step(SimpleNamespace(name="review", detail="nothing to correct"))
        self.asked.append({"question": question, **kwargs})
        return self.result


def _settle(tools: _Facade, job_id: str) -> dict:
    for _ in range(200):
        job = tools.jobs.get(job_id)
        if job and job["status"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("the job never finished")


def test_a_composition_runs_as_a_job_and_get_job_returns_the_artifact() -> None:
    tools = _Facade(RESULT)
    started = tools.compose_artifact("kb", "Write it", source_names=["tf-core"])

    assert started["kind"] == "compose" and started["job_id"] == started["id"]
    assert "get_job" in started["poll"]
    job = _settle(tools, started["job_id"])
    assert job["status"] == "succeeded" and job["result"]["files"][1]["name"] == "main.tf"
    assert "request" not in job["result"], "the caller has it; not echoed back"
    assert job["progress"]["phase"] == "review", "the workflow's own step, not a fixed phase"
    assert tools.asked == [
        {
            "question": "Write it",
            "intent": "compose",
            "source_names": ["tf-core"],
            "source_name": None,
            "history": [],
            "principal": None,
            "principal_groups": None,
            "options": None,
            "save": None,
            "conversation_id": None,
        }
    ]


def test_wait_returns_the_artifact_directly() -> None:
    payload = _Facade(RESULT).compose_artifact("kb", "Write it", wait=True)
    assert payload["files"][0]["name"] == "providers.tf"


def test_an_answer_with_no_artifact_fails_the_job_and_says_why() -> None:
    declined = {**RESULT, "answer": "Cannot be assembled.", "answer_mode": "llm"}
    tools = _Facade(declined)
    job = _settle(tools, tools.compose_artifact("kb", "Write it")["job_id"])
    assert job["status"] == "failed" and job["error"] == "no artifact was written"

    unreachable = {**RESULT, "answer_mode": "extractive", "error": "model unreachable"}
    tools = _Facade(unreachable)
    job = _settle(tools, tools.compose_artifact("kb", "Write it")["job_id"])
    assert job["status"] == "failed" and job["error"] == "model unreachable"
    assert job["result"]["files"] == [], "quoted passages are not files"


def test_an_empty_request_and_an_unknown_region_are_refused() -> None:
    from pheasant.services.errors import InvalidRequest

    with pytest.raises(InvalidRequest, match="needs a request"):
        _Facade(RESULT).compose_artifact("kb", "  ")
    with pytest.raises(ValueError, match="Unknown knowledge base"):
        _Facade(RESULT).compose_artifact("nope", "Write it")


def test_the_server_publishes_the_tool_and_says_when_to_use_it(tmp_path) -> None:
    import asyncio

    pytest.importorskip("mcp")
    from pheasant.config.schema import PheasantConfig
    from pheasant.mcp_server.server import create_mcp_server

    server = create_mcp_server(
        PheasantConfig.model_validate(
            {
                "pheasant": {
                    "name": "compose",
                    "defaults": "standalone",
                    "state_path": str(tmp_path / "state"),
                    "exports_path": str(tmp_path / "exports"),
                    "workspace_root": str(tmp_path),
                },
                "sources": [],
            }
        )
    )
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert "compose_artifact" in tools
    assert "get_job" in tools["compose_artifact"].description
    assert "compose_artifact" in (server.instructions or "")


def test_the_compose_skill_names_only_published_tools(tmp_path) -> None:
    import asyncio
    import re

    import yaml

    pytest.importorskip("mcp")
    from pheasant.config.schema import PheasantConfig
    from pheasant.mcp_server.server import create_mcp_server
    from tests.conftest import REPO_ROOT

    skill = REPO_ROOT / ".agents" / "skills" / "pheasant-compose" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---")[1])
    assert frontmatter["name"] == "pheasant-compose"
    server = create_mcp_server(
        PheasantConfig.model_validate(
            {
                "pheasant": {
                    "name": "skill",
                    "defaults": "standalone",
                    "state_path": str(tmp_path / "state"),
                    "exports_path": str(tmp_path / "exports"),
                    "workspace_root": str(tmp_path),
                },
                "sources": [],
            }
        )
    )
    published = {tool.name for tool in asyncio.run(server.list_tools())}
    named = {
        name
        for name in re.findall(r"`([a-z]+(?:_[a-z]+)+)(?:\(|`)", text)
        if name.split("_")[0] in {"ask", "search", "describe", "get", "list", "compose"}
    }
    assert {"compose_artifact", "get_job", "list_sources"} <= named
    assert not named - published, f"the skill names tools the server lacks: {named - published}"
    metadata = yaml.safe_load((skill.parent / "agents" / "openai.yaml").read_text())
    assert "$pheasant-compose" in metadata["interface"]["default_prompt"]


def test_a_revision_reads_the_earlier_artifact_from_its_job() -> None:
    tools = _Facade(RESULT)
    first = _settle(tools, tools.compose_artifact("kb", "Write it")["job_id"])
    tools.compose_artifact("kb", "Fix the vault SKU", wait=True, previous_job=first["id"])

    revision = tools.asked[-1]
    draft = revision["options"]["compose_draft"]
    assert "### `main.tf`\n```hcl\n# [1]" in draft and "var.sku" in draft, "the draft to edit"
    assert revision["history"] == [], "not a conversation turn: an edit of the draft"
    assert revision["question"] == "Fix the vault SKU"


def test_a_revision_says_what_it_dropped_from_the_previous_artifact() -> None:
    tools = _Facade(RESULT)
    first = _settle(tools, tools.compose_artifact("kb", "Write it")["job_id"])
    tools.result = {
        **RESULT,
        "answer": ANSWER.replace('resource "azurerm_resource_group" "rg" {}', ""),
    }
    payload = tools.compose_artifact("kb", "Fix it", wait=True, previous_job=first["id"])
    assert payload["dropped_from_previous"] == ["azurerm_resource_group"]
    assert "dropped_from_previous" not in _Facade(RESULT).compose_artifact("kb", "x", wait=True)
    tools.result = {
        **RESULT,
        "answer": "The model could not be reached.",
        "answer_mode": "extractive",
    }
    failed = tools.compose_artifact("kb", "Fix it", wait=True, previous_job=first["id"])
    assert "dropped_from_previous" not in failed, "no artifact, nothing dropped"


def test_a_previous_job_the_region_does_not_hold_is_refused() -> None:
    from pheasant.services.errors import InvalidRequest

    with pytest.raises(InvalidRequest, match="send its files in history instead"):
        _Facade(RESULT).compose_artifact("kb", "Fix it", previous_job="nope")


def test_an_artifact_sent_back_in_history_is_the_draft_to_edit() -> None:
    tools = _Facade(RESULT)
    earlier = [{"question": "q0", "answer": "hi"}, {"question": "Write it", "answer": ANSWER}]
    tools.compose_artifact("kb", "Fix the SKU", wait=True, history=earlier)
    revision = tools.asked[-1]
    assert revision["options"]["compose_draft"] == ANSWER, "whole, not cut to 1,200 characters"
    assert revision["history"] == [{"question": "q0", "answer": "hi"}]

    tools.compose_artifact("kb", "Again", wait=True, history=[{"question": "q", "answer": "prose"}])
    assert tools.asked[-1]["options"] is None, "a turn with no code stays a turn"

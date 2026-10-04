import yaml

from tests.conftest import REPO_ROOT

SKILL = REPO_ROOT / ".agents" / "skills" / "pheasant-deploy" / "SKILL.md"
OPENAI_METADATA = SKILL.parent / "agents" / "openai.yaml"


def test_repository_deployment_skill_routes_blank_and_preset_workflows() -> None:
    text = SKILL.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])

    assert frontmatter["name"] == "pheasant-deploy"
    assert "Blank canvas" in text
    assert "Preset" in text
    assert "Never hand-write `pheasant.yaml`" in text
    assert "deploy/compose/docker-compose.advanced.yml" in text
    assert "deploy/compose/docker-compose.scale.yml" in text
    assert "memory_write" in text


def test_deployment_skill_has_discoverable_ui_metadata() -> None:
    metadata = yaml.safe_load(OPENAI_METADATA.read_text(encoding="utf-8"))
    assert metadata["interface"]["display_name"] == "Pheasant Deploy"
    assert "$pheasant-deploy" in metadata["interface"]["default_prompt"]


RETRIEVAL_SKILL = REPO_ROOT / ".agents" / "skills" / "pheasant-retrieval" / "SKILL.md"


def test_retrieval_skill_names_only_tools_the_server_publishes(tmp_path) -> None:
    """A skill that tells an agent to call a tool the server does not have is
    the readiness contract's stale-symbol bug, in prose. Every backticked
    snake_case call in the skill must be a registered MCP tool or prompt."""

    import asyncio
    import re

    import pytest

    pytest.importorskip("mcp")
    from pheasant.config.schema import PheasantConfig
    from pheasant.mcp_server.server import create_mcp_server

    text = RETRIEVAL_SKILL.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    assert frontmatter["name"] == "pheasant-retrieval"

    server = create_mcp_server(
        PheasantConfig.model_validate(
            {
                # Not the default /state, which only a root runner can create.
                "pheasant": {
                    "name": "skill",
                    "state_path": str(tmp_path / "state"),
                    "exports_path": str(tmp_path / "exports"),
                    "workspace_root": str(tmp_path),
                },
                "sources": [],
            }
        )
    )
    published = {tool.name for tool in asyncio.run(server.list_tools())}
    published |= {prompt.name for prompt in asyncio.run(server.list_prompts())}
    named = set(re.findall(r"`([a-z]+(?:_[a-z]+)+)(?:\(|`)", text))
    tools = {
        name
        for name in named
        if name.split("_")[0]
        in {"ask", "search", "describe", "get", "explain", "list", "record", "seal", "use"}
    }
    assert {"search_context", "get_graph_neighbors", "get_graph_slice"} <= tools
    assert not tools - published, f"the skill names tools the server lacks: {tools - published}"

    metadata = yaml.safe_load((RETRIEVAL_SKILL.parent / "agents" / "openai.yaml").read_text())
    assert "$pheasant-retrieval" in metadata["interface"]["default_prompt"]

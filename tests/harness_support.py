"""Shared fixtures for the agentic runtime harness extension's tests."""
# ruff: noqa: E501

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pheasant_harness")

from pheasant_harness import faults  # noqa: E402
from pheasant_harness.context import Harness  # noqa: E402
from pheasant_harness.coordinator import Coordinator  # noqa: E402
from pheasant_harness.models import RunRequest  # noqa: E402

from pheasant.config.loader import load_config  # noqa: E402
from pheasant.mcp_server.tools import PheasantTools  # noqa: E402


def _yaml(state: Path, exports: Path, workspace: Path, sources: str) -> str:
    lexical = (
        ""
        if os.environ.get("PHEASANT_REQUIRE_PG_TEXTSEARCH") == "1"
        else "  lexical_index: tsvector\n"
    )
    return (
        "pheasant:\n"
        "  name: harness-test\n"
        "  defaults: standalone\n"
        f'  state_path: "{state}"\n'
        f'  exports_path: "{exports}"\n'
        f'  workspace_root: "{workspace}"\n'
        "storage:\n"
        f"{lexical or '  {}'}"
        "security:\n"
        f'  allow_workspace_roots: ["{workspace}"]\n'
        "sync:\n"
        "  scheduler:\n"
        "    enabled: false\n"
        "    startup_sync: false\n"
        "artifacts:\n"
        "  index_on_save: false\n"
        f"sources:{sources or ' []'}\n"
    )


@dataclass
class Region:
    root: Path
    workspace: Path
    config: Any
    tools: PheasantTools
    harness: Harness

    @property
    def services(self):
        return self.tools.services

    def coordinator(self, worker: str = "worker-a", **kwargs) -> Coordinator:
        return Coordinator(self.harness, worker, **kwargs)

    def register(self, path: Path | str, actor: str = "local", **kwargs) -> str:
        uri = path if isinstance(path, str) else f"file://{path}"
        return self.harness.registry.register(uri, actor, **kwargs)["id"]

    def start(
        self, actor: str = "local", key: str = "k1", worker: str = "worker-a", **request
    ) -> dict:
        return self.coordinator(worker).start(RunRequest(**request), actor=actor, client_key=key)

    def run(self, actor: str = "local", key: str = "k1", **request) -> dict:
        status = self.start(actor, key, **request)
        return self.coordinator().drive(status["run_id"])

    def detail(self, run_id: str) -> dict:
        return self.coordinator("reader").detail(run_id)

    def text(self, digest: str) -> str:
        return self.harness.blobs.get_text(digest)


def build_region(tmp_path: Path, monkeypatch, sources: str = "") -> Region:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    state, exports = tmp_path / "state", tmp_path / "exports"
    state.mkdir(exist_ok=True)
    exports.mkdir(exist_ok=True)
    monkeypatch.setenv("PHEASANT_HARNESS_SANDBOX_ROOT", str(tmp_path / "sandboxes"))
    monkeypatch.setenv("PHEASANT_HARNESS_DELIVERY_ROOT", str(tmp_path / "delivered"))
    monkeypatch.setenv("PHEASANT_HARNESS_RUNNER", "off")
    monkeypatch.setenv("PHEASANT_HARNESS_NO_BROWSER", "1")
    path = tmp_path / "pheasant.yaml"
    path.write_text(
        _yaml(state, exports, workspace, sources.replace("{workspace}", str(workspace)))
    )
    config = load_config(path)
    tools = PheasantTools(config)
    return Region(tmp_path, workspace, config, tools, Harness(tools.services))


@pytest.fixture()
def region(tmp_path, monkeypatch) -> Region:
    faults.disarm()
    yield build_region(tmp_path, monkeypatch)
    faults.disarm()


def write_tree(root: Path, files: dict[str, str | bytes]) -> Path:
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    return root


def git_repo(root: Path, files: dict[str, str | bytes], message: str = "initial") -> str:
    write_tree(root, files)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    if not (root / ".git").exists():
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", message], check=True, env=env)
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def events(region: Region, run_id: str, kind: str | None = None) -> list[dict]:
    found = region.harness.runs.events(run_id, operator=True)
    return [e for e in found if kind is None or e["type"] == kind]


ORDERS = '''"""Order screen rendering."""

from dataclasses import dataclass


@dataclass
class Order:
    id: str
    discount_rate: float  # a fraction: 0.15 is 15%


def badges(order: Order) -> list[str]:
    return []


def render_order_screen(order: Order) -> str:
    items = "".join(f'<span class="badge">{b}</span>' for b in badges(order))
    return f'<section id="order-{order.id}"><h1>Order {order.id}</h1><div class="badges">{items}</div></section>'
'''

ORDER_TESTS = """from erp.orders import Order, render_order_screen


def test_screen_renders():
    assert "Order A1" in render_order_screen(Order("A1", 0.0))
"""

BADGE_EDIT = [
    {
        "path": "erp/orders.py",
        "find": "def badges(order: Order) -> list[str]:\n    return []",
        "replace": "def badges(order: Order) -> list[str]:\n"
        "    # Policy: a discount above 15% needs an exception badge.\n"
        '    return ["Exception"] if order.discount_rate > 0.15 else []',
    }
]


def erp_repo(root: Path) -> Path:
    return write_tree(
        root,
        {
            "pyproject.toml": '[project]\nname = "erp"\nversion = "0.1.0"\n',
            "README.md": "# ERP\n\nRun `python -m pytest -q` before sending a change.\n",
            "erp/__init__.py": "",
            "erp/orders.py": ORDERS,
            "tests/test_orders.py": ORDER_TESTS,
        },
    )


PYTEST = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]


def code_request(repo_id: str, *more_inputs: str, **extra) -> dict:
    request = {
        "workload": "code_change",
        "question": "Show an exception badge on orders whose discount exceeds the policy threshold.",
        "inputs": [repo_id, *more_inputs],
        "symbols": ["badges"],
        "proposals": [BADGE_EDIT],
        "test_command": PYTEST,
    }
    request.update(extra)
    return request

"""A region with three connected accounts and the fake services behind them."""
# ruff: noqa: E501

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pheasant_harness")

from pheasant_harness import connections as hconn  # noqa: E402
from pheasant_harness import faults  # noqa: E402

from pheasant.connections import resolve  # noqa: E402
from pheasant.services import connections as svc  # noqa: E402
from tests.harness_connection_fakes import (  # noqa: E402
    DRIVE_TOKEN,
    GITHUB_TOKEN,
    GRAPH_TOKEN,
    SECRETS,
    FakeDrive,
    FakeGitHub,
    FakeGraph,
    Network,
)
from tests.harness_support import ORDER_TESTS, ORDERS, Region, build_region  # noqa: E402

HANDBOOK = {
    "README.md": "# Acme handbook\n\nPolicies live under docs/.\n",
    "docs/discounts.md": "# Discounts\n\nEffective: 2026-10-01\n\nOrders with a discount above 15% need an exception badge.\n",
    "docs/shipping.md": "# Shipping\n\nFree shipping over 75 USD.\n",
    "src/billing.py": "def charge(order, currency):\n    return round(order.total, 2)\n",
    ".github/workflows/ci.yml": "on: push\n",
}
REFUND_DOC = "Refund policy\n\nRefunds within 30 days of delivery are approved automatically.\n"
RUNBOOK = "# Runbook\n\nRestart the api with `make restart-api`.\n"


@dataclass
class World:
    region: Region
    net: Network
    github: FakeGitHub
    drive: FakeDrive
    graph: FakeGraph
    policies_folder: str
    refund_doc: str

    @property
    def harness(self):
        return self.region.harness

    def allow(self, connection: str, actions, resources=(), write_resources=(), **extra) -> dict:
        body = {
            "actions": list(actions),
            "resources": list(resources),
            "write_resources": list(write_resources),
            **extra,
        }
        return self.harness.connections.set_policy(connection, body, "ops")["policy"]

    def register(self, uri: str, actor: str = "local", **kwargs) -> str:
        return self.harness.registry.register(uri, actor, **kwargs)["id"]

    def handbook(self, actions=("list", "read"), resources=("acme/handbook:*",), **extra) -> str:
        self.allow("gh", actions, resources, **extra)
        return self.register("connection://gh/acme/handbook")

    def calls(self, run_id: str | None = None, **kw) -> list[dict]:
        return self.harness.connections.calls(run_id=run_id, limit=2000, **kw)

    def text(self, digest: str) -> str:
        return self.region.text(digest)


def connect(region: Region, provider: str, name: str, token: str) -> None:
    svc.connect(
        region.services,
        svc.ConnectRequest(
            provider=provider,
            method="token",
            fields={"token": token},
            name=name,
            verify=False,
            actor="ops",
        ),
    )


@pytest.fixture()
def world(tmp_path, monkeypatch) -> World:
    faults.disarm()
    hconn.forget_cache()
    region = build_region(tmp_path, monkeypatch)
    github, drive, graph = FakeGitHub(), FakeDrive(), FakeGraph()
    github.commit("acme/handbook", "main", HANDBOOK, "initial")
    github.commit(
        "acme/erp",
        "main",
        {
            "pyproject.toml": '[project]\nname = "erp"\nversion = "0.1.0"\n',
            "README.md": "# ERP\n\nRun `python -m pytest -q` before sending a change.\n",
            "erp/__init__.py": "",
            "erp/orders.py": ORDERS,
            "tests/test_orders.py": ORDER_TESTS,
        },
        "initial",
    )
    folder = drive.folder("Policies")
    refund = drive.doc(folder, "Refund policy", REFUND_DOC)
    drive.file(folder, "notes.md", "# Notes\n\nNothing about refunds here.\n")
    graph.drives["b!drive1"] = {
        "Ops/runbook.md": RUNBOOK.encode(),
        "Ops/oncall.md": b"# On call\n\nPage the platform team.\n",
    }
    net = Network(github, drive, graph)
    monkeypatch.setattr(hconn, "transport", net)
    connect(region, "github", "gh", GITHUB_TOKEN)
    connect(region, "google_drive", "drive", DRIVE_TOKEN)
    connect(region, "microsoft_365", "m365", GRAPH_TOKEN)
    yield World(region, net, github, drive, graph, folder, refund)
    faults.disarm()
    hconn.forget_cache()
    resolve.uninstall()


# ---------------------------------------------------------------------------
# Leak detection: every place a credential could end up, searched for it
# ---------------------------------------------------------------------------


def _variants(secret: str) -> list[str]:
    raw = secret.encode()
    return [secret, base64.b64encode(raw).decode(), raw.hex(), json.dumps(secret)[1:-1]]


def leaks(world: World, *extra_texts: str, secrets=SECRETS) -> list[str]:
    """Every location a credential (or its base64/hex spelling) was found. Empty is the goal."""
    needles = [v for s in secrets for v in _variants(s)]
    found: list[str] = []
    state = world.harness.state
    tables = [
        r["table_name"]
        for r in state.rows(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
            "AND table_name LIKE 'harness_%'"
        )
    ]
    for table in tables:
        for row in state.rows(f"SELECT * FROM {table}"):
            text = json.dumps(dict(row), default=str)
            if table == "harness_blobs":
                text += base64.b64decode(row["content_b64"]).decode("utf-8", errors="replace")
            for needle in needles:
                if needle in text:
                    found.append(f"table {table}")
    root: Path = world.region.root
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        try:
            data = path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            continue
        for needle in needles:
            if needle in data:
                found.append(f"file {path.relative_to(root)}")
    for index, text in enumerate(extra_texts):
        for needle in needles:
            if needle in text:
                found.append(f"text #{index}")
    return sorted(set(found))


def answer_request(inputs: list[str], **extra: Any) -> dict:
    request = {
        "workload": "answer",
        "question": "Which orders need an exception badge?",
        "inputs": inputs,
        "identifiers": ["exception badge"],
        "discovery": "off",
    }
    request.update(extra)
    return request

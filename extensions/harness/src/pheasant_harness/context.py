"""What every harness operation needs, assembled once from the host's context."""

from __future__ import annotations

from typing import Any

from pheasant_harness.blobs import BlobStore
from pheasant_harness.broker import Broker
from pheasant_harness.connections import Gateway
from pheasant_harness.registry import Registry
from pheasant_harness.sandbox import SandboxManager
from pheasant_harness.store import RunStore


class Harness:
    def __init__(self, services: Any, *, sandbox_root: str | None = None):
        self.services = services
        self.config = services.config
        self.state = services.state
        self.kb_id = services.config.knowledge_base_id
        self.blobs = BlobStore(self.state)
        self.registry = Registry(self)
        self.sandboxes = SandboxManager(self, sandbox_root)
        self.broker = Broker(self)
        self.connections = Gateway(self)
        self.runs = RunStore(self.state, self.kb_id)


_CACHE: dict[int, Harness] = {}


def harness_for(services: Any) -> Harness:
    key = id(services)
    found = _CACHE.get(key)
    if found is None or found.services is not services:
        found = _CACHE[key] = Harness(services)
    return found

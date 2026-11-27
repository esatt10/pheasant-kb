"""Delivery adapters: publish one verified revision to one authorized target.

Each adapter states what it can promise. ``dispatch`` is called with a stable
operation id; ``reconcile`` answers, after an ambiguous outcome, whether the
target already holds that operation's effect:

* ``committed`` -- yes, with the downstream id and revision (a receipt);
* ``absent``    -- provably not, so dispatching again is safe;
* ``unknown``   -- the target can neither deduplicate nor be observed, so a
  second dispatch could double the effect. The coordinator must not repeat it.

An outbox makes the *intent* durable. It cannot make another system apply a
write once; only the target's own dedup, a conditional write, or an observable
state that reconciliation can read does that.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

from pheasant_harness import faults
from pheasant_harness.common import HarnessError, digest_bytes, new_id, now

DELIVERY_ROOT_ENV = "PHEASANT_HARNESS_DELIVERY_ROOT"


class DeliveryConflict(HarnessError):
    status = 409
    code = "HARNESS_DELIVERY_CONFLICT"


class Adapter:
    name = "base"
    idempotent = False
    observable = False

    def __init__(self, harness: Any):
        self.harness = harness
        self.state = harness.state

    def describe(self) -> dict:
        return {"adapter": self.name, "idempotent": self.idempotent, "observable": self.observable}

    def dispatch(
        self, operation_id: str, target: str, payload: bytes, expected: str | None
    ) -> dict:
        raise NotImplementedError

    def reconcile(self, operation_id: str, target: str, payload_digest: str) -> dict:
        raise NotImplementedError


class RecordAdapter(Adapter):
    """A read-only answer is delivered by committing its accessible result record."""

    name = "record"
    idempotent = True
    observable = True

    def dispatch(self, operation_id, target, payload, expected):
        return {"downstream_id": operation_id, "downstream_revision": digest_bytes(payload)}

    def reconcile(self, operation_id, target, payload_digest):
        return {"status": "absent"}


class TestSinkAdapter(Adapter):
    """An idempotent sink keyed by operation id: the shape of a well-behaved API."""

    name = "test_sink"
    idempotent = True
    observable = True

    def dispatch(self, operation_id, target, payload, expected):
        digest = digest_bytes(payload)
        self.state.execute(
            "INSERT INTO harness_test_sink(operation_id,payload_digest,body,created_at) "
            "VALUES(?,?,?,?) ON CONFLICT (operation_id) DO NOTHING",
            (operation_id, digest, payload.decode("utf-8", errors="replace"), now()),
        )
        faults.hit("after_sink_commit")
        return {"downstream_id": operation_id, "downstream_revision": digest}

    def reconcile(self, operation_id, target, payload_digest):
        rows = self.state.rows(
            "SELECT payload_digest FROM harness_test_sink WHERE operation_id=?", (operation_id,)
        )
        if not rows:
            return {"status": "absent"}
        if rows[0]["payload_digest"] != payload_digest:
            return {"status": "conflict", "found": rows[0]["payload_digest"]}
        return {
            "status": "committed",
            "downstream_id": operation_id,
            "downstream_revision": payload_digest,
        }


class BlindSinkAdapter(Adapter):
    """A sink with neither deduplication nor observable state (fixture A09)."""

    name = "blind_sink"

    def dispatch(self, operation_id, target, payload, expected):
        self.state.execute(
            "INSERT INTO harness_blind_sink(id,payload_digest,body,created_at) VALUES(?,?,?,?)",
            (
                new_id("blind"),
                digest_bytes(payload),
                payload.decode("utf-8", errors="replace"),
                now(),
            ),
        )
        faults.hit("after_sink_commit")
        return {"downstream_id": None, "downstream_revision": None}

    def reconcile(self, operation_id, target, payload_digest):
        return {"status": "unknown"}


class FileAdapter(Adapter):
    """A file under the delivery root, written conditionally on its prior version."""

    name = "file"
    idempotent = True
    observable = True

    def _path(self, target: str) -> Path:
        root = Path(
            os.environ.get(DELIVERY_ROOT_ENV)
            or Path(self.harness.config.pheasant.exports_path) / "harness"
        )
        rel = PurePosixPath(target)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise HarnessError(f"delivery target {target!r} leaves the delivery root")
        return root / rel

    @staticmethod
    def version(path: Path) -> str:
        return digest_bytes(path.read_bytes()) if path.exists() else "absent"

    def dispatch(self, operation_id, target, payload, expected):
        path = self._path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        receipt = path.with_name(path.name + ".receipt.json")
        current = self.version(path)
        if current == digest_bytes(payload) and receipt.exists():
            prior = json.loads(receipt.read_text())
            if prior.get("operation_id") == operation_id:
                return {"downstream_id": str(path), "downstream_revision": current}
        if expected is not None and current != expected:
            raise DeliveryConflict(
                f"{target} is at {current[:19]}, not the expected {expected[:19]}; the intervening "
                "change is preserved and a new candidate needs revalidation"
            )
        temp = path.with_name(f".{path.name}.{operation_id[-12:]}.tmp")
        temp.write_bytes(payload)
        os.replace(temp, path)
        receipt.write_text(
            json.dumps({"operation_id": operation_id, "digest": digest_bytes(payload)})
        )
        faults.hit("after_sink_commit")
        return {"downstream_id": str(path), "downstream_revision": digest_bytes(payload)}

    def reconcile(self, operation_id, target, payload_digest):
        path = self._path(target)
        receipt = path.with_name(path.name + ".receipt.json")
        if receipt.exists():
            prior = json.loads(receipt.read_text())
            if prior.get("operation_id") == operation_id and self.version(path) == payload_digest:
                return {
                    "status": "committed",
                    "downstream_id": str(path),
                    "downstream_revision": payload_digest,
                }
        return {"status": "absent"}


ADAPTERS = {
    cls.name: cls for cls in (RecordAdapter, TestSinkAdapter, BlindSinkAdapter, FileAdapter)
}


def adapter(harness: Any, name: str) -> Adapter:
    if name not in ADAPTERS:
        raise HarnessError(f"unknown delivery adapter {name!r}; choose from {sorted(ADAPTERS)}")
    return ADAPTERS[name](harness)

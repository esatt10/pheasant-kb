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


class ConnectionAdapter(Adapter):
    """A file in a repository or folder, written through a connected account.

    Only what the connection's harness policy allows (``write`` on the target's
    resource), only after the approval the policy asks for, conditional on the
    expected version, and tagged with the operation id so reconciliation can find
    it: a GitHub commit whose message names the operation, a Drive file whose
    ``appProperties`` carry it.
    """

    name = "connection"
    idempotent = True
    observable = True

    def describe(self) -> dict:
        return {
            **super().describe(),
            "writes_through": "a connected account, under its harness policy",
            "approval": "as the connection's policy requires (default: an operator approves)",
            "providers": ["github", "google_drive"],
        }

    def dispatch(self, operation_id, target, payload, expected):
        from pheasant_harness import connections

        adapter, parsed = connections.parse_target(self.harness, target)
        receipt = adapter.write(parsed, payload, operation_id, expected)
        faults.hit("after_sink_commit")
        return receipt

    def reconcile(self, operation_id, target, payload_digest):
        from pheasant_harness import connections

        adapter, parsed = connections.parse_target(self.harness, target)
        return adapter.reconcile(parsed, operation_id, payload_digest)


class ArtifactAdapter(Adapter):
    """A Pheasant artifact: the verified result kept as knowledge in ``artifacts``.

    Saved through Pheasant's own artifact service, so it is filed by day and
    person, searchable, browsable in the taxonomy view and removable like any
    artifact. A document is kept as a report; a code change as the files it
    changed (deleted files listed in its summary) with its release note as the
    report text. The operation id is the artifact's session, which is what
    reconciliation looks it up by: a crash after the save finds it instead of
    saving a second copy. ``target`` is the title (default: the document's first
    heading).
    """

    name = "artifact"
    idempotent = True
    observable = True

    def describe(self) -> dict:
        return {**super().describe(), "keeps": "a Pheasant artifact in the artifacts source"}

    def _find(self, operation_id: str) -> str | None:
        rows = self.state.rows(
            "SELECT id FROM saved_artifacts WHERE kb_id=? AND session_id=? ORDER BY created_at",
            (self.harness.kb_id, operation_id),
        )
        return rows[0]["id"] if rows else None

    @staticmethod
    def _content(payload: bytes) -> tuple[str, list[dict], str]:
        text = payload.decode("utf-8", errors="replace")
        try:
            bundle = json.loads(text)
        except ValueError:
            bundle = None
        if not isinstance(bundle, dict) or not any(
            isinstance(v, dict) and "digest" in v for v in bundle.values()
        ):
            return text, [], "report"
        files: list[dict] = []
        notes: list[str] = []
        deleted: list[str] = []
        for _key, part in sorted(bundle.items()):
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("files"), dict):
                for path, content in sorted(part["files"].items()):
                    if content is None:
                        deleted.append(path)
                    else:
                        files.append({"name": path, "content": content})
            elif "content" in part:
                notes.append(str(part["content"]))
        markdown = "\n\n".join(notes)
        if deleted:
            markdown += "\n\n## Deleted files\n\n" + "\n".join(f"- `{p}`" for p in deleted)
        return markdown.strip(), files, "files" if files else "report"

    def dispatch(self, operation_id, target, payload, expected):
        from pheasant.services import artifacts as artifact_service
        from pheasant_harness.connections import _who

        found = self._find(operation_id)
        if found:
            return {"downstream_id": found, "downstream_revision": digest_bytes(payload)}
        markdown, files, kind = self._content(payload)
        heading = next(
            (line.lstrip("# ").strip() for line in markdown.splitlines() if line.strip()), ""
        )
        title = (target or heading or "Harness result")[:120]
        saved = artifact_service.save(
            self.harness.services,
            artifact_service.SaveRequest(
                title=title,
                kind=kind,
                markdown=markdown or None,
                files=files or None,
                summary=f"Built and verified by the Pheasant harness ({operation_id}).",
                principal=_who().get("actor"),
                conversation_id=operation_id,
                channel="api",
            ),
        )
        faults.hit("after_sink_commit")
        return {"downstream_id": saved["id"], "downstream_revision": digest_bytes(payload)}

    def reconcile(self, operation_id, target, payload_digest):
        found = self._find(operation_id)
        if found:
            return {
                "status": "committed",
                "downstream_id": found,
                "downstream_revision": payload_digest,
            }
        return {"status": "absent"}


ADAPTERS = {
    cls.name: cls
    for cls in (
        RecordAdapter,
        TestSinkAdapter,
        BlindSinkAdapter,
        FileAdapter,
        ConnectionAdapter,
        ArtifactAdapter,
    )
}


def adapter(harness: Any, name: str) -> Adapter:
    if name not in ADAPTERS:
        raise HarnessError(f"unknown delivery adapter {name!r}; choose from {sorted(ADAPTERS)}")
    return ADAPTERS[name](harness)

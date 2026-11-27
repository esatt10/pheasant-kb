"""Tool execution: one admitted action in, one typed result out.

A result carries its outcome class, a compact observation for the ledger,
the evidence records the observation supports, any artifact it staged (bytes
already in the blob store, digest in hand), and what it spent. Nothing here
commits run state; the coordinator does, fenced, after checking the result
belongs to the step's current attempt.
"""

from __future__ import annotations

import io
import json
import re
import time
from fnmatch import fnmatch
from typing import Any

from pheasant_harness import discovery, investigate, render
from pheasant_harness.broker import untrusted_instructions
from pheasant_harness.common import HarnessError, digest_bytes, digest_value, now
from pheasant_harness.registry import EntryUnreadable, Revision, RevisionMoved

EXTRACT_CHARS = 1600
#: A read's evidence keeps the whole page it read (bounded by the read tool itself).
READ_EXTRACT_CHARS = 64 * 1024
EVIDENCE_REF = re.compile(r"\[(E[0-9a-f]{8})\]")


def evidence_id(
    registration_id: str | None, revision: str | None, locator: dict, extract: str
) -> str:
    return "E" + digest_value([registration_id, revision, locator, extract])[7:15]


def _evidence(
    reg_id,
    revision,
    locator,
    method,
    extract,
    classification="internal",
    status="observed",
    **flags,
) -> dict:
    extract = extract[: READ_EXTRACT_CHARS if method == "read" else EXTRACT_CHARS]
    record_flags = {k: v for k, v in flags.items() if v}
    if untrusted_instructions(extract):
        # Data, not instructions: kept, labeled, and never able to grant anything.
        record_flags["untrusted_instructions"] = True
    return {
        "id": evidence_id(reg_id, revision, locator, extract),
        "registration_id": reg_id,
        "revision": revision,
        "locator": locator,
        "method": method,
        "status": status,
        "extract": extract,
        "extract_digest": digest_bytes(extract.encode()),
        "classification": classification,
        "flags": record_flags,
    }


class ToolContext:
    """Per-run helpers the tools share: pinned revisions and the workspace."""

    def __init__(self, harness: Any, run: dict, cancelled=None):
        self.harness = harness
        self.run = run
        self.cancelled = cancelled or (lambda: False)

    def pinned(self, reg_id: str, revision: str | None) -> Revision:
        actor = self.run["actor"]
        if revision:
            return self.harness.registry.revision(reg_id, revision, actor)
        rows = self.harness.state.rows(
            "SELECT result_json FROM harness_steps WHERE run_id=? AND action='resolve' "
            "AND status='succeeded' ORDER BY updated_at DESC",
            (self.run["id"],),
        )
        for row in rows:
            result = json.loads(row["result_json"])
            handle = result.get("observation", {}).get("handle", {})
            if handle.get("registration_id") == reg_id:
                return self.harness.registry.revision(reg_id, handle["revision"], actor)
        return self.harness.registry.resolve(reg_id, actor)

    def resolver(self, reg_id: str):
        reg = self.harness.registry.get(reg_id)
        return reg, self.harness.registry.resolver(reg)

    # -- workspaces ------------------------------------------------------------

    def latest_tree(self) -> dict | None:
        rows = self.harness.state.rows(
            "SELECT digest, provenance_json FROM harness_artifacts WHERE run_id=? AND kind='tree' "
            "ORDER BY created_at DESC LIMIT 1",
            (self.run["id"],),
        )
        if not rows:
            return None
        manifest = json.loads(
            self.harness.blobs.get_text(rows[0]["digest"].replace("tree:", "", 1))
        )
        return {"digest": rows[0]["digest"], "manifest": manifest}

    def tree_files(self, manifest: dict) -> dict[str, bytes]:
        """Base revision plus the candidate's overlay, every digest verified."""
        base = manifest["base"]
        reg, resolver = self.resolver(base["registration_id"])
        revision = self.harness.registry.revision(reg.id, base["revision"], self.run["actor"])
        files = {}
        for entry in revision.entries:
            if entry["kind"] == "file":
                files[entry["path"]] = resolver.read_bytes(revision, entry["path"])
        for path, digest in manifest.get("files", {}).items():
            if digest is None:
                files.pop(path, None)
            else:
                files[path] = self.harness.blobs.get(digest)
        return files

    def workspace(self, reg_id: str | None = None, *, from_base: bool = False):
        sandboxes = self.harness.sandboxes
        if from_base:
            sandboxes.destroy_for_run(self.run["id"], "reset to base for a new proposal")
        rows = self.harness.state.rows(
            "SELECT generation FROM harness_sandboxes WHERE run_id=? AND profile='workspace' "
            "AND state='active' ORDER BY created_at DESC",
            (self.run["id"],),
        )
        for row in rows:
            found = sandboxes.get(row["generation"])
            if found is not None:
                sandboxes.renew(found)
                return found, False
            sandboxes.destroy(row["generation"], "stale: lease lapsed or scratch lost")
        sandbox = sandboxes.allocate(self.run["id"], self.run["tenant"], "workspace")
        tree = None if from_base else self.latest_tree()
        if tree is not None:
            files = self.tree_files(tree["manifest"])
        elif reg_id is not None:
            reg, resolver = self.resolver(reg_id)
            revision = self.pinned(reg_id, None)
            files = {
                e["path"]: resolver.read_bytes(revision, e["path"])
                for e in revision.entries
                if e["kind"] == "file"
            }
        else:
            raise HarnessError("no checkpoint and no base registration to materialize")
        sandboxes.materialize(sandbox, files)
        return sandbox, True


def execute(harness: Any, run: dict, step: dict, cancelled=None) -> dict:
    started = time.monotonic()
    context = ToolContext(harness, run, cancelled)
    action, args = step["action"], step["args"]
    handler = HANDLERS.get(action)
    if handler is None:
        raise HarnessError(f"no executor for {action}")
    try:
        result = handler(context, args)
    except RevisionMoved as exc:
        result = {"outcome": "version_conflict", "observation": {"error": str(exc)}}
    except EntryUnreadable as exc:
        result = {"outcome": "partial", "observation": {"error": str(exc)}}
    except HarnessError as exc:
        if exc.status == 403:
            kind = "denied"
        elif exc.retryable:
            kind = "transient_error"
        elif exc.code == "HARNESS_CONNECTION_UNAVAILABLE":
            kind = "permanent_error"
        else:
            kind = "invalid_input"
        result = {"outcome": kind, "observation": {"error": str(exc), "code": exc.code}}
    result.setdefault("evidence", [])
    result.setdefault("artifacts", [])
    spent = dict(result.pop("spent", {}))
    spent.setdefault("tool_calls", 1)
    spent["wall_seconds"] = round(time.monotonic() - started, 3)
    result["spent"] = spent
    return result


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _resolve(ctx: ToolContext, args: dict) -> dict:
    revision = ctx.harness.registry.resolve(
        args["registration_id"], ctx.run["actor"], args.get("selector")
    )
    unreadable = [e["path"] for e in revision.entries if e["kind"] == "unreadable"]
    reg = ctx.harness.registry.get(args["registration_id"])
    listing = "\n".join(e["path"] for e in revision.entries[:400])
    record = _evidence(
        reg.id,
        revision.revision,
        {
            "registration_id": reg.id,
            "revision": revision.revision,
            "manifest": revision.manifest_digest,
        },
        "resolve",
        f"revision {revision.revision} ({revision.kind}, {len(revision.entries)} "
        f"entries)\n{listing}",
        reg.classification,
        time_bound=revision.time_bound,
    )
    return {
        "outcome": "success",
        "observation": {
            "handle": revision.handle(),
            "unreadable": unreadable,
            "paths": [e["path"] for e in revision.entries[:2000]],
            "evidence_id": record["id"],
        },
        "evidence": [record],
    }


def _discover(ctx: ToolContext, args: dict) -> dict:
    regs = [ctx.harness.registry.get(r) for r in ctx.run["request"].get("inputs", [])]
    result = discovery.discover(
        ctx.harness,
        args["query"],
        filters=args.get("filters"),
        cursor=args.get("cursor"),
        limit=int(args.get("limit", 8)),
        registrations=regs,
        enabled=ctx.run["request"].get("discovery") != "off",
    )
    return {"outcome": result["outcome"], "observation": result}


def _enumerate(ctx: ToolContext, args: dict) -> dict:
    revision = ctx.pinned(args["registration_id"], args.get("revision"))
    page = investigate.enumerate_entries(
        revision, args.get("prefix", ""), args.get("cursor"), int(args.get("limit", 500))
    )
    return {"outcome": page["outcome"], "observation": page}


def _grep(ctx: ToolContext, args: dict) -> dict:
    reg, resolver = ctx.resolver(args["registration_id"])
    revision = ctx.pinned(reg.id, args.get("revision"))
    result = investigate.grep(
        resolver, revision, args["pattern"], args.get("options"), args.get("cursor")
    )
    evidence = [
        _evidence(
            reg.id,
            revision.revision,
            m["locator"],
            "grep",
            m["text"],
            reg.classification,
            time_bound=revision.time_bound,
        )
        for m in result["matches"]
    ]
    coverage = _evidence(
        reg.id,
        revision.revision,
        {
            "registration_id": reg.id,
            "revision": revision.revision,
            "pattern": args["pattern"],
            "cursor": args.get("cursor"),
        },
        "grep-coverage",
        result["coverage"]["statement"],
        reg.classification,
        status="observed" if result["coverage"]["complete"] else "partial",
    )
    for match, record in zip(result["matches"], evidence, strict=True):
        match["evidence_id"] = record["id"]
    result["coverage_evidence_id"] = coverage["id"]
    bytes_scanned = (
        sum((e.get("size") or 0) for e in revision.entries if e["kind"] == "file")
        if result["coverage"]["complete"]
        else 0
    )
    return {
        "outcome": result["outcome"],
        "observation": result,
        "evidence": [*evidence, coverage],
        "spent": {"bytes_scanned": bytes_scanned},
    }


def _read(ctx: ToolContext, args: dict) -> dict:
    reg, resolver = ctx.resolver(args["registration_id"])
    revision = ctx.pinned(reg.id, args.get("revision"))
    page = investigate.read_range(
        resolver, revision, args["path"], args.get("start_line"), args.get("end_line")
    )
    evidence = []
    if page.get("content"):
        evidence.append(
            _evidence(
                reg.id,
                revision.revision,
                page["locator"],
                "read",
                page["content"],
                reg.classification,
                time_bound=revision.time_bound,
                truncated=page["truncated"],
            )
        )
        page["evidence_id"] = evidence[0]["id"]
        page["untrusted_instructions"] = bool(evidence[0]["flags"].get("untrusted_instructions"))
    return {
        "outcome": page["outcome"],
        "observation": page,
        "evidence": evidence,
        "spent": {"bytes_scanned": len((page.get("content") or "").encode())},
    }


def _inspect(ctx: ToolContext, args: dict) -> dict:
    reg, resolver = ctx.resolver(args["registration_id"])
    revision = ctx.pinned(reg.id, args.get("revision"))
    result = investigate.inspect_media(
        resolver, revision, args["path"], args.get("region"), ctx.harness.blobs
    )
    summary = {
        "dimensions": result.get("dimensions"),
        "region": result.get("region"),
        "derivatives": [
            {"path": d["path"], "kind": d["kind"], "content": d["content"]}
            for d in result["derivatives"]
        ],
    }
    locator = {
        "registration_id": reg.id,
        "revision": revision.revision,
        "path": args["path"],
        "asset_digest": result["asset_digest"],
        "region": (result.get("region") or {}).get("box"),
        "coordinate_system": result.get("coordinate_system"),
    }
    record = _evidence(
        reg.id,
        revision.revision,
        locator,
        "inspect",
        json.dumps(summary, sort_keys=True),
        reg.classification,
        faint=(result.get("region") or {}).get("legibility") == "faint",
    )
    result["evidence_id"] = record["id"]
    return {"outcome": result["outcome"], "observation": result, "evidence": [record]}


def _follow(ctx: ToolContext, args: dict) -> dict:
    reg, resolver = ctx.resolver(args["registration_id"])
    revision = ctx.pinned(reg.id, args.get("revision"))
    result = investigate.follow_symbol(resolver, revision, args["symbol"])
    evidence = []
    for kind in ("definitions", "callers"):
        for match in result[kind]:
            record = _evidence(
                reg.id,
                revision.revision,
                match["locator"],
                f"follow:{kind}",
                match["text"],
                reg.classification,
            )
            match["evidence_id"] = record["id"]
            evidence.append(record)
    return {"outcome": result["outcome"], "observation": result, "evidence": evidence}


def _compare(ctx: ToolContext, args: dict) -> dict:
    reg, resolver = ctx.resolver(args["registration_id"])
    older = ctx.pinned(reg.id, args["from_revision"])
    newer = ctx.pinned(reg.id, args.get("to_revision"))
    return {"outcome": "success", "observation": investigate.compare(resolver, older, newer)}


def _materialize(ctx: ToolContext, args: dict) -> dict:
    sandbox, fresh = ctx.workspace(args["registration_id"])
    return {
        "outcome": "success",
        "observation": {
            "generation": sandbox.generation,
            "fresh": fresh,
            "rehydrated_from": (ctx.latest_tree() or {}).get("digest"),
        },
    }


def _apply_patch(ctx: ToolContext, args: dict) -> dict:
    base_reg = args.get("registration_id") or ctx.run["request"]["inputs"][0]
    tree = ctx.latest_tree()
    expected = args.get("expected_revision")
    if expected and expected != (tree["digest"] if tree else "base"):
        return {
            "outcome": "version_conflict",
            "observation": {"expected": expected, "current": tree["digest"] if tree else "base"},
        }
    sandbox, fresh = ctx.workspace(base_reg, from_base=bool(args.get("reset")))
    parent = tree["digest"] if tree else None
    if args.get("reset"):
        tree = None
    sandboxes = ctx.harness.sandboxes
    touched = []
    for edit in args["edits"]:
        path = edit["path"]
        if edit.get("delete"):
            sandboxes.delete(sandbox, path)
        elif edit.get("content") is not None:
            sandboxes.write(sandbox, path, edit["content"].encode())
        else:
            before = sandboxes.read(sandbox, path)
            if before is None:
                return {
                    "outcome": "invalid_input",
                    "observation": {"error": f"{path} does not exist"},
                }
            text = before.decode()
            count = text.count(edit["find"])
            if count != 1:
                return {
                    "outcome": "invalid_input",
                    "observation": {"error": f"find text matches {count} times in {path}; needs 1"},
                }
            sandboxes.write(sandbox, path, text.replace(edit["find"], edit["replace"]).encode())
        touched.append(path)
    # Checkpoint: the candidate is the base revision plus every changed file, by digest.
    if tree is not None:
        base = tree["manifest"]["base"]
    else:
        base = {"registration_id": base_reg, "revision": ctx.pinned(base_reg, None).revision}
    base_files = ctx.tree_files({"base": base, "files": {}})
    work = sandboxes.work_files(sandbox)
    overlay = {}
    for path, data in work.items():
        if base_files.get(path) != data:
            overlay[path] = ctx.harness.blobs.put(data)
    for path in base_files.keys() - work.keys():
        overlay[path] = None
    manifest = {
        "base": base,
        "files": dict(sorted(overlay.items())),
        "parent": parent,
        "kind": "tree",
    }
    manifest_digest = ctx.harness.blobs.put_text(
        json.dumps(manifest, sort_keys=True), "application/json"
    )
    digest = "tree:" + manifest_digest
    diff = _diff(base_files, work, overlay)
    return {
        "outcome": "success",
        "observation": {
            "candidate": digest,
            "changed": sorted(overlay),
            "touched": touched,
            "generation": sandbox.generation,
            "rehydrated": fresh,
            "diff": diff[:6000],
        },
        "artifacts": [
            {
                "digest": digest,
                "kind": "tree",
                "name": "candidate tree",
                "media_type": "application/vnd.pheasant.tree+json",
                "parent_digest": parent,
                "provenance": {
                    "base": base,
                    "changed": sorted(overlay),
                    "generation": sandbox.generation,
                },
            }
        ],
    }


def _diff(base: dict[str, bytes], work: dict[str, bytes], overlay: dict) -> str:
    import difflib

    chunks = []
    for path in sorted(overlay):
        before = base.get(path, b"").decode(errors="replace").splitlines(keepends=True)
        after = work.get(path, b"").decode(errors="replace").splitlines(keepends=True)
        chunks.extend(difflib.unified_diff(before, after, f"a/{path}", f"b/{path}"))
    return "".join(chunks)


def _execute(ctx: ToolContext, args: dict) -> dict:
    base_reg = args.get("registration_id") or ctx.run["request"]["inputs"][0]
    sandbox, fresh = ctx.workspace(base_reg)
    tree = ctx.latest_tree()
    outcome = ctx.harness.sandboxes.execute(
        sandbox, args["argv"], timeout=args.get("timeout_seconds"), cancelled=ctx.cancelled
    )
    outcome["tree"] = tree["digest"] if tree else "base"
    outcome["rehydrated"] = fresh
    locator = {
        "tree": outcome["tree"],
        "argv": args["argv"],
        "image": outcome["environment"]["image_digest"],
    }
    record = _evidence(
        None,
        outcome["tree"],
        locator,
        "execute",
        f"exit {outcome['exit_status']} ({outcome['terminated'] or 'completed'})\n"
        + (outcome["stdout"][-700:] + outcome["stderr"][-700:]),
    )
    outcome["evidence_id"] = record["id"]
    kind = "success" if outcome["exit_status"] == 0 else "permanent_error"
    if outcome["terminated"] == "cancelled":
        kind = "partial"
    return {
        "outcome": kind,
        "observation": outcome,
        "evidence": [record],
        "spent": {"sandbox_seconds": outcome["duration_seconds"]},
    }


def _figure(values: list[dict]) -> bytes:
    """A bar chart of the table's values, labeled as a generated illustration."""
    from PIL import Image, ImageDraw

    width, bar_h, pad = 640, 34, 16
    image = Image.new("RGB", (width, pad * 3 + bar_h * len(values) + 18), "white")
    draw = ImageDraw.Draw(image)
    numbers = [float(v["value"]) for v in values] or [1.0]
    top = max(numbers) or 1.0
    draw.text((pad, pad // 2), "Generated illustration", fill=(90, 98, 106))
    for i, (item, number) in enumerate(zip(values, numbers, strict=True)):
        y = pad * 2 + i * bar_h
        draw.text((pad, y + 9), str(item["label"])[:28], fill=(29, 33, 37))
        length = int((width - 260) * number / top)
        draw.rectangle((220, y + 4, 220 + length, y + bar_h - 8), fill=(70, 110, 150))
        draw.text((228 + length, y + 9), str(item["value"]), fill=(29, 33, 37))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _draft(ctx: ToolContext, args: dict) -> dict:
    content = args["content"]
    if args.get("kind") == "figure":
        try:
            data = _figure(args.get("figure") or [])
        except ImportError:
            # An illustration is optional; without an image library there is none.
            return {"outcome": "partial", "observation": {"error": "no image library on this host"}}
        digest = ctx.harness.blobs.put(data, "image/png")
        return {
            "outcome": "success",
            "observation": {
                "digest": digest,
                "bytes": len(data),
                "name": args["name"],
                "generated": True,
            },
            "artifacts": [
                {
                    "digest": digest,
                    "kind": "figure",
                    "name": args["name"],
                    "media_type": "image/png",
                    "parent_digest": None,
                    "provenance": {"generated_illustration": True, "values": args.get("figure")},
                }
            ],
            "spent": {"output_bytes": len(data)},
        }
    data = content.encode()
    digest = ctx.harness.blobs.put(data, args.get("media_type", "text/markdown"))
    cited = sorted(set(EVIDENCE_REF.findall(content)))
    return {
        "outcome": "success",
        "observation": {"digest": digest, "bytes": len(data), "cites": cited, "name": args["name"]},
        "artifacts": [
            {
                "digest": digest,
                "kind": args.get("kind", "document"),
                "name": args["name"],
                "media_type": args.get("media_type", "text/markdown"),
                "parent_digest": args.get("parent"),
                "provenance": {
                    "cites": cited,
                    "audience": ctx.run["request"].get("audience", ""),
                    "tree": args.get("tree"),
                },
            }
        ],
        "spent": {"output_bytes": len(data)},
    }


def _render(ctx: ToolContext, args: dict) -> dict:
    source = ctx.harness.blobs.get_text(args["digest"])
    profile = args.get("profile", "print")
    artifacts = []
    observation: dict[str, Any] = {"of": args["digest"], "profile": profile}
    if profile == "print":
        printed = render.render_print(source)
        out = ctx.harness.blobs.put_text(printed["text"])
        observation.update({"render": out, "clipped": printed["clipped"], "status": "rendered"})
        artifacts.append(
            {
                "digest": out,
                "kind": "render",
                "name": "print render",
                "media_type": "text/plain",
                "parent_digest": args["digest"],
                "provenance": {
                    "profile": "print",
                    "renderer": printed["renderer"],
                    "clipped": printed["clipped"],
                },
            }
        )
    else:
        page = render.render_html(source)
        page_digest = ctx.harness.blobs.put_text(page, "text/html")
        artifacts.append(
            {
                "digest": page_digest,
                "kind": "render",
                "name": "html render",
                "media_type": "text/html",
                "parent_digest": args["digest"],
                "provenance": {"profile": "html", "renderer": render.RENDERER_VERSION},
            }
        )
        observation.update({"render": page_digest, "status": "rendered"})
        if profile == "screenshot":
            shot = render.render_screenshot(page, tuple(args.get("viewport", (1024, 768))))
            observation["screenshot"] = {k: v for k, v in shot.items() if k != "png"}
            if shot.get("status") == "rendered":
                png = ctx.harness.blobs.put(shot["png"], "image/png")
                observation["screenshot"]["digest"] = png
                artifacts.append(
                    {
                        "digest": png,
                        "kind": "render",
                        "name": "screenshot",
                        "media_type": "image/png",
                        "parent_digest": args["digest"],
                        "provenance": {
                            "profile": "screenshot",
                            "overflow": shot["overflow"],
                            "viewport": shot["viewport"],
                        },
                    }
                )
    return {"outcome": "success", "observation": observation, "artifacts": artifacts}


HANDLERS = {
    "resolve": _resolve,
    "discover": _discover,
    "enumerate": _enumerate,
    "grep": _grep,
    "read": _read,
    "inspect": _inspect,
    "follow": _follow,
    "compare": _compare,
    "materialize": _materialize,
    "apply_patch": _apply_patch,
    "execute": _execute,
    "draft": _draft,
    "render": _render,
}


def matches_any(path: str, globs: list[str]) -> bool:
    return any(fnmatch(path, g) for g in globs)


__all__ = ["EVIDENCE_REF", "ToolContext", "execute", "matches_any", "now"]

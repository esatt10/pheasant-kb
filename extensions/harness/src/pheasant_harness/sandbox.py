"""Disposable workspaces, assumed lost between any two tool calls.

A sandbox *generation* is bound to one run, tenant and trust profile, starts
from a clean directory, receives its inputs read-only from pinned bytes, and
writes only to a private working copy. Nothing in it is part of the recovery
contract: what survives is what was checkpointed -- staged into the blob store
by digest and named by a committed journal entry. A fresh generation on any
host rebuilds from that.

Isolation is declared per trust profile (:func:`isolation_manifest`). This
build's mechanism is an OS process per command with its own session, resource
limits (CPU, address space, processes, file size), a scrubbed environment with
no credentials, a working directory inside the generation, and -- where the
kernel allows unprivileged user namespaces -- a private network namespace with
no interfaces but loopback. A deployment with stronger needs runs the same
contract under gVisor or a microVM; the manifest is where it says so.

The reaper (:meth:`SandboxManager.reap`) is the cleanup controller: it needs
nothing from the worker that allocated a generation, kills whatever process
group is still recorded for it, removes the directory, confirms the directory
is gone and records how long disposal took. A generation it cannot confirm is
quarantined and never reassigned.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pheasant_harness import faults
from pheasant_harness.common import HarnessError, digest_bytes, digest_value, new_id, now

ROOT_ENV = "PHEASANT_HARNESS_SANDBOX_ROOT"
OUTPUT_CAP = 256 * 1024


def _unshare_available() -> bool:
    if not shutil.which("unshare"):
        return False
    try:
        return (
            subprocess.run(
                ["unshare", "-rn", "true"], capture_output=True, timeout=5, check=False
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


_NETNS = None


def network_isolation() -> bool:
    global _NETNS
    if _NETNS is None:
        _NETNS = _unshare_available()
    return _NETNS


PROFILES = {
    "workspace": {
        "description": "editing: repository and generated code, builds and the builder's own tests",
        "cpu_seconds": 120,
        "memory_bytes": 2 * 1024**3,
        "processes": 256,
        "file_bytes": 256 * 1024**2,
        "wall_seconds": 300,
        "lease_seconds": 300,
        "max_lifetime_seconds": 3600,
        "egress": "denied",
    },
    "verifier": {
        "description": "independent checks on an exact candidate digest, with held-out fixtures",
        "cpu_seconds": 120,
        "memory_bytes": 2 * 1024**3,
        "processes": 256,
        "file_bytes": 256 * 1024**2,
        "wall_seconds": 300,
        "lease_seconds": 300,
        "max_lifetime_seconds": 1800,
        "egress": "denied",
    },
}

CLEANUP_DEADLINE_SECONDS = 30.0


def image_digest() -> str:
    """What the sandbox executes with, pinned: interpreter, platform and toolchain."""
    return digest_value(
        {
            "python": sys.version,
            "executable": os.path.realpath(sys.executable),
            "platform": platform.platform(),
            "machine": platform.machine(),
        }
    )


def isolation_manifest() -> dict:
    netns = network_isolation()
    return {
        "mechanism": "process-session+"
        + ("prlimit" if shutil.which("prlimit") else "no-rlimits")
        + "+scrubbed-env"
        + ("+user-netns" if netns else " (no network namespace available)"),
        "network": "private namespace, loopback only" if netns else "not isolated by kernel",
        "credentials": "none: environment is rebuilt from an allowlist",
        "profiles": PROFILES,
        "cleanup_deadline_seconds": CLEANUP_DEADLINE_SECONDS,
        "cleanup_failure_alert": "sandbox quarantined; harness_sandboxes.state='quarantined' and "
        "an event 'sandbox.quarantined' on the run",
        "disposal_confirmation": "directory absent and recorded process group gone, re-checked",
        "image_digest": image_digest(),
        "host": platform.node(),
    }


@dataclass
class Sandbox:
    generation: str
    run_id: str
    profile: str
    root: Path

    @property
    def inputs(self) -> Path:
        return self.root / "inputs"

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"


def _safe_relative(path: str) -> PurePosixPath:
    rel = PurePosixPath(path)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise HarnessError(f"path {path!r} leaves the workspace")
    return rel


class SandboxManager:
    def __init__(self, harness: Any, root: str | os.PathLike | None = None):
        self.harness = harness
        self.state = harness.state
        base = root or os.environ.get(ROOT_ENV) or Path(tempfile.gettempdir()) / "pheasant-harness"
        self.base = Path(base)

    # -- lifecycle ---------------------------------------------------------

    def allocate(self, run_id: str, tenant: str, profile: str = "workspace") -> Sandbox:
        if profile not in PROFILES:
            raise HarnessError(f"unknown trust profile {profile}")
        spec = PROFILES[profile]
        generation = new_id("sbx")
        root = self.base / generation
        for sub in ("inputs", "work", "tmp"):
            (root / sub).mkdir(parents=True, exist_ok=False)
        stamp = now()
        self.state.execute(
            "INSERT INTO harness_sandboxes(generation,run_id,tenant,profile,image_digest,root,"
            "lease_until,expires_at,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                generation,
                run_id,
                tenant,
                profile,
                image_digest(),
                str(root),
                stamp + spec["lease_seconds"],
                stamp + spec["max_lifetime_seconds"],
                "active",
                stamp,
            ),
        )
        return Sandbox(generation, run_id, profile, root)

    def get(self, generation: str) -> Sandbox | None:
        rows = self.state.rows(
            "SELECT * FROM harness_sandboxes WHERE generation=? AND state='active'", (generation,)
        )
        if not rows:
            return None
        row = rows[0]
        sandbox = Sandbox(row["generation"], row["run_id"], row["profile"], Path(row["root"]))
        if not sandbox.work.is_dir() or row["lease_until"] < now() or row["expires_at"] < now():
            return None
        return sandbox

    def renew(self, sandbox: Sandbox) -> None:
        spec = PROFILES[sandbox.profile]
        self.state.execute(
            "UPDATE harness_sandboxes SET lease_until=? WHERE generation=? AND state='active'",
            (
                min(now() + spec["lease_seconds"], now() + spec["max_lifetime_seconds"]),
                sandbox.generation,
            ),
        )

    def materialize(self, sandbox: Sandbox, files: dict[str, bytes]) -> dict:
        """Lay down pinned input bytes read-only, then a private working copy."""
        manifest = {}
        for path, data in sorted(files.items()):
            rel = _safe_relative(path)
            target = sandbox.inputs / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o444)
            work = sandbox.work / rel
            work.parent.mkdir(parents=True, exist_ok=True)
            work.write_bytes(data)
            manifest[path] = digest_bytes(data)
        return {
            "generation": sandbox.generation,
            "files": len(manifest),
            "digest": digest_value(manifest),
        }

    def write(self, sandbox: Sandbox, path: str, data: bytes) -> None:
        target = sandbox.work / _safe_relative(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def delete(self, sandbox: Sandbox, path: str) -> None:
        (sandbox.work / _safe_relative(path)).unlink(missing_ok=True)

    def read(self, sandbox: Sandbox, path: str) -> bytes | None:
        target = sandbox.work / _safe_relative(path)
        return target.read_bytes() if target.is_file() else None

    def work_files(self, sandbox: Sandbox) -> dict[str, bytes]:
        out = {}
        for path in sorted(sandbox.work.rglob("*")):
            if (
                path.is_file()
                and "__pycache__" not in path.parts
                and not path.name.endswith(".pyc")
            ):
                out[path.relative_to(sandbox.work).as_posix()] = path.read_bytes()
        return out

    # -- execution ---------------------------------------------------------

    def execute(
        self,
        sandbox: Sandbox,
        argv: list[str],
        *,
        timeout: float | None = None,
        cancelled: Callable[[], bool] | None = None,
        env: dict[str, str] | None = None,
    ) -> dict:
        spec = PROFILES[sandbox.profile]
        limit = min(float(timeout or spec["wall_seconds"]), float(spec["wall_seconds"]))
        clean_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(sandbox.tmp),
            "TMPDIR": str(sandbox.tmp),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PHEASANT_HARNESS_SANDBOX": sandbox.generation,
            **(env or {}),
        }
        command = list(argv)
        if command[0] in ("python", "python3"):
            command[0] = sys.executable
        isolated_network = network_isolation() and spec["egress"] == "denied"
        if isolated_network:
            command = ["unshare", "-rn", "--", *command]

        # Limits are applied by prlimit(1) in the child, never by Python code run
        # between fork and exec: that window is unsafe in a threaded process.
        if shutil.which("prlimit"):
            command = [
                "prlimit",
                f"--cpu={spec['cpu_seconds']}",
                f"--as={spec['memory_bytes']}",
                f"--fsize={spec['file_bytes']}",
                "--",
                *command,
            ]

        started = time.monotonic()
        out_path, err_path = sandbox.tmp / ".stdout", sandbox.tmp / ".stderr"
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            process = subprocess.Popen(  # noqa: S603 - argv was checked by the broker
                command,
                cwd=sandbox.work,
                env=clean_env,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            self._record_pid(sandbox, process.pid)
            status, reason = None, None
            while status is None:
                try:
                    status = process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    if time.monotonic() - started > limit:
                        reason = "timeout"
                    elif cancelled is not None and cancelled():
                        reason = "cancelled"
                    if reason:
                        self._kill_group(process.pid)
                        status = process.wait()
        self._clear_pid(sandbox, process.pid)
        stdout = out_path.read_bytes()
        stderr = err_path.read_bytes()
        blobs = self.harness.blobs
        return {
            "argv": list(argv),
            "exit_status": status,
            "terminated": reason,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": stdout[-OUTPUT_CAP:].decode("utf-8", errors="replace"),
            "stderr": stderr[-OUTPUT_CAP:].decode("utf-8", errors="replace"),
            "stdout_ref": blobs.put(stdout, "text/plain"),
            "stderr_ref": blobs.put(stderr, "text/plain"),
            "output_truncated": len(stdout) > OUTPUT_CAP or len(stderr) > OUTPUT_CAP,
            "environment": {
                "generation": sandbox.generation,
                "profile": sandbox.profile,
                "image_digest": image_digest(),
                "network_isolated": isolated_network,
                "limits": {k: spec[k] for k in ("cpu_seconds", "memory_bytes", "file_bytes")},
            },
            "finished_at": now(),
        }

    def spawn_background(self, sandbox: Sandbox, argv: list[str]) -> int:
        """A process left running in the generation (drills: fixture A18)."""
        process = subprocess.Popen(  # noqa: S603
            argv,
            cwd=sandbox.work,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._record_pid(sandbox, process.pid)
        return process.pid

    def _record_pid(self, sandbox: Sandbox, pid: int) -> None:
        rows = self.state.rows(
            "SELECT pids_json FROM harness_sandboxes WHERE generation=?", (sandbox.generation,)
        )
        pids = set(json.loads(rows[0]["pids_json"])) if rows else set()
        pids.add(pid)
        self.state.execute(
            "UPDATE harness_sandboxes SET pids_json=? WHERE generation=?",
            (json.dumps(sorted(pids)), sandbox.generation),
        )

    def _clear_pid(self, sandbox: Sandbox, pid: int) -> None:
        rows = self.state.rows(
            "SELECT pids_json FROM harness_sandboxes WHERE generation=?", (sandbox.generation,)
        )
        pids = set(json.loads(rows[0]["pids_json"])) - {pid} if rows else set()
        self.state.execute(
            "UPDATE harness_sandboxes SET pids_json=? WHERE generation=?",
            (json.dumps(sorted(pids)), sandbox.generation),
        )

    @staticmethod
    def _kill_group(pid: int) -> bool:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return False
        except PermissionError:  # pragma: no cover - not our process group
            return False
        return True

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:  # pragma: no cover
            return True
        return True

    # -- disposal ------------------------------------------------------------

    def destroy(self, generation: str, reason: str) -> dict:
        rows = self.state.rows("SELECT * FROM harness_sandboxes WHERE generation=?", (generation,))
        if not rows or rows[0]["state"] != "active":
            return {"generation": generation, "state": rows[0]["state"] if rows else "unknown"}
        row = rows[0]
        started = time.monotonic()
        # Fence first: once the row says destroying, no step can be handed it.
        self.state.execute(
            "UPDATE harness_sandboxes SET state='destroying', lease_until=0 WHERE generation=?",
            (generation,),
        )
        faults.hit("during_cleanup")
        killed = [pid for pid in json.loads(row["pids_json"]) if self._kill_group(pid)]
        deadline = time.monotonic() + 5
        pids = json.loads(row["pids_json"])
        while any(self._alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        root = Path(row["root"])
        removed_bytes = (
            sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) if root.exists() else 0
        )
        if root.exists():
            for path in root.rglob("*"):
                try:
                    path.chmod(0o700 if path.is_dir() else 0o600)
                except OSError:
                    pass
            shutil.rmtree(root, ignore_errors=True)
        confirmed = not root.exists() and not any(self._alive(pid) for pid in pids)
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        disposal = {
            "reason": reason,
            "processes_killed": killed,
            "bytes_removed": removed_bytes,
            "confirmed": confirmed,
            "deadline_seconds": CLEANUP_DEADLINE_SECONDS,
            "within_deadline": elapsed_ms / 1000 <= CLEANUP_DEADLINE_SECONDS,
        }
        final = "destroyed" if confirmed else "quarantined"
        self.state.execute(
            "UPDATE harness_sandboxes SET state=?, destroyed_at=?, disposal_ms=?, disposal_json=?, "
            "pids_json='[]' WHERE generation=?",
            (final, now(), elapsed_ms, json.dumps(disposal), generation),
        )
        return {"generation": generation, "state": final, "disposal_ms": elapsed_ms, **disposal}

    def destroy_for_run(self, run_id: str, reason: str) -> list[dict]:
        rows = self.state.rows(
            "SELECT generation FROM harness_sandboxes WHERE run_id=? AND state IN "
            "('active','destroying')",
            (run_id,),
        )
        out = []
        for row in rows:
            self.state.execute(
                "UPDATE harness_sandboxes SET state='active' WHERE generation=? AND "
                "state='destroying'",
                (row["generation"],),
            )
            out.append(self.destroy(row["generation"], reason))
        return out

    def reap(self) -> dict:
        """Find and dispose of every abandoned generation, independent of its worker."""
        stamp = now()
        rows = self.state.rows(
            "SELECT generation, state FROM harness_sandboxes WHERE "
            "(state='active' AND (lease_until<? OR expires_at<?)) OR state='destroying'",
            (stamp, stamp),
        )
        results = []
        for row in rows:
            if row["state"] == "destroying":
                # A worker died mid-cleanup: take the row back and finish it.
                self.state.execute(
                    "UPDATE harness_sandboxes SET state='active' WHERE generation=? "
                    "AND state='destroying'",
                    (row["generation"],),
                )
            results.append(self.destroy(row["generation"], "reaped: lease expired or abandoned"))
        return {"reaped": results, "count": len(results)}

    def list(self, run_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM harness_sandboxes"
        params: tuple = ()
        if run_id:
            sql += " WHERE run_id=?"
            params = (run_id,)
        rows = self.state.rows(sql + " ORDER BY created_at", params)
        return [
            {
                "generation": r["generation"],
                "run_id": r["run_id"],
                "profile": r["profile"],
                "state": r["state"],
                "image_digest": r["image_digest"],
                "created_at": r["created_at"],
                "lease_until": r["lease_until"],
                "destroyed_at": r["destroyed_at"],
                "disposal_ms": r["disposal_ms"],
                "disposal": json.loads(r["disposal_json"]) if r["disposal_json"] else None,
                "pids": json.loads(r["pids_json"]),
            }
            for r in rows
        ]

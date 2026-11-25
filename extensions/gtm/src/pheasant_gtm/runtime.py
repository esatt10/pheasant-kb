"""Isolated parser process launcher and bounded JSON protocol."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from hashlib import sha256
from threading import BoundedSemaphore, Event, Thread, Timer
from typing import Any

MAX_REQUEST = 6_200_000
MAX_RESPONSE = 1_900_000
_PROTOCOL = 1
_PARSER_REVISION = 6
_IMAGE = "pheasant-gtm-sandbox:local"
_SLOTS = BoundedSemaphore(2)
_TIMEOUT = 18
_CACHE: ContextVar[tuple[dict, dict] | None] = ContextVar("gtm_parser_cache", default=None)


class IsolationUnavailable(RuntimeError):
    """No usable sandbox. The checkpoint may be resumed after setup."""


class IsolatedInputRejected(ValueError):
    """The parser rejected or exhausted its budget on this publisher input."""


def _invocation() -> tuple[list[str], dict[str, str]]:
    if sys.platform == "linux":
        return [sys.executable, "-I", "-B", "-m", "pheasant_gtm.worker"], {}
    docker = shutil.which("docker")
    if docker is None:
        raise IsolationUnavailable("GTM parser needs Docker with the local sandbox image")
    image = os.environ.get("PHEASANT_GTM_SANDBOX_IMAGE", _IMAGE)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/@-]{0,240}", image):
        raise IsolationUnavailable("invalid GTM sandbox image name")
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "USERPROFILE", "HOME")
        if key in os.environ
    }
    return [
        docker,
        "run",
        "--rm",
        "--pull=never",
        "--interactive",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--memory=384m",
        "--cpus=1",
        "--pids-limit=16",
        "--log-driver=none",
        image,
    ], environment


def _exchange(command: list[str], env: dict[str, str], request: bytes) -> tuple[int, bytes]:
    with subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        close_fds=True,
    ) as process:
        stdin, stdout = process.stdin, process.stdout
        assert stdin is not None and stdout is not None
        timed_out = Event()

        def feed() -> None:
            with suppress(OSError), stdin:
                stdin.write(request)

        def stop() -> None:
            timed_out.set()
            with suppress(OSError):
                process.kill()

        sender = Thread(target=feed, daemon=True)
        timer = Timer(_TIMEOUT, stop)
        timer.daemon = True
        sender.start()
        timer.start()
        try:
            response = stdout.read(MAX_RESPONSE + 1)
            if len(response) > MAX_RESPONSE:
                process.kill()
            code = process.wait()
        finally:
            if process.poll() is None:
                with suppress(OSError):
                    process.kill()
            timer.cancel()
            sender.join(timeout=1)
        if timed_out.is_set():
            raise IsolatedInputRejected("isolated parser exceeded the time limit")
        return code, response


@contextmanager
def reuse_parsed(previous: dict, current: dict) -> Iterator[None]:
    token = _CACHE.set((previous, current))
    try:
        yield
    finally:
        _CACHE.reset(token)


def _sidecar(path: str, request: bytes) -> tuple[int, bytes]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(24)
        channel.connect(path)
        channel.sendall(request)
        channel.shutdown(socket.SHUT_WR)
        with channel.makefile("rb") as incoming:
            # One status byte precedes the normal bounded subprocess response.
            result = incoming.read(MAX_RESPONSE + 2)
    if not result:
        raise IsolationUnavailable("GTM parser sidecar returned no response")
    return result[0], result[1:]


def parse_in_sandbox(operation: str, **payload: Any) -> Any:
    cached = _CACHE.get()
    if cached is None:
        return _parse(operation, payload)
    previous, current = cached
    digest = sha256(
        json.dumps([_PROTOCOL, _PARSER_REVISION, operation, payload], sort_keys=True).encode(
            "utf-8"
        )
    ).hexdigest()
    if digest not in current:
        current[digest] = previous[digest] if digest in previous else _parse(operation, payload)
    return current[digest]


def _parse(operation: str, payload: dict) -> Any:
    if not _SLOTS.acquire(timeout=0.2):
        raise IsolationUnavailable("GTM parser sandbox is busy; retry the checkpoint")
    try:
        # JSON escaping can expand publisher bytes. Cap the actual wire payload.
        request = json.dumps(
            {"protocol": _PROTOCOL, "operation": operation, **payload}, ensure_ascii=False
        ).encode("utf-8", errors="replace")
        if len(request) > MAX_REQUEST:
            raise IsolatedInputRejected("parser request exceeded the byte limit")
        socket_path = os.environ.get("PHEASANT_GTM_PARSER_SOCKET")
        if socket_path:
            code, response = _sidecar(socket_path, request)
        else:
            command, env = _invocation()
            code, response = _exchange(command, env, request)
    except OSError as exc:
        raise IsolationUnavailable("could not launch the GTM parser sandbox") from exc
    finally:
        _SLOTS.release()
    if len(response) > MAX_RESPONSE:
        raise IsolatedInputRejected("isolated parser reply exceeded the byte limit")
    if code in {1, 70, 125, 126, 127}:
        raise IsolationUnavailable("could not start the GTM parser sandbox")
    if code:
        raise IsolatedInputRejected("isolated parser exceeded a runtime limit")
    try:
        reply = json.loads(response)
        if reply["protocol"] != _PROTOCOL:
            raise IsolationUnavailable("GTM parser sandbox protocol mismatch")
        if reply.get("rejected"):
            raise IsolatedInputRejected("isolated parser rejected the source document")
        return reply["result"]
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
        raise IsolationUnavailable("invalid reply from the GTM parser sandbox") from exc

"""Answer for the api while its database wakes, then get out of the way.

A region that scales to zero wakes on its first request, and the api cannot
start until the database accepts a login: the deployment waits for that
before it serves (`deploy/...: wait_for_database`). For that minute or so
nothing listened on the api's port, so a browser spun on a blank page and an
agent's call hung until its own timeout -- indistinguishable from an outage.

This holds the port instead. It answers ``/health`` (so the platform routes
traffic here at once), ``/region/status`` in the shape the api's own status
uses (`services/region_status.py`), and everything else with a 503 that says
the region is waking, for how long so far and how long a wake usually takes,
with ``Retry-After``; a browser gets a small page that switches to the app
once the app answers. When the database accepts a connection it stops and
exits 0, and the deployment starts the api in its place.

    python -m pheasant.deployment.wake_gate && exec pheasant serve ...

Standard library only (and psycopg, which the image has), so it starts in a
fraction of a second. It reveals nothing about the region but that it is
waking, which is why it asks for no credential.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger("pheasant.wake_gate")

#: How long a wake usually takes, end to end (`services.region_status`).
TYPICAL_WAKE_SECONDS = 90
POLL_SECONDS = 2.0
RETRY_AFTER_SECONDS = 5

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pheasant is waking up</title>
<style>
:root{{color-scheme:light dark;--bg:#f7f5f0;--fg:#1f1d1a;--muted:#6b665d;--accent:#b4652a}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1b1a18;--fg:#eeebe4;--muted:#a39d92}}}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);
color:var(--fg);font:16px/1.5 system-ui,sans-serif;padding:0 16px}}
main{{max-width:30rem}} h1{{font-size:1.25rem;margin:0 0 .5rem}}
p{{margin:.25rem 0;color:var(--muted)}} b{{color:var(--fg)}}
.dot{{display:inline-block;width:.6rem;height:.6rem;border-radius:50%;
background:var(--accent);margin-right:.5rem;animation:pulse 1.4s ease-in-out infinite}}
@keyframes pulse{{50%{{opacity:.3}}}}
</style></head><body><main>
<h1><span class="dot"></span>pheasant is waking up</h1>
<p>It was idle and scaled to zero. <b>The database is starting</b>; this usually
takes about {typical} s in all.</p>
<p>Waiting <b id="elapsed">{elapsed}</b> s so far. This page opens the app by itself.</p>
</main><script>
const started = Date.now() - {elapsed} * 1000;
setInterval(() => {{
  document.getElementById("elapsed").textContent = Math.round((Date.now() - started) / 1000);
}}, 1000);
async function check() {{
  try {{
    const r = await fetch("/region/status", {{cache: "no-store"}});
    const body = await r.json();
    if (body.served_by !== "wake_gate") {{ location.reload(); return; }}
  }} catch (e) {{}}
  setTimeout(check, 3000);
}}
setTimeout(check, 3000);
</script></body></html>
"""


class WakeGate:
    """The port's stand-in until ``ready()`` says the database answers."""

    def __init__(
        self,
        ready: Callable[[], bool],
        *,
        host: str = "0.0.0.0",
        port: int = 8765,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.ready = ready
        self.poll_seconds = poll_seconds
        self.started = time.monotonic()
        self._done = threading.Event()
        gate = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's naming
                gate._answer(self)

            def do_POST(self) -> None:  # noqa: N802
                gate._answer(self)

            def do_HEAD(self) -> None:  # noqa: N802
                gate._answer(self)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def status(self) -> dict[str, Any]:
        elapsed = time.monotonic() - self.started
        return {
            "status": "waking",
            "components": [
                {
                    "name": "database",
                    "label": "Database",
                    "state": "waking",
                    "detail": "starting after being idle; not accepting connections yet",
                },
                {
                    "name": "api",
                    "label": "API",
                    "state": "waking",
                    "detail": "waiting for the database before it starts",
                },
            ],
            "elapsed_seconds": round(elapsed, 1),
            "typical_seconds": TYPICAL_WAKE_SECONDS,
            "message": (
                "Waking up after being idle: the database is still starting "
                f"(usually about {TYPICAL_WAKE_SECONDS} s in all). Retry shortly."
            ),
            "served_by": "wake_gate",
        }

    def _answer(self, request: BaseHTTPRequestHandler) -> None:
        path = request.path.split("?", 1)[0]
        status = self.status()
        if path == "/health":
            self._send(request, 200, {"status": "ok", "waking": True})
        elif path == "/region/status":
            self._send(request, 200, status)
        elif request.command == "GET" and "text/html" in (request.headers.get("Accept") or ""):
            elapsed = int(status["elapsed_seconds"])
            page = _PAGE.format(typical=TYPICAL_WAKE_SECONDS, elapsed=elapsed)
            self._send(request, 503, page, content_type="text/html; charset=utf-8")
        else:
            # The API's and MCP's callers: the refusal both surfaces would
            # give, with the code an agent can act on and when to come back.
            self._send(
                request,
                503,
                {
                    "detail": status["message"],
                    "code": "REGION_WAKING",
                    "retryable": True,
                    "region": status,
                },
            )

    def _send(
        self,
        request: BaseHTTPRequestHandler,
        code: int,
        body: Any,
        *,
        content_type: str = "application/json",
    ) -> None:
        payload = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
        request.send_response(code)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(payload)))
        request.send_header("Cache-Control", "no-store")
        if code == 503:
            request.send_header("Retry-After", str(RETRY_AFTER_SECONDS))
        request.end_headers()
        if request.command != "HEAD":
            request.wfile.write(payload)

    def run(self) -> float:
        """Serve until the database answers; return how long that took."""

        serving = threading.Thread(target=self.server.serve_forever, daemon=True)
        serving.start()
        try:
            while not self.ready():
                logger.info(
                    "wake gate: waiting for the database (%.0f s)",
                    time.monotonic() - self.started,
                )
                time.sleep(self.poll_seconds)
        finally:
            self.server.shutdown()
            self.server.server_close()
        return time.monotonic() - self.started


def database_answers(dsn: str) -> Callable[[], bool]:
    """A login attempt, the same test the deployment's wait loop makes."""

    def ready() -> bool:
        try:
            import psycopg

            psycopg.connect(dsn, connect_timeout=5).close()
        except Exception:  # noqa: BLE001 - any failure is "not yet"
            return False
        return True

    return ready


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pheasant.deployment.wake_gate")
    parser.add_argument("--dsn-env", default="PHEASANT_DATABASE_URL")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PHEASANT_PORT", "8765")))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        logger.info("wake gate: %s is unset; nothing to wait for", args.dsn_env)
        return 0
    ready = database_answers(dsn)
    if ready():
        return 0
    took = WakeGate(ready, host=args.host, port=args.port).run()
    logger.info("wake gate: the database answered after %.0f s; starting the api", took)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Offline parser sidecar, reached through a region-private Unix socket."""

from __future__ import annotations

import argparse
import os
import socketserver
import stat
import sys
from contextlib import suppress
from pathlib import Path

from pheasant_gtm.runtime import MAX_REQUEST, MAX_RESPONSE, IsolatedInputRejected, _exchange


class ParseRequest(socketserver.StreamRequestHandler):
    timeout = 22

    def handle(self) -> None:
        try:
            request = self.rfile.read(MAX_REQUEST + 1)
            if not request:
                return  # health probe
            if len(request) > MAX_REQUEST:
                code, reply = 2, b""
            else:
                code, reply = _exchange(
                    [sys.executable, "-I", "-B", "-m", "pheasant_gtm.worker"], {}, request
                )
            if len(reply) > MAX_RESPONSE:
                code, reply = 2, b""
        except IsolatedInputRejected:
            code, reply = 2, b""
        except OSError:
            code, reply = 70, b""
        with suppress(OSError):
            self.wfile.write(bytes([code if 0 <= code < 256 else 2]) + reply)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True, type=Path)
    path = parser.parse_args().socket
    with suppress(FileNotFoundError):
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise RuntimeError("GTM socket path is occupied")
        path.unlink()
    os.umask(0o077)
    with socketserver.UnixStreamServer(str(path), ParseRequest) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()

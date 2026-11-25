"""One-shot parser worker. Only a trusted bootstrap runs before confinement."""

from __future__ import annotations

import encodings.idna  # noqa: F401 - URL normalization loads this codec after confinement
import json
import sys

from pheasant_gtm import sources
from pheasant_gtm.isolation import confine
from pheasant_gtm.runtime import _PROTOCOL, MAX_REQUEST, MAX_RESPONSE


def _dispatch(request: dict):
    target, body = request.get("target"), request["body"]
    operation = request["operation"]
    if operation == "page":
        return sources.parse(
            target,
            request["content_type"],
            body,
            request["kind"],
            embedded=request.get("embedded", True),
        ).dump()
    if operation == "board":
        return [doc.dump() for doc in sources.board_documents(target, body, request["limit"])]
    if operation == "board_summary":
        return sources.greenhouse_job_links(target, body, request["limit"])
    if operation == "search":
        return sources.search_links(body)
    raise ValueError("unknown parser operation")


def main() -> None:
    try:
        confine()
    except (OSError, RuntimeError, ValueError):
        sys.exit(70)
    try:
        data = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(data) > MAX_REQUEST:
            raise ValueError("input exceeded parser byte limit")
        request = json.loads(data)
        if request.get("protocol") != _PROTOCOL:
            sys.exit(70)
        payload = json.dumps({"protocol": _PROTOCOL, "result": _dispatch(request)}).encode()
        if len(payload) > MAX_RESPONSE:
            raise ValueError("output exceeded parser byte limit")
    except (
        AttributeError,
        LookupError,
        OSError,
        TypeError,
        ValueError,
        RecursionError,
        MemoryError,
        sources.FetchError,
    ):
        payload = json.dumps({"protocol": _PROTOCOL, "rejected": True}).encode()
    sys.stdout.buffer.write(payload)


if __name__ == "__main__":
    main()

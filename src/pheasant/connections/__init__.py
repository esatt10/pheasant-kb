"""Accounts connected while the region runs: tokens, sign-ins, keys.

``vault`` seals rows of the ``connections`` table, ``providers`` says what can
be connected and checks it, ``google`` holds Drive's sign-in, and ``resolve``
is how any process reads a secret: the environment first, then a connection.
The operations people and agents call are ``services/connections.py``.
"""

from pheasant.connections.resolve import (
    ConnectionUnavailable,
    ambient,
    environment,
    install,
    named,
    request_headers,
    secret,
    source_connection,
)

__all__ = [
    "ConnectionUnavailable",
    "ambient",
    "environment",
    "install",
    "named",
    "request_headers",
    "secret",
    "source_connection",
]

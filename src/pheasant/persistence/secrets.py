"""Resolving the database DSN without ever putting it in a config file.

A Postgres DSN carries a password. pheasant's house rule for every other
credential — connector tokens, embedding keys, the Ed25519 signing seed — is
that only the *name* of an environment variable reaches YAML
(``connector.api_key_env``, ``search.embeddings.api_key_env``,
``synapse.signing_key_ref``). Storage is not an exception, so
``storage.dsn_env`` is a variable name and there is deliberately no
``storage.dsn`` field to paste a password into.
"""

from __future__ import annotations

import os
from typing import Any

#: Checked when the config names no variable, so a container can be pointed at
#: a database purely through the environment (the usual Kubernetes shape).
DEFAULT_DSN_ENV = "PHEASANT_DATABASE_URL"


class DsnUnavailable(RuntimeError):
    """The configured backend needs a DSN and none could be resolved."""


def resolve_dsn(storage: Any) -> str:
    """Read the DSN out of the environment variable the config names.

    Raises rather than falling back to SQLite: a region told to use Postgres
    and silently given a local file would index into the wrong place and look
    entirely healthy doing it.
    """

    name = str(getattr(storage, "dsn_env", "") or DEFAULT_DSN_ENV)
    value = os.environ.get(name, "").strip()
    if not value:
        raise DsnUnavailable(
            f"storage.backend is 'postgres' but environment variable {name!r} is empty "
            "or unset. Set it to a libpq connection string, e.g. "
            "postgresql://user:password@host:5432/pheasant. Postgres is the default "
            "because pheasant-kb's configuration is (docs/pheasant-kb/); to run on "
            "SQLite in one process instead, set PHEASANT_DEFAULTS=standalone or "
            "pheasant.defaults: standalone."
        )
    return value


def redact_dsn(dsn: str) -> str:
    """A DSN safe to log or return over HTTP.

    Everything between ``//`` and the last ``@`` is credentials. Naive
    splitting on ``:`` gets this wrong for passwords containing one, which is
    exactly when a leak would be least noticed.
    """

    if "://" not in dsn:
        return "***"
    scheme, _, rest = dsn.partition("://")
    if "@" not in rest:
        return f"{scheme}://{rest}"
    credentials, _, host = rest.rpartition("@")
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}" if user else f"{scheme}://***@{host}"

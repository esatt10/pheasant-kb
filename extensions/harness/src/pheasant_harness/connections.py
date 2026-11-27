"""Working through Pheasant's connected accounts, inside a policy an operator sets.

A connection (``pheasant.connections``) is a sealed credential for an outside
service: a GitHub token, a Google Drive sign-in, a Microsoft 365 app. The
harness reads -- and, where a policy says so, writes -- through one, and the
credential never leaves this module: not to the agent, not to the model, not
to a sandbox, not to a journal event, an evidence record, an artifact, an
audit row, a log line or a response.

**Nothing is allowed until an operator says so.** A connection with no
harness policy is unusable here. A policy names the *actions* the harness may
take through it (``list``, ``read``, ``write``), the *resources* each may
touch (glob patterns over a provider's resource names, which name the repo,
folder or drive and the path), per-run request and byte quotas, and whether a
write waits for a person's approval. Actions the harness never performs
through anyone's account (``delete``, ``share``, ...) cannot be enabled.

**The policy is checked at every door.** At registration (a pointer outside
the policy is refused), in the broker before a tool call is admitted, at the
gateway before every request -- including a re-read for verification and a
read served from cache -- and before a delivery is dispatched. A narrowed or
disabled policy, a removed connection or a revoked grant therefore takes
effect on the next call, never on the next run.

**Every request is pinned.** HTTPS only, to the provider's own API host; a
redirect is never followed with the credential (Graph's pre-signed download
is fetched *without* it, and only from Microsoft's download hosts); a
next-page link that names another host is not followed and the listing says
it is incomplete; read actions may only ``GET``. A response that echoes the
credential has it removed before anything else sees the bytes.

**Every request is accounted for.** One ``harness_connection_calls`` row per
allowed, denied or failed call: who, which run, which action on which
resource, the status code and bytes, the policy version in force, and why a
denial was a denial. Never a header.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import fnmatch
import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pheasant_harness.common import (
    HarnessError,
    NotFound,
    Unsupported,
    canonical,
    digest_bytes,
    digest_value,
    loads,
    new_id,
    now,
)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

ACTIONS: dict[str, dict[str, Any]] = {
    "list": {
        "kind": "read",
        "methods": ("GET",),
        "description": "List a repository, folder or drive and each item's version.",
    },
    "read": {
        "kind": "read",
        "methods": ("GET",),
        "description": "Read one item's content at the version the listing pinned.",
    },
    "write": {
        "kind": "write",
        "methods": ("GET", "PUT", "POST", "PATCH"),
        "description": "Deliver one verified artifact: create or update one file, conditionally.",
    },
}
#: Things the harness never does through someone's account, whatever a policy says.
NEVER = ("delete", "share", "permissions", "admin", "comment", "merge", "invite")

#: The connection action each harness operation and tool needs.
OPERATION_ACTION = {
    "resolve": "list",
    "enumerate": "list",
    "compare": "list",
    "grep": "read",
    "read": "read",
    "inspect": "read",
    "materialize": "read",
    "follow": "read",
}

DEFAULT_LIMITS = {
    "max_requests_per_run": 400,
    "max_bytes_per_run": 64 * 1024 * 1024,
    "max_item_bytes": 4 * 1024 * 1024,
    "max_entries": 5000,
}
DEFAULT_APPROVAL = {"write": "required", "self_approval": False}
REDACTION = "[credential removed]"
LISTING_BYTES = 16 * 1024 * 1024
TIMEOUT_SECONDS = 20


class ConnectionDenied(HarnessError):
    """The policy (or its absence) does not allow this."""

    status = 403
    code = "HARNESS_CONNECTION_DENIED"


class QuotaExhausted(ConnectionDenied):
    """The run used what the policy gives one run through this connection."""

    code = "HARNESS_CONNECTION_QUOTA"


class ConnectionFailed(HarnessError):
    """The service could not be reached, or refused the connection's credential."""

    status = 502
    code = "HARNESS_CONNECTION_FAILED"
    retryable = True


class ConnectionMissing(HarnessError):
    """The connection is gone from this region (removed, or never connected)."""

    status = 409
    code = "HARNESS_CONNECTION_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Who is calling: a run, a verifier, a person through the API
# ---------------------------------------------------------------------------

_CALLER: contextvars.ContextVar[dict] = contextvars.ContextVar("harness_connection_caller")


@contextlib.contextmanager
def caller(*, actor: str, run_id: str | None = None, purpose: str = "direct"):
    token = _CALLER.set({"actor": actor, "run_id": run_id, "purpose": purpose})
    try:
        yield
    finally:
        _CALLER.reset(token)


def _who() -> dict:
    return _CALLER.get({"actor": "unknown", "run_id": None, "purpose": "direct"})


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------


@dataclass
class Policy:
    connection: str
    provider: str
    version: int
    actions: tuple[str, ...]
    resources: tuple[str, ...]
    write_resources: tuple[str, ...]
    limits: dict
    approval: dict
    enabled: bool
    updated_by: str
    updated_at: float

    @classmethod
    def from_row(cls, row: Any) -> Policy:
        return cls(
            connection=row["connection"],
            provider=row["provider"],
            version=int(row["version"]),
            actions=tuple(loads(row["actions_json"], [])),
            resources=tuple(loads(row["resources_json"], [])),
            write_resources=tuple(loads(row["write_resources_json"], [])),
            limits={**DEFAULT_LIMITS, **loads(row["limits_json"], {})},
            approval={**DEFAULT_APPROVAL, **loads(row["approval_json"], {})},
            enabled=bool(row["enabled"]),
            updated_by=row["updated_by"],
            updated_at=row["updated_at"],
        )

    def public(self) -> dict:
        return {
            "connection": self.connection,
            "provider": self.provider,
            "version": self.version,
            "actions": list(self.actions),
            "resources": list(self.resources),
            "write_resources": list(self.write_resources),
            "limits": self.limits,
            "approval": self.approval,
            "enabled": self.enabled,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at,
        }

    def allows(self, action: str, resource: str | None) -> str | None:
        """None when allowed; otherwise the reason, in words.

        ``resource=None`` asks only whether the action is allowed at all (a grep over a
        registration, before it knows which files it will read); every request still
        names its resource and is checked against the patterns.
        """
        if not self.enabled:
            return f"the harness policy for connection {self.connection!r} is disabled"
        if action not in self.actions:
            return (
                f"the policy for connection {self.connection!r} allows "
                f"{', '.join(self.actions) or 'nothing'}, not {action}"
            )
        if resource is None:
            return None
        patterns = self.write_resources if ACTIONS[action]["kind"] == "write" else self.resources
        if not any(fnmatch.fnmatchcase(resource, pattern) for pattern in patterns):
            return (
                f"{resource!r} is outside the {action} resources the policy for "
                f"{self.connection!r} names"
            )
        return None


def validate_policy(provider: str, body: dict) -> dict:
    """A policy as stored, or a refusal naming what is wrong with it."""
    actions = [str(a) for a in body.get("actions") or []]
    for action in actions:
        if action in NEVER:
            raise Unsupported(
                f"the harness never performs {action!r} through a connection; it cannot be enabled"
            )
        if action not in ACTIONS:
            raise Unsupported(
                f"unknown connection action {action!r}; choose from {sorted(ACTIONS)}"
            )
    adapter = ADAPTERS.get(provider)
    if adapter is None:
        raise Unsupported(
            f"the harness cannot work through {provider} connections; "
            f"supported: {', '.join(sorted(ADAPTERS))}"
        )
    unsupported = [a for a in actions if a not in adapter.actions]
    if unsupported:
        raise Unsupported(
            f"{provider} connections offer {list(adapter.actions)}; not {unsupported}"
        )
    resources = [str(r) for r in body.get("resources") or [] if str(r).strip()]
    write_resources = [str(r) for r in body.get("write_resources") or [] if str(r).strip()]
    if any(a in actions for a in ("list", "read")) and not resources:
        raise HarnessError("a policy that allows list or read names the resources they may touch")
    if "write" in actions and not write_resources:
        raise HarnessError("a policy that allows write names the write_resources it may touch")
    if write_resources and "write" not in actions:
        raise HarnessError("write_resources name where writes land; the policy allows no write")
    limits = dict(body.get("limits") or {})
    unknown = set(limits) - set(DEFAULT_LIMITS)
    if unknown:
        raise HarnessError(f"unknown policy limits {sorted(unknown)}")
    for key, value in limits.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise HarnessError(f"limit {key} must be a positive integer")
    approval = {**DEFAULT_APPROVAL, **dict(body.get("approval") or {})}
    if approval["write"] not in ("required", "none"):
        raise HarnessError("approval.write is 'required' or 'none'")
    if not isinstance(approval["self_approval"], bool):
        raise HarnessError("approval.self_approval is true or false")
    return {
        "actions": sorted(set(actions), key=list(ACTIONS).index),
        "resources": resources,
        "write_resources": write_resources,
        "limits": limits,
        "approval": approval,
        "enabled": bool(body.get("enabled", True)),
    }


# ---------------------------------------------------------------------------
# The transport. Module-level so tests stand in for the services.
# ---------------------------------------------------------------------------


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes
    truncated: bool = False

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ConnectionFailed("the service answered with something that is not JSON") from exc


class _NoRedirect(HTTPRedirectHandler):
    """urllib would follow a redirect carrying the Authorization header to any host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def transport(
    method: str, url: str, headers: dict, body: bytes | None, timeout: float, max_bytes: int
) -> Response:
    """One HTTPS request, redirects not followed, at most ``max_bytes`` read."""
    opener = build_opener(_NoRedirect)
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as handle:
            data = handle.read(max_bytes + 1)
            return Response(
                handle.status, dict(handle.headers), data[:max_bytes], len(data) > max_bytes
            )
    except HTTPError as exc:
        data = exc.read(max_bytes + 1) if exc.fp else b""
        return Response(exc.code, dict(exc.headers or {}), data[:max_bytes], len(data) > max_bytes)
    except (URLError, TimeoutError, OSError) as exc:
        raise ConnectionFailed(
            f"could not reach {urlsplit(url).hostname}: {type(exc).__name__}"
        ) from exc


# ---------------------------------------------------------------------------
# The gateway: the only code that holds a connection's credential
# ---------------------------------------------------------------------------

_CACHE_BYTES = 32 * 1024 * 1024
_cache: OrderedDict[tuple, bytes] = OrderedDict()
_cache_lock = threading.Lock()


def _cache_get(key: tuple) -> bytes | None:
    with _cache_lock:
        found = _cache.get(key)
        if found is not None:
            _cache.move_to_end(key)
        return found


def _cache_put(key: tuple, data: bytes) -> None:
    with _cache_lock:
        _cache[key] = data
        _cache.move_to_end(key)
        total = sum(len(v) for v in _cache.values())
        while total > _CACHE_BYTES and _cache:
            _, dropped = _cache.popitem(last=False)
            total -= len(dropped)


def forget_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _host_matches(host: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(host, p) for p in patterns)


class Gateway:
    def __init__(self, harness: Any):
        self.harness = harness
        self.state = harness.state
        self.kb = harness.kb_id

    # -- connections (never their secrets) ----------------------------------------

    def _install(self) -> None:
        from pheasant.connections import resolve

        if resolve.installed_state() is not self.state:
            resolve.install(self.harness.config, self.state)

    def connection(self, name: str):
        """The vault row (status, provider, details), or None. Never opened here."""
        from pheasant.connections import resolve

        self._install()
        return resolve.connection(name)

    def connections(self) -> list[dict]:
        from pheasant.connections import vault

        self._install()
        try:
            rows = vault.all_rows(self.state)
        except Exception:  # noqa: BLE001 - a region with no vault lists nothing
            rows = []
        policies = {p.connection: p for p in self.policies()}
        out = []
        for row in rows:
            adapter = ADAPTERS.get(row.provider)
            policy = policies.get(row.name)
            out.append(
                {
                    "name": row.name,
                    "provider": row.provider,
                    "status": row.status,
                    "account": (row.details or {}).get("account"),
                    "supported": adapter is not None,
                    "offers": list(adapter.actions) if adapter else [],
                    "resource_grammar": adapter.grammar if adapter else None,
                    "policy": policy.public() if policy else None,
                }
            )
        for name, policy in policies.items():
            if name not in {r.name for r in rows}:
                out.append(
                    {
                        "name": name,
                        "provider": policy.provider,
                        "status": "missing",
                        "account": None,
                        "supported": policy.provider in ADAPTERS,
                        "offers": list(ADAPTERS[policy.provider].actions)
                        if policy.provider in ADAPTERS
                        else [],
                        "resource_grammar": ADAPTERS[policy.provider].grammar
                        if policy.provider in ADAPTERS
                        else None,
                        "policy": policy.public(),
                    }
                )
        return out

    # -- policies ----------------------------------------------------------------------

    def policies(self) -> list[Policy]:
        rows = self.state.rows(
            "SELECT * FROM harness_connection_policies WHERE kb_id=? ORDER BY connection",
            (self.kb,),
        )
        return [Policy.from_row(r) for r in rows]

    def policy(self, name: str) -> Policy | None:
        rows = self.state.rows(
            "SELECT * FROM harness_connection_policies WHERE kb_id=? AND connection=?",
            (self.kb, name),
        )
        return Policy.from_row(rows[0]) if rows else None

    def set_policy(self, name: str, body: dict, actor: str) -> dict:
        row = self.connection(name)
        if row is None:
            raise NotFound(f"no connection named {name!r} on this region")
        stored = validate_policy(row.provider, body)
        current = self.policy(name)
        version = (current.version if current else 0) + 1
        values = (
            row.provider,
            version,
            canonical(stored["actions"]),
            canonical(stored["resources"]),
            canonical(stored["write_resources"]),
            canonical(stored["limits"]),
            canonical(stored["approval"]),
            int(stored["enabled"]),
            actor,
            now(),
        )
        with self.state.conn:
            self.state.conn.execute(
                "INSERT INTO harness_connection_policies(kb_id,connection,provider,version,"
                "actions_json,resources_json,write_resources_json,limits_json,approval_json,"
                "enabled,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (kb_id,connection) DO UPDATE SET provider=excluded.provider,"
                "version=excluded.version,actions_json=excluded.actions_json,"
                "resources_json=excluded.resources_json,"
                "write_resources_json=excluded.write_resources_json,"
                "limits_json=excluded.limits_json,approval_json=excluded.approval_json,"
                "enabled=excluded.enabled,updated_by=excluded.updated_by,"
                "updated_at=excluded.updated_at",
                (self.kb, name, *values),
            )
            self.state.conn.execute(
                "INSERT INTO harness_connection_policy_history(kb_id,connection,version,"
                "policy_json,changed_by,changed_at) VALUES(?,?,?,?,?,?)",
                (self.kb, name, version, canonical(stored), actor, now()),
            )
        return {"policy": self.policy(name).public(), "previous_version": version - 1}

    def disable(self, name: str, actor: str) -> dict:
        current = self.policy(name)
        if current is None:
            raise NotFound(f"connection {name!r} has no harness policy")
        body = {
            "actions": list(current.actions),
            "resources": list(current.resources),
            "write_resources": list(current.write_resources),
            "limits": current.limits,
            "approval": current.approval,
            "enabled": False,
        }
        stored = validate_policy(current.provider, body)
        with self.state.conn:
            self.state.conn.execute(
                "UPDATE harness_connection_policies SET enabled=0, version=version+1, "
                "updated_by=?, updated_at=? WHERE kb_id=? AND connection=?",
                (actor, now(), self.kb, name),
            )
            self.state.conn.execute(
                "INSERT INTO harness_connection_policy_history(kb_id,connection,version,"
                "policy_json,changed_by,changed_at) VALUES(?,?,?,?,?,?)",
                (self.kb, name, current.version + 1, canonical(stored), actor, now()),
            )
        return {"policy": self.policy(name).public()}

    def history(self, name: str) -> list[dict]:
        rows = self.state.rows(
            "SELECT * FROM harness_connection_policy_history WHERE kb_id=? AND connection=? "
            "ORDER BY version",
            (self.kb, name),
        )
        return [
            {
                "version": r["version"],
                "policy": loads(r["policy_json"], {}),
                "changed_by": r["changed_by"],
                "changed_at": r["changed_at"],
            }
            for r in rows
        ]

    # -- the audit ---------------------------------------------------------------------

    def _audit(
        self,
        name: str,
        provider: str,
        action: str,
        resource: str,
        outcome: str,
        *,
        method: str = "",
        host: str = "",
        status: int | None = None,
        size: int = 0,
        reason: str | None = None,
        policy_version: int | None = None,
        flags: dict | None = None,
        who: dict | None = None,
    ) -> None:
        who = who or _who()
        self.state.execute(
            "INSERT INTO harness_connection_calls(kb_id,id,run_id,purpose,actor,connection,"
            "provider,action,method,host,resource,outcome,status_code,bytes,reason,"
            "policy_version,flags_json,at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.kb,
                new_id("call"),
                who["run_id"],
                who["purpose"],
                who["actor"],
                name,
                provider,
                action,
                method,
                host,
                resource[:500],
                outcome,
                status,
                size,
                (reason or "")[:500] or None,
                policy_version,
                canonical(flags or {}),
                now(),
            ),
        )

    def calls(
        self, *, run_id: str | None = None, connection: str | None = None, limit: int = 200
    ) -> list[dict]:
        where, values = ["kb_id=?"], [self.kb]
        if run_id:
            where.append("run_id=?")
            values.append(run_id)
        if connection:
            where.append("connection=?")
            values.append(connection)
        rows = self.state.rows(
            f"SELECT * FROM harness_connection_calls WHERE {' AND '.join(where)} "
            "ORDER BY at DESC LIMIT ?",
            (*values, max(1, min(limit, 2000))),
        )
        return [
            {
                "id": r["id"],
                "run_id": r["run_id"],
                "purpose": r["purpose"],
                "actor": r["actor"],
                "connection": r["connection"],
                "provider": r["provider"],
                "action": r["action"],
                "method": r["method"],
                "host": r["host"],
                "resource": r["resource"],
                "outcome": r["outcome"],
                "status_code": r["status_code"],
                "bytes": r["bytes"],
                "reason": r["reason"],
                "policy_version": r["policy_version"],
                "flags": loads(r["flags_json"], {}),
                "at": r["at"],
            }
            for r in rows
        ]

    def _usage(self, run_id: str, name: str) -> tuple[int, int]:
        rows = self.state.rows(
            "SELECT COUNT(*) AS n, COALESCE(SUM(bytes),0) AS b FROM harness_connection_calls "
            "WHERE kb_id=? AND run_id=? AND connection=? AND outcome='allowed'",
            (self.kb, run_id, name),
        )
        return int(rows[0]["n"]), int(rows[0]["b"])

    # -- the checks ----------------------------------------------------------------------

    def reason(self, name: str, provider: str, action: str, resource: str | None) -> str | None:
        """Why ``action`` on ``resource`` through ``name`` is refused now, or None."""
        if action in NEVER or action not in ACTIONS:
            return f"the harness never performs {action!r} through a connection"
        row = self.connection(name)
        if row is None:
            return f"connection {name!r} is not connected on this region"
        if row.provider != provider:
            return f"connection {name!r} is now a {row.provider} connection, not {provider}"
        policy = self.policy(name)
        if policy is None:
            return (
                f"connection {name!r} has no harness policy; an operator must allow actions first"
            )
        if policy.provider != provider:
            return f"the policy for {name!r} was written for {policy.provider}"
        why = policy.allows(action, resource)
        if why:
            return why
        who = _who()
        if who["run_id"]:
            # A quota is the run's budget, not a property of any file: it stops the run.
            requests, spent = self._usage(who["run_id"], name)
            if requests >= policy.limits["max_requests_per_run"]:
                cap = policy.limits["max_requests_per_run"]
                return f"quota: this run used its {cap} requests to {name!r}"
            if spent >= policy.limits["max_bytes_per_run"]:
                cap = policy.limits["max_bytes_per_run"]
                return f"quota: this run read its {cap} bytes from {name!r}"
        return None

    def check(self, name: str, provider: str, action: str, resource: str) -> Policy:
        why = self.reason(name, provider, action, resource)
        policy = self.policy(name)
        if why:
            self._audit(
                name,
                provider,
                action,
                resource,
                "denied",
                reason=why,
                policy_version=policy.version if policy else None,
            )
            if "is not connected on this region" in why:
                raise ConnectionMissing(why)
            if why.startswith("quota: "):
                raise QuotaExhausted(why[len("quota: ") :])
            raise ConnectionDenied(why)
        return policy

    def record_denial(
        self,
        name: str,
        provider: str,
        action: str,
        resource: str | None,
        why: str,
        who: dict | None = None,
    ) -> None:
        policy = self.policy(name)
        self._audit(
            name,
            provider,
            action,
            resource or "(any)",
            "denied",
            reason=why,
            policy_version=policy.version if policy else None,
            who=who,
        )

    def approval(self, name: str) -> dict:
        policy = self.policy(name)
        return dict(policy.approval) if policy else dict(DEFAULT_APPROVAL)

    # -- requests ------------------------------------------------------------------------

    def request(
        self,
        name: str,
        provider: str,
        action: str,
        resource: str,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: dict | None = None,
        authenticated: bool = True,
        max_bytes: int | None = None,
        accept_status: tuple[int, ...] = (200,),
    ) -> Response:
        from pheasant.connections import resolve

        policy = self.check(name, provider, action, resource)
        adapter = ADAPTERS[provider](self, name)
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()

        def refuse(why: str) -> ConnectionDenied:
            self._audit(
                name,
                provider,
                action,
                resource,
                "denied",
                method=method,
                host=host,
                reason=why,
                policy_version=policy.version,
            )
            return ConnectionDenied(why)

        if method not in ACTIONS[action]["methods"]:
            raise refuse(f"a {action} may not {method}")
        if parts.scheme != "https":
            raise refuse(f"{parts.scheme}:// is refused; connections speak HTTPS only")
        if parts.username or parts.password:
            raise refuse("a URL carrying credentials is refused")
        secret_fields: dict = {}
        token = None
        if authenticated:
            try:
                row, secret_fields = resolve.named(name, providers_allowed=(provider,))
                token = resolve.token(row, secret_fields)
            except resolve.ConnectionUnavailable as exc:
                self._audit(
                    name,
                    provider,
                    action,
                    resource,
                    "failed",
                    reason=str(exc),
                    policy_version=policy.version,
                )
                raise ConnectionMissing(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 - a sign-in that cannot refresh
                self._audit(
                    name,
                    provider,
                    action,
                    resource,
                    "failed",
                    reason=type(exc).__name__,
                    policy_version=policy.version,
                )
                raise ConnectionFailed(
                    f"connection {name!r} could not produce a credential ({type(exc).__name__})"
                ) from exc
            if not token:
                raise refuse(f"connection {name!r} holds no usable credential")
            if host not in adapter.api_hosts(secret_fields):
                raise refuse(f"{host} is not {provider}'s API host; the credential stays home")
        elif not _host_matches(host, adapter.download_hosts):
            raise refuse(f"{host} is not a {provider} download host")
        sent = {"User-Agent": "pheasant-harness", **(headers or {})}
        if token:
            sent["Authorization"] = f"Bearer {token}"
        # An item's bytes are bounded by the policy; a listing or a write's answer by the
        # listing cap, so a small item limit never truncates the JSON that names the items.
        limit = int(
            max_bytes or (policy.limits["max_item_bytes"] if action == "read" else LISTING_BYTES)
        )
        try:
            response = transport(method, url, sent, body, TIMEOUT_SECONDS, limit)
        except ConnectionFailed as exc:
            self._audit(
                name,
                provider,
                action,
                resource,
                "failed",
                method=method,
                host=host,
                reason=str(exc),
                policy_version=policy.version,
            )
            raise
        flags = {}
        if token:
            # A service that echoes the credential (an error page, a debug field) does not
            # hand it to anything downstream: it is removed before the bytes leave here.
            needle = token.encode()
            if needle in response.body:
                response.body = response.body.replace(needle, REDACTION.encode())
                flags["reflected_credential_removed"] = True
            for key, value in list(response.headers.items()):
                if token in str(value):
                    response.headers[key] = str(value).replace(token, REDACTION)
                    flags["reflected_credential_removed"] = True
        if response.truncated:
            flags["truncated_at_bytes"] = limit
        outcome = "allowed" if response.status in accept_status else "failed"
        self._audit(
            name,
            provider,
            action,
            resource,
            outcome,
            method=method,
            host=host,
            status=response.status,
            size=len(response.body),
            reason=None if outcome == "allowed" else f"HTTP {response.status}",
            policy_version=policy.version,
            flags=flags,
        )
        if response.status in accept_status:
            return response
        if 300 <= response.status < 400:
            raise ConnectionDenied(
                f"{provider} answered {response.status} with a redirect; the harness does not "
                "follow a redirect with a connection's credential"
            )
        if response.status in (401, 403):
            raise ConnectionFailed(
                f"{provider} refused connection {name!r} ({response.status}); reconnect it or "
                "share the item with its account"
            )
        if response.status == 404:
            raise NotFound(f"{provider} has no such item, or {name!r} cannot see it")
        raise ConnectionFailed(f"{provider} answered {response.status}")

    def cached_read(self, key: tuple, fetch, *, audit: tuple[str, str, str]) -> bytes:
        """Bytes for one pinned item version. The caller checked the policy first; a hit is
        still written to the audit, because who saw what does not depend on a cache."""
        full = (self.kb, *key)
        found = _cache_get(full)
        if found is not None:
            name, provider, resource = audit
            policy = self.policy(name)
            self._audit(
                name,
                provider,
                "read",
                resource,
                "cached",
                size=len(found),
                policy_version=policy.version if policy else None,
            )
            return found
        data = fetch()
        _cache_put(full, data)
        return data


# ---------------------------------------------------------------------------
# Provider adapters. Each names its resources, pins revisions, and reads.
# ---------------------------------------------------------------------------

BINARY_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".zip", ".gz", ".tgz",
    ".xz", ".bz2", ".7z", ".jar", ".whl", ".so", ".dll", ".exe", ".woff", ".woff2", ".ttf",
    ".mp3", ".mp4", ".mov", ".bin",
)  # fmt: skip


def _git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _gap(reason: str) -> dict:
    """A listing the provider did not finish: not a file, and never an absence."""
    return {
        "path": "(provider listing)",
        "kind": "unreadable",
        "reason": reason,
        "listing_gap": True,
        "hidden": False,
        "ignored": False,
    }


@dataclass
class Pinned:
    revision: str
    kind: str
    entries: list[dict]
    revision_time: float | None
    time_bound: bool
    extra: dict = field(default_factory=dict)


class Adapter:
    provider = "base"
    actions: tuple[str, ...] = ("list", "read")
    download_hosts: tuple[str, ...] = ()
    grammar = ""
    idempotent_writes = False

    def __init__(self, gateway: Gateway, name: str):
        self.gw = gateway
        self.name = name

    def api_hosts(self, fields: dict) -> set[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def call(self, action: str, resource: str, method: str, url: str, **kwargs) -> Response:
        return self.gw.request(self.name, self.provider, action, resource, method, url, **kwargs)

    def _base(self) -> str:
        from pheasant.connections import resolve

        row = resolve.connection(self.name)
        details = dict(getattr(row, "details", None) or {})
        return str(details.get("api_url") or self.default_base).rstrip("/")


# -- GitHub ------------------------------------------------------------------------------


class GitHubAdapter(Adapter):
    provider = "github"
    actions = ("list", "read", "write")
    default_base = "https://api.github.com"
    grammar = "owner/repo:path (reads); owner/repo@branch:path (writes)"
    idempotent_writes = True

    def api_hosts(self, fields: dict) -> set[str]:
        base = str(fields.get("api_url") or self.default_base)
        return {(urlsplit(base).hostname or "").lower()}

    @staticmethod
    def parse(path: str) -> dict:
        parts = [p for p in path.strip("/").split("/") if p]
        if len(parts) < 2:
            raise HarnessError(
                "a GitHub connection URI names owner/repo: connection://<name>/<owner>/<repo>[/<path>]"
            )
        return {"owner": parts[0], "repo": parts[1], "prefix": "/".join(parts[2:])}

    @staticmethod
    def list_resource(scope: dict) -> str:
        prefix = scope.get("prefix") or ""
        return f"{scope['owner']}/{scope['repo']}:{prefix + '/' if prefix else ''}"

    @staticmethod
    def item_resource(scope: dict, entry: dict) -> str:
        prefix = scope.get("prefix") or ""
        remote = entry.get("remote") or (f"{prefix}/{entry['path']}" if prefix else entry["path"])
        return f"{scope['owner']}/{scope['repo']}:{remote}"

    def _repo(self, scope: dict) -> str:
        return f"{self._base()}/repos/{quote(scope['owner'])}/{quote(scope['repo'])}"

    def resolve(self, scope: dict, selector: str | None) -> Pinned:
        resource = self.list_resource(scope)
        ref = selector or scope.get("ref") or "HEAD"
        commit = self.call(
            "list",
            resource,
            "GET",
            f"{self._repo(scope)}/commits/{quote(ref, safe='')}",
            headers={"Accept": "application/vnd.github+json"},
        ).json()
        sha = str(commit.get("sha") or "")
        if len(sha) != 40:
            raise ConnectionFailed("GitHub answered a commit without its sha")
        tree_sha = ((commit.get("commit") or {}).get("tree") or {}).get("sha") or sha
        tree = self.call(
            "list",
            resource,
            "GET",
            f"{self._repo(scope)}/git/trees/{tree_sha}?recursive=1",
            headers={"Accept": "application/vnd.github+json"},
        ).json()
        prefix = scope.get("prefix") or ""
        entries: list[dict] = []
        for item in tree.get("tree") or []:
            remote = str(item.get("path") or "")
            if prefix and not (remote == prefix or remote.startswith(prefix + "/")):
                continue
            rel = remote[len(prefix) + 1 :] if prefix else remote
            if not rel:
                continue
            base = {
                "path": rel,
                "remote": remote,
                "hidden": any(p.startswith(".") for p in rel.split("/")),
                "ignored": False,
            }
            if item.get("type") == "blob" and item.get("mode") == "120000":
                entries.append({**base, "kind": "unreadable", "reason": "symbolic link"})
            elif item.get("type") == "commit":
                entries.append({**base, "kind": "unreadable", "reason": "submodule"})
            elif item.get("type") == "blob":
                from pheasant_harness.registry import is_generated

                entries.append(
                    {
                        **base,
                        "kind": "file",
                        "oid": item["sha"],
                        "size": int(item.get("size") or 0),
                        "digest": "git:" + item["sha"],
                        "binary": rel.lower().endswith(BINARY_SUFFIXES),
                        "generated": is_generated(rel),
                    }
                )
        entries.sort(key=lambda e: e["path"])
        if tree.get("truncated"):
            entries.append(
                _gap("GitHub truncated the tree listing; entries beyond it were not listed")
            )
        date = ((commit.get("commit") or {}).get("committer") or {}).get("date")
        return Pinned(sha, "commit", entries, _iso(date), time_bound=False, extra={"ref": ref})

    def read(self, scope: dict, entry: dict) -> bytes:
        def fetch() -> bytes:
            response = self.call(
                "read",
                self.item_resource(scope, entry),
                "GET",
                f"{self._repo(scope)}/git/blobs/{entry['oid']}",
                headers={"Accept": "application/vnd.github.raw"},
            )
            if response.truncated:
                raise _too_large(entry)
            data = response.body
            if _git_blob_sha(data) != entry["oid"]:
                # The bytes are not the object the commit names: never cite them.
                raise ConnectionFailed(
                    f"{entry['path']}: GitHub returned bytes that do not hash to blob "
                    f"{entry['oid'][:12]}; refusing to read them as that revision"
                )
            return data

        return self.gw.cached_read(
            (self.name, "git", entry["oid"]),
            fetch,
            audit=(self.name, self.provider, self.item_resource(scope, entry)),
        )

    # writes --------------------------------------------------------------------------

    @staticmethod
    def parse_target(path: str, query: dict) -> dict:
        scope = GitHubAdapter.parse(path)
        if not scope["prefix"]:
            raise HarnessError("a GitHub delivery target names a file: <owner>/<repo>/<path>")
        branch = (query.get("branch") or [""])[0]
        if not branch:
            raise HarnessError("a GitHub delivery target names its branch: ...?branch=<name>")
        return {**scope, "path": scope["prefix"], "branch": branch}

    @staticmethod
    def write_resource(target: dict) -> str:
        return f"{target['owner']}/{target['repo']}@{target['branch']}:{target['path']}"

    def _current(self, target: dict) -> dict | None:
        url = f"{self._repo(target)}/contents/{quote(target['path'])}?" + urlencode(
            {"ref": target["branch"]}
        )
        response = self.call(
            "write",
            self.write_resource(target),
            "GET",
            url,
            headers={"Accept": "application/vnd.github+json"},
            accept_status=(200, 404),
        )
        if response.status == 404:
            return None
        found = response.json()
        content = base64.b64decode(found.get("content") or "") if found.get("content") else b""
        return {"sha": found.get("sha"), "digest": digest_bytes(content)}

    def write(self, target: dict, payload: bytes, operation_id: str, expected: str | None) -> dict:
        current = self._current(target)
        blob = _git_blob_sha(payload)
        if current is not None and current["sha"] == blob:
            return {
                "downstream_id": self.write_resource(target),
                "downstream_revision": "git:" + blob,
            }
        if expected is not None:
            held = current["digest"] if current else "absent"
            if held != expected:
                from pheasant_harness.delivery import DeliveryConflict

                raise DeliveryConflict(
                    f"{self.write_resource(target)} is at {held[:19]}, not the expected "
                    f"{expected[:19]}; the intervening change is preserved"
                )
        body = {
            "message": f"pheasant harness delivery {operation_id}",
            "content": base64.b64encode(payload).decode(),
            "branch": target["branch"],
        }
        if current is not None:
            body["sha"] = current["sha"]
        response = self.call(
            "write",
            self.write_resource(target),
            "PUT",
            f"{self._repo(target)}/contents/{quote(target['path'])}",
            body=json.dumps(body).encode(),
            headers={"Accept": "application/vnd.github+json", "Content-Type": "application/json"},
            accept_status=(200, 201, 409, 422),
        )
        if response.status in (409, 422):
            from pheasant_harness.delivery import DeliveryConflict

            raise DeliveryConflict(
                f"{self.write_resource(target)} changed while the delivery was being written "
                f"(GitHub {response.status}); nothing was overwritten"
            )
        result = response.json()
        commit = (result.get("commit") or {}).get("sha")
        return {
            "downstream_id": f"{target['owner']}/{target['repo']}@{commit}:{target['path']}",
            "downstream_revision": "git:" + ((result.get("content") or {}).get("sha") or blob),
        }

    def reconcile(self, target: dict, operation_id: str, payload_digest: str) -> dict:
        url = f"{self._repo(target)}/commits?" + urlencode(
            {"sha": target["branch"], "path": target["path"], "per_page": 20}
        )
        response = self.call(
            "write",
            self.write_resource(target),
            "GET",
            url,
            headers={"Accept": "application/vnd.github+json"},
            accept_status=(200, 404, 409),
        )
        if response.status == 200:
            for item in response.json() or []:
                message = ((item.get("commit") or {}).get("message")) or ""
                if operation_id in message:
                    return {
                        "status": "committed",
                        "downstream_id": f"{target['owner']}/{target['repo']}@{item.get('sha')}:"
                        f"{target['path']}",
                        "downstream_revision": payload_digest,
                    }
        current = self._current(target)
        if current is not None and current["digest"] == payload_digest:
            return {
                "status": "committed",
                "downstream_id": self.write_resource(target),
                "downstream_revision": payload_digest,
            }
        return {"status": "absent"}


# -- Google Drive --------------------------------------------------------------------------

DRIVE_FOLDER = "application/vnd.google-apps.folder"
DRIVE_EXPORTS = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
}
DRIVE_FIELDS = "nextPageToken,files(id,name,mimeType,size,md5Checksum,modifiedTime)"


class DriveAdapter(Adapter):
    provider = "google_drive"
    actions = ("list", "read", "write")
    default_base = "https://www.googleapis.com/drive/v3"
    upload_base = "https://www.googleapis.com/upload/drive/v3"
    grammar = "<folder id>/<path>"
    idempotent_writes = True

    def api_hosts(self, fields: dict) -> set[str]:
        return {"www.googleapis.com"}

    @staticmethod
    def parse(path: str) -> dict:
        parts = [p for p in path.strip("/").split("/") if p]
        if not parts:
            raise HarnessError(
                "a Drive connection URI names a folder: connection://<name>/<folder id>"
            )
        return {"folder": parts[0], "prefix": "/".join(parts[1:])}

    @staticmethod
    def list_resource(scope: dict) -> str:
        prefix = scope.get("prefix") or ""
        return f"{scope['folder']}/{prefix + '/' if prefix else ''}"

    @staticmethod
    def item_resource(scope: dict, entry: dict) -> str:
        return f"{scope['folder']}/{entry['path']}"

    def _list(self, resource: str, folder: str, limit: int) -> tuple[list[dict], bool]:
        items, page, complete = [], None, True
        while True:
            params = {
                "q": f"'{folder}' in parents and trashed = false",
                "fields": DRIVE_FIELDS,
                "pageSize": 200,
                "orderBy": "name",
            }
            if page:
                params["pageToken"] = page
            body = self.call(
                "list", resource, "GET", f"{self.default_base}/files?{urlencode(params)}"
            ).json()
            items.extend(body.get("files") or [])
            page = body.get("nextPageToken")
            if not page:
                break
            if len(items) >= limit:
                complete = False
                break
        return items, complete

    def resolve(self, scope: dict, selector: str | None) -> Pinned:
        if selector:
            raise Unsupported(
                "Drive keeps no addressable folder revisions; resolve without a selector"
            )
        limit = self.gw.policy(self.name).limits["max_entries"]
        resource = self.list_resource(scope)
        entries: list[dict] = []
        gaps: list[str] = []
        queue = [(scope["folder"], "")]
        latest = None
        while queue:
            folder, base = queue.pop(0)
            items, complete = self._list(resource, folder, limit)
            if not complete:
                gaps.append(f"Drive listing of {base or 'the folder'} stopped at {limit} entries")
            seen: dict[str, int] = {}
            for item in items:
                name = str(item.get("name") or item.get("id"))
                seen[name] = seen.get(name, 0) + 1
            for item in items:
                name = str(item.get("name") or item.get("id"))
                if seen[name] > 1:
                    name = f"{name} ({item['id']})"  # two files, one name: both stay addressable
                mime = str(item.get("mimeType") or "")
                path = f"{base}{name}"
                modified = item.get("modifiedTime")
                latest = max(latest or "", modified or "") or latest
                common = {
                    "path": path,
                    "id": item["id"],
                    "hidden": name.startswith("."),
                    "ignored": False,
                }
                if mime == DRIVE_FOLDER:
                    if len(entries) < limit:
                        queue.append((item["id"], path + "/"))
                    continue
                if mime in DRIVE_EXPORTS:
                    export, suffix = DRIVE_EXPORTS[mime]
                    entries.append(
                        {
                            **common,
                            "path": path + suffix,
                            "kind": "file",
                            "export": export,
                            "modified": modified,
                            "size": 0,
                            "digest": "drive-modified:" + str(modified),
                            "binary": False,
                            "generated": False,
                        }
                    )
                elif mime.startswith("application/vnd.google-apps."):
                    entries.append(
                        {**common, "kind": "unreadable", "reason": f"no text export for {mime}"}
                    )
                elif not item.get("md5Checksum"):
                    entries.append(
                        {**common, "kind": "unreadable", "reason": "Drive reports no checksum"}
                    )
                else:
                    entries.append(
                        {
                            **common,
                            "kind": "file",
                            "md5": item["md5Checksum"],
                            "size": int(item.get("size") or 0),
                            "digest": "md5:" + item["md5Checksum"],
                            "binary": path.lower().endswith(BINARY_SUFFIXES),
                            "generated": False,
                        }
                    )
        prefix = scope.get("prefix") or ""
        if prefix:
            entries = [
                {**e, "path": e["path"][len(prefix) + 1 :]}
                for e in entries
                if e["path"].startswith(prefix + "/")
            ]
        entries.sort(key=lambda e: e["path"])
        entries.extend(_gap(g) for g in gaps)
        return Pinned(
            digest_value(entries), "drive-manifest", entries, _iso(latest), time_bound=True
        )

    def _modified(self, resource: str, entry: dict) -> str | None:
        meta = self.call(
            "read",
            resource,
            "GET",
            f"{self.default_base}/files/{quote(entry['id'])}?fields=modifiedTime,md5Checksum",
        ).json()
        return meta.get("modifiedTime")

    def read(self, scope: dict, entry: dict) -> bytes:
        from pheasant_harness.registry import RevisionMoved

        resource = self.item_resource(scope, entry)
        if entry.get("export"):
            if self._modified(resource, entry) != entry.get("modified"):
                raise RevisionMoved(
                    f"{entry['path']} was edited in Drive after this revision was resolved"
                )

            def fetch_export() -> bytes:
                response = self.call(
                    "read",
                    resource,
                    "GET",
                    f"{self.default_base}/files/{quote(entry['id'])}/export?"
                    + urlencode({"mimeType": entry["export"]}),
                )
                if response.truncated:
                    raise _too_large(entry)
                # An export has no checksum: check the document did not move under it.
                if self._modified(resource, entry) != entry.get("modified"):
                    raise RevisionMoved(f"{entry['path']} was edited in Drive while it was read")
                return response.body

            return self.gw.cached_read(
                (self.name, "drive", entry["id"], entry["modified"]),
                fetch_export,
                audit=(self.name, self.provider, resource),
            )

        def fetch() -> bytes:
            response = self.call(
                "read", resource, "GET", f"{self.default_base}/files/{quote(entry['id'])}?alt=media"
            )
            if response.truncated:
                raise _too_large(entry)
            if hashlib.md5(response.body, usedforsecurity=False).hexdigest() != entry["md5"]:
                raise RevisionMoved(
                    f"{entry['path']} changed in Drive after this revision was resolved"
                )
            return response.body

        return self.gw.cached_read(
            (self.name, "drive", entry["id"], entry["md5"]),
            fetch,
            audit=(self.name, self.provider, resource),
        )

    # writes --------------------------------------------------------------------------

    @staticmethod
    def parse_target(path: str, query: dict) -> dict:
        parts = [p for p in path.strip("/").split("/") if p]
        if len(parts) != 2:
            raise HarnessError("a Drive delivery target is <folder id>/<file name>")
        return {"folder": parts[0], "name": parts[1]}

    @staticmethod
    def write_resource(target: dict) -> str:
        return f"{target['folder']}/{target['name']}"

    def _find(self, target: dict, query: str) -> list[dict]:
        params = {"q": query, "fields": "files(id,name,md5Checksum,appProperties)", "pageSize": 10}
        return (
            self.call(
                "write",
                self.write_resource(target),
                "GET",
                f"{self.default_base}/files?{urlencode(params)}",
            )
            .json()
            .get("files")
            or []
        )

    def _by_operation(self, target: dict, operation_id: str) -> list[dict]:
        return self._find(
            target,
            f"'{target['folder']}' in parents and trashed = false and appProperties has "
            f"{{ key='pheasantOperation' and value='{operation_id}' }}",
        )

    def write(self, target: dict, payload: bytes, operation_id: str, expected: str | None) -> dict:
        md5 = hashlib.md5(payload, usedforsecurity=False).hexdigest()
        prior = self._by_operation(target, operation_id)
        if prior:
            return {
                "downstream_id": prior[0]["id"],
                "downstream_revision": "md5:" + str(prior[0].get("md5Checksum")),
            }
        name = target["name"].replace("'", "\\'")
        existing = self._find(
            target, f"'{target['folder']}' in parents and trashed = false and name = '{name}'"
        )
        if expected is not None:
            held = "md5:" + str(existing[0].get("md5Checksum")) if existing else "absent"
            if held != expected:
                from pheasant_harness.delivery import DeliveryConflict

                raise DeliveryConflict(
                    f"{self.write_resource(target)} is at {held}, not the expected {expected}; "
                    "the intervening change is preserved"
                )
        boundary = "pheasant-" + operation_id[-16:]
        metadata = {"appProperties": {"pheasantOperation": operation_id}}
        if not existing:
            metadata.update({"name": target["name"], "parents": [target["folder"]]})
        body = (
            (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(metadata)}\r\n--{boundary}\r\n"
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + payload
            + f"\r\n--{boundary}--\r\n".encode()
        )
        if existing:
            method, url = (
                "PATCH",
                f"{self.upload_base}/files/{quote(existing[0]['id'])}?uploadType=multipart",
            )
        else:
            method, url = "POST", f"{self.upload_base}/files?uploadType=multipart"
        created = self.call(
            "write",
            self.write_resource(target),
            method,
            url + "&fields=id,md5Checksum",
            body=body,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        ).json()
        return {
            "downstream_id": created.get("id"),
            "downstream_revision": "md5:" + str(created.get("md5Checksum") or md5),
        }

    def reconcile(self, target: dict, operation_id: str, payload_digest: str) -> dict:
        found = self._by_operation(target, operation_id)
        if not found:
            return {"status": "absent"}
        return {
            "status": "committed",
            "downstream_id": found[0]["id"],
            "downstream_revision": "md5:" + str(found[0].get("md5Checksum")),
        }


# -- Microsoft 365 (OneDrive / SharePoint document libraries) -------------------------------

GRAPH = "https://graph.microsoft.com/v1.0"


class GraphAdapter(Adapter):
    provider = "microsoft_365"
    actions = ("list", "read")
    default_base = GRAPH
    download_hosts = ("*.sharepoint.com", "*.sharepoint.us", "*.svc.ms", "*.files.1drv.com")
    grammar = "<drive id>:<path>"

    def api_hosts(self, fields: dict) -> set[str]:
        return {"graph.microsoft.com"}

    @staticmethod
    def parse(path: str) -> dict:
        parts = [p for p in path.strip("/").split("/") if p]
        if not parts:
            raise HarnessError(
                "a Microsoft 365 connection URI names a drive: "
                "connection://<name>/<drive id>[/<folder>]"
            )
        return {"drive": parts[0], "prefix": "/".join(parts[1:])}

    @staticmethod
    def list_resource(scope: dict) -> str:
        prefix = scope.get("prefix") or ""
        return f"{scope['drive']}:{prefix + '/' if prefix else ''}"

    @staticmethod
    def item_resource(scope: dict, entry: dict) -> str:
        prefix = scope.get("prefix") or ""
        return f"{scope['drive']}:{prefix + '/' if prefix else ''}{entry['path']}"

    def _children_url(self, drive: str, path: str) -> str:
        select = urlencode(
            {"$select": "id,name,size,file,folder,cTag,lastModifiedDateTime", "$top": 200}
        )
        if path:
            return f"{GRAPH}/drives/{quote(drive)}/root:/{quote(path)}:/children?{select}"
        return f"{GRAPH}/drives/{quote(drive)}/root/children?{select}"

    def resolve(self, scope: dict, selector: str | None) -> Pinned:
        if selector:
            raise Unsupported(
                "a drive keeps no addressable folder revisions; resolve without a selector"
            )
        resource = self.list_resource(scope)
        limit = self.gw.policy(self.name).limits["max_entries"]
        prefix = scope.get("prefix") or ""
        queue = [(prefix, "")]
        entries: list[dict] = []
        gaps: list[str] = []
        latest = None
        while queue:
            remote, base = queue.pop(0)
            url = self._children_url(scope["drive"], remote)
            while url:
                body = self.call("list", resource, "GET", url).json()
                for item in body.get("value") or []:
                    name = str(item.get("name") or item.get("id"))
                    path = base + name
                    latest = max(latest or "", item.get("lastModifiedDateTime") or "") or latest
                    common = {
                        "path": path,
                        "id": item["id"],
                        "hidden": name.startswith("."),
                        "ignored": False,
                    }
                    if "folder" in item:
                        queue.append(((remote + "/" if remote else "") + name, path + "/"))
                        continue
                    hashes = ((item.get("file") or {}).get("hashes")) or {}
                    sha = str(hashes.get("sha256Hash") or "").lower()
                    entries.append(
                        {
                            **common,
                            "kind": "file",
                            "size": int(item.get("size") or 0),
                            "ctag": item.get("cTag"),
                            "sha256": sha or None,
                            "digest": ("sha256:" + sha) if sha else "ctag:" + str(item.get("cTag")),
                            "binary": path.lower().endswith(BINARY_SUFFIXES),
                            "generated": False,
                        }
                    )
                following = body.get("@odata.nextLink")
                if (
                    following
                    and (urlsplit(following).hostname or "").lower() != "graph.microsoft.com"
                ):
                    gaps.append(
                        "the service sent a next-page link to another host; it was not followed"
                    )
                    following = None
                if len(entries) >= limit and following:
                    gaps.append(f"listing stopped at {limit} entries")
                    following = None
                url = following
        entries.sort(key=lambda e: e["path"])
        entries.extend(_gap(g) for g in gaps)
        return Pinned(
            digest_value(entries), "drive-manifest", entries, _iso(latest), time_bound=True
        )

    def read(self, scope: dict, entry: dict) -> bytes:
        from pheasant_harness.registry import RevisionMoved

        resource = self.item_resource(scope, entry)

        def fetch() -> bytes:
            response = self.call(
                "read",
                resource,
                "GET",
                f"{GRAPH}/drives/{quote(scope['drive'])}/items/{quote(entry['id'])}/content",
                accept_status=(200, 302),
            )
            if response.status == 302:
                location = {k.lower(): v for k, v in response.headers.items()}.get("location", "")
                # A pre-signed download: fetched without the credential, from Microsoft only.
                response = self.call("read", resource, "GET", location, authenticated=False)
            if response.truncated:
                raise _too_large(entry)
            data = response.body
            if entry.get("sha256"):
                if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                    raise RevisionMoved(f"{entry['path']} changed after this revision was resolved")
            else:
                meta = self.call(
                    "read",
                    resource,
                    "GET",
                    f"{GRAPH}/drives/{quote(scope['drive'])}/items/{quote(entry['id'])}?$select=cTag",
                ).json()
                if meta.get("cTag") != entry.get("ctag"):
                    raise RevisionMoved(f"{entry['path']} changed after this revision was resolved")
            return data

        return self.gw.cached_read(
            (self.name, "graph", entry["id"], entry["digest"]),
            fetch,
            audit=(self.name, self.provider, resource),
        )


ADAPTERS: dict[str, type[Adapter]] = {
    cls.provider: cls for cls in (GitHubAdapter, DriveAdapter, GraphAdapter)
}


def _too_large(entry: dict):
    from pheasant_harness.registry import EntryUnreadable

    return EntryUnreadable(f"{entry['path']} is larger than the policy's max_item_bytes")


def _iso(value: str | None) -> float | None:
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# URIs
# ---------------------------------------------------------------------------


def parse_connection_uri(harness: Any, uri: str) -> dict:
    """``connection://<name>/<provider path>`` as a registration scope."""
    parts = urlsplit(uri)
    name = parts.netloc
    if not name:
        raise HarnessError("connection:// URIs name a connection: connection://<name>/<path>")
    row = harness.connections.connection(name)
    if row is None:
        raise ConnectionMissing(f"connection {name!r} is not connected on this region")
    adapter = ADAPTERS.get(row.provider)
    if adapter is None:
        raise Unsupported(
            f"connection {name!r} is a {row.provider} connection; the harness reads through "
            f"{', '.join(sorted(ADAPTERS))}"
        )
    from urllib.parse import unquote

    return {"connection": name, "provider": row.provider, **adapter.parse(unquote(parts.path))}


def parse_target(harness: Any, target: str) -> tuple[Adapter, dict]:
    from urllib.parse import parse_qs, unquote

    parts = urlsplit(target)
    if parts.scheme != "connection" or not parts.netloc:
        raise HarnessError("a connection delivery target is connection://<name>/<path>")
    row = harness.connections.connection(parts.netloc)
    if row is None:
        raise ConnectionMissing(f"connection {parts.netloc!r} is not connected on this region")
    cls = ADAPTERS.get(row.provider)
    if cls is None or "write" not in cls.actions:
        raise Unsupported(f"the harness does not write through {row.provider} connections")
    adapter = cls(harness.connections, parts.netloc)
    return adapter, adapter.parse_target(unquote(parts.path), parse_qs(parts.query))


def write_reason(harness: Any, target: str) -> str | None:
    """Why a delivery to ``target`` is refused now, or None (no request is made)."""
    try:
        adapter, parsed = parse_target(harness, target)
    except HarnessError as exc:
        return str(exc)
    return harness.connections.reason(
        adapter.name, adapter.provider, "write", adapter.write_resource(parsed)
    )


def adapter_for(harness: Any, scope: dict) -> Adapter:
    return ADAPTERS[scope["provider"]](harness.connections, scope["connection"])


def operation_reason(
    harness: Any, scope: dict, operation: str, path: str | None = None
) -> str | None:
    """Why ``operation`` on a connection registration is refused now, or None.

    A listing is checked against the registration's own resource; a read of a named
    path against that path's; a read with no path yet (grep, materialize) only against
    whether reads are allowed at all -- each file it then reads is checked by name.
    """
    action = OPERATION_ACTION.get(operation)
    if action is None:
        return None
    adapter = adapter_for(harness, scope)
    if action == "list":
        resource = adapter.list_resource(scope)
    elif path:
        resource = adapter.item_resource(scope, {"path": path})
    else:
        resource = None
    return harness.connections.reason(scope["connection"], scope["provider"], action, resource)


def registration_reason(harness: Any, scope: dict, operations: tuple[str, ...]) -> str | None:
    """Why a registration over ``scope`` offering ``operations`` is refused, or None."""
    for operation in sorted(set(operations), key=lambda op: OPERATION_ACTION.get(op, "")):
        why = operation_reason(harness, scope, operation)
        if why:
            return why
    return None


__all__ = [
    "ACTIONS",
    "ADAPTERS",
    "ConnectionDenied",
    "ConnectionFailed",
    "ConnectionMissing",
    "Gateway",
    "NEVER",
    "OPERATION_ACTION",
    "Policy",
    "caller",
    "parse_connection_uri",
    "transport",
    "validate_policy",
]

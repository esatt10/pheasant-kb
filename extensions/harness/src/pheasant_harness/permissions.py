"""Harness privileges on the shared HTTP and MCP surfaces.

Reading, investigating and running are ``knowledge.use``: a run executes only
with its caller's own grants, which the broker checks on every tool call.
Changing what may be read (registrations, grants), resolving an uncertain
external effect and operating sandboxes are ``sources.manage``. Deciding what
the harness may do through a connected account is ``security.manage``, the
permission that connects accounts in the first place; approving a write through
one is an operator's decision, which the service checks itself.
"""

HTTP = {
    ("GET", "/harness/contract"): "knowledge.use",
    ("GET", "/harness/registrations"): "knowledge.use",
    ("POST", "/harness/registrations"): "sources.manage",
    ("POST", "/harness/registrations/{registration_id}/resolve"): "knowledge.use",
    ("POST", "/harness/registrations/{registration_id}/grants"): "sources.manage",
    ("GET", "/harness/registrations/{registration_id}/entries"): "knowledge.use",
    ("POST", "/harness/grep"): "knowledge.use",
    ("POST", "/harness/read"): "knowledge.use",
    ("POST", "/harness/discover"): "knowledge.use",
    ("GET", "/harness/runs"): "knowledge.use",
    ("POST", "/harness/runs"): "knowledge.use",
    ("GET", "/harness/runs/{run_id}"): "knowledge.use",
    ("GET", "/harness/runs/{run_id}/detail"): "knowledge.use",
    ("GET", "/harness/runs/{run_id}/events"): "knowledge.use",
    ("GET", "/harness/runs/{run_id}/events/stream"): "knowledge.use",
    ("POST", "/harness/runs/{run_id}/step"): "knowledge.use",
    ("POST", "/harness/runs/{run_id}/resume"): "knowledge.use",
    ("POST", "/harness/runs/{run_id}/cancel"): "knowledge.use",
    ("GET", "/harness/runs/{run_id}/integrity"): "knowledge.use",
    ("GET", "/harness/runs/{run_id}/artifacts/{digest}"): "knowledge.use",
    ("GET", "/harness/runs/{run_id}/artifacts/{digest}/raw"): "knowledge.use",
    ("POST", "/harness/runs/{run_id}/effects/{operation_id}/resolve"): "sources.manage",
    ("POST", "/harness/runs/{run_id}/approvals/{operation_id}"): "knowledge.use",
    ("GET", "/harness/connections"): "sources.manage",
    ("GET", "/harness/connections/usable"): "knowledge.use",
    ("GET", "/harness/connections/{connection}/browse"): "knowledge.use",
    ("POST", "/harness/attachments"): "knowledge.use",
    ("GET", "/harness/connections/calls"): "knowledge.use",
    ("PUT", "/harness/connections/{connection}/policy"): "security.manage",
    ("POST", "/harness/connections/{connection}/policy/disable"): "security.manage",
    ("GET", "/harness/connections/{connection}/policy/history"): "sources.manage",
    ("GET", "/harness/sandboxes"): "sources.manage",
    ("POST", "/harness/sandboxes/reap"): "sources.manage",
}

MCP = {
    "harness_contract": "knowledge.use",
    "harness_register": "sources.manage",
    "harness_list_registrations": "knowledge.use",
    "harness_resolve": "knowledge.use",
    "harness_set_grant": "sources.manage",
    "harness_enumerate": "knowledge.use",
    "harness_grep": "knowledge.use",
    "harness_read": "knowledge.use",
    "harness_discover": "knowledge.use",
    "harness_start_run": "knowledge.use",
    "harness_list_runs": "knowledge.use",
    "harness_run_status": "knowledge.use",
    "harness_run_events": "knowledge.use",
    "harness_get_run_detail": "knowledge.use",
    "harness_step_run": "knowledge.use",
    "harness_resume_run": "knowledge.use",
    "harness_cancel_run": "knowledge.use",
    "harness_get_artifact": "knowledge.use",
    "harness_resolve_effect": "sources.manage",
    "harness_reap_sandboxes": "sources.manage",
    "harness_decide_approval": "knowledge.use",
    "harness_list_connections": "sources.manage",
    "harness_usable_connections": "knowledge.use",
    "harness_browse_connection": "knowledge.use",
    "harness_attach_files": "knowledge.use",
    "harness_set_connection_policy": "security.manage",
    "harness_disable_connection_policy": "security.manage",
    "harness_connection_policy_history": "sources.manage",
    "harness_connection_calls": "knowledge.use",
}

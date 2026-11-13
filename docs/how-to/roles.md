# Roles for the UI, HTTP API and MCP

Pheasant 1.4.0 uses one server-side role policy for all three surfaces. Microsoft
sign-in, personal keys and MCP OAuth carry the same permissions. The existing
shared API token remains an Admin credential. A local instance with authentication
off retains its existing operator access.

| Operation | Reader | Contributor | Admin |
| --- | --- | --- | --- |
| Search, chat, browse, graph, visuals, generate drafts, download | Yes | Yes | Yes |
| Inspect existing evaluation/tuning status and metrics | Yes | Yes | Yes |
| Register/edit/remove sources, upload, sync, archive documents | No | Yes | Yes |
| Save/publish/remove artifacts or write knowledge records | No | Yes | Yes |
| Run evaluation or retrieval tuning; apply/rollback bundles | No | Yes | Yes |
| Change retrieval and application learning settings | No | Yes | Yes |
| Change security, shared connections, credentials or infrastructure | No | No | Yes |
| Create and revoke personal keys within own permissions | Yes | Yes | Yes |

Content ACLs still apply. A role grants operations, not an override of private
content. Request parameters such as `principal` and `principal_groups` cannot
impersonate someone else when a verified credential is present.

## Microsoft identities and Terraform

The local Azure infrastructure export accepts `entra_user_login_ids`, a map of
directory login UPN to `Admin`, `Contributor` or `Reader`, and the existing
`entra_user_object_ids` map. Terraform resolves login UPNs into immutable object
IDs, assigns the corresponding Entra app roles, and renders that same map into
`security.entra.role_assignments`. The human owner remains Admin. Conflicting
login/object-ID assignments are refused.

The tenant and object ID identify a person. A change to their email does not
change ownership of their keys. The current Terraform assignment caps every
session, personal key and OAuth credential; removing a user denies their old
credentials. Deploy the regenerated configuration to **all API replicas** when
changing assignments. The new policy takes effect on each replica when it loads
that configuration. No directory lookup is added to a search request.

Outside that deployment, `role_assignments: null` uses the verified token's role
claims. This compatibility mode does not independently poll Entra for role
changes: existing region-issued credentials retain their issued role until
revoked/expired. Use the authoritative Terraform mapping for managed deployments.
An empty mapping denies every Entra identity. Only configured tenant identities
with a recognized app role are admitted.

Terraform gives Admins Owner access to the deployment resource group and ownership
of the product application registration. Contributor and Reader receive no Azure
infrastructure role from their application role. Independent Azure/directory grants
must be reviewed separately: Pheasant cannot remove privileges assigned elsewhere.
An application API key is not an Azure Resource Manager credential; infrastructure
changes run under an Azure-authorized human or deployment identity.

## API tokens and personal keys

Sign in with Microsoft, open **Connect API â†’ Personal keys**, and choose the
current permissions or a lower role. Send the returned key as
`Authorization: Bearer <key>` to either HTTP or MCP. A Reader cannot mint a
Contributor key. Keys are shown once and only their secret digest is stored.
Revocation reaches other replicas' caches within 60 seconds.

For operator-managed service credentials, `security.api_auth.role_tokens` maps
environment variable **names** to roles. For example, the setup wizard can set
`PHEASANT_READER_API_TOKEN` to `Reader` and `PHEASANT_CONTRIBUTOR_API_TOKEN` to
`Contributor` in that mapping. Supply distinct long random values through the
deployment secret mechanism. These static service credentials rotate by restarting
the API; use expiring personal keys for individually attributable clients.
Never put credential values in YAML, Terraform inputs, a ZIP or a log.

## Readers and generated output

Readers can compose answers and download generated files. Shared artifact saving
is disabled for Readers even when region-wide autosave is on. Explicit `save=true`
requests and artifact/legacy-memory writes are refused. Background MCP composition
retains the initiating identity so a thread cannot accidentally gain operator
access. Temporary jobs, answer resumption and operational logs continue to work.
Readers can inspect existing tuning status and metrics; starting or cancelling
runs, pinning parameters and applying or rolling back bundles require Contributor
or Admin. The tuning management page is hidden for Readers.

## Verification and upgrade

New HTTP operations and MCP tools/resources require an explicit entry in
`security/permissions.py`; coverage tests detect omissions and authenticated calls
to an unclassified operation fail closed. HTTP denials return `403` with
`code: PERMISSION_DENIED`; MCP returns a deliberate tool/resource refusal.

Run `pytest -q tests/test_role_permissions.py tests/test_entra_identity.py` and the
full suite before release. `scripts/verify_role_permissions.py` exercises live HTTP,
MCP and text-search latency for credentials supplied through environment variables.
Its optional `--writes` creates uniquely named test sources/artifacts and removes
them afterward. Reports never contain credentials. Compare baseline and candidate
on the same corpus without concurrent image builds or indexing work.

No source IDs, artifact IDs or persisted credential formats change. Back up the
existing deployment before upgrading and retain its original Terraform state and
role UUIDs. The added Contributor app role is additive; existing Reader/Admin
assignments remain valid, with Reader's operation restrictions now enforced.

# Role permissions release validation

Prepared 2026-11-13 for server **1.4.0**. Local and Azure candidate validation
completed, and the local infrastructure ZIP was refreshed. Main published 1.3.0
for the preceding artifact work while this feature awaited Azure approval, so
this feature uses the next minor version, 1.4.0.

## Resulting behavior

The UI, HTTP API and MCP use one server-side permission policy. Microsoft tenant
and object IDs identify people; Terraform accepts login UPNs or object IDs and
renders authoritative assignments. Personal keys inherit or narrow their owner's
permissions. Existing shared API tokens remain Admin. See
[the role matrix and configuration guide](how-to/roles.md).

Readers retain search, chat, browsing, graph exploration, draft generation and
downloads. They cannot register or alter sources, persist artifacts, run tuning
or apply bundles. Existing status and metrics remain readable.
Contributors manage sources, artifacts, evaluation, tuning and
application settings. Admins additionally manage credentials, security and
infrastructure. Background work retains its initiating identity. Content ACLs
continue to apply and asserted request principals cannot impersonate other users.

No persistence migration, source identity change or model/provider change is
required. Existing Reader/Admin Entra role UUIDs are preserved; Contributor is
added. New endpoints and MCP operations must be explicitly classified and fail
closed for authenticated callers if omitted.

## Evidence

| Check | Observed result |
| --- | --- |
| Complete Linux suite, production dependencies and separate PostgreSQL 17/BM25 | 4,056 passed, 57 skipped, two release-test fixture failures |
| Final Linux regression run on the 1.4.0 image | 70 passed, including both previously failing fixtures, roles, Entra, release checks, MCP Apps and thread saturation |
| Final focused security/policy suite | 120 passed |
| Local live HTTP and MCP, all three static roles | Passed; Admin/Contributor source and artifact creation/removal passed; Reader mutation and tuning refusals passed |
| Real Microsoft-signed access token on local final image | Verified against tenant signing keys; narrowed Contributor/Reader personal keys passed HTTP/MCP checks and were revoked afterward |
| Browser checks on final image | All three roles passed at 1,440 and 390 px; management visibility, tuning refusal, overflow and page errors checked |
| Frontend | Production build and unit tests passed; existing bundle-size advisory remains |
| Source quality | Ruff checks, formatting, version synchronization and strict MkDocs build passed |
| Infrastructure package | Terraform validate and formatting passed; three render tests passed; all six region/worker configurations loaded without unknown schema fields; nine helper tests passed |
| Azure 1.4.0 candidate, HTTP/MCP | Admin, Contributor and Reader passed; real Microsoft access token and narrowed personal keys; source/artifact creation and cleanup; Reader write/tuning refusals |
| Azure 1.4.0 candidate, UI | All three roles passed at 1,440 and 390 px; no page errors or horizontal overflow |
| Azure Reader answering | The existing configured model returned a draft with save=false and no saved artifact |
| Azure preservation | Existing eight source definitions, corpus totals, storage, service identities and settings retained; only intended role fields and images changed |

The two Linux failures were version-test fixtures that assumed the project
version was at most 1.2.3. Their scenarios now explicitly set their test project
version; separate tests continue to cover a project version ahead of Git tags.
All failures from that Linux run have passing targeted rechecks. Skipped tests
remain skipped; no claim is made that unavailable optional integrations ran.

The native Windows full run returned 3,903 passed, 160 skipped and 25 failures.
Four async tests passed after enabling the installed pytest-asyncio plugin. The
legacy connection exception contract, hand-off count and two release fixtures
were corrected and passed their rechecks. Seventeen Windows failures remain:
four host-path normalization/idempotency/remedy cases, five POSIX backup/restore
cases, three SIGUSR1 cases, two Unix mode-0600 assertions, two CRLF content cases
and sibling contract byte parity with different line endings. Representative
host-path, mode, signal and backup failures were reproduced against unchanged Git
HEAD. The Linux deployment target passed those platform cases. The native
Windows suite is therefore **not fully green**.

The tested local image is `pheasant:1.4.0-rbac`, image ID
`sha256:1074c80affc45b89d821fcae1ab062d3b3b6cf651093121559764241f87635a8`.
Its `org.opencontainers.image.pheasant.authorization` label is `roles-v1`.
All 418 runtime Python and HTML source files were compared with the working
tree; there were no differences. The image was built on the fully tested role
candidate to retain its exact runtime dependencies and UI bundle while updating
the package version. It was pushed to the existing private Azure registry under
a unique development tag, then deployed by immutable digest. The public 1.4.0
release remains subject to PR merge and the normal CI publication workflow.

## Performance

A randomized, interleaved comparison used unchanged Git HEAD 1.2.0 and the
initial 1.3.0 role candidate against the same corpus, dependencies and configuration, with
10 warmups and 140 measured text-search requests per arm:

| Metric | Before | After |
| --- | ---: | ---: |
| Median | 25.623 ms | 26.375 ms |
| p95 | 57.006 ms | 49.885 ms |

Median changed +2.93%; p95 improved. The final 1.4.0 image retains those runtime
dependencies and the same authorization implementation. On Azure, sequential
before/after runs used the same query, 10 warmups and 140 measured requests each:

| Azure metric | Before | After |
| --- | ---: | ---: |
| Median | 99.456 ms | 100.319 ms |
| p95 | 118.294 ms | 116.006 ms |

Azure median changed +0.87% and p95 improved 1.93%. These measurements show no
material regression for the measured text-search workload. They do not establish
identical latency for every workflow. Normal scheduled synchronization continued
during the rollout; source definitions and corpus totals remained unchanged.
A warm-auth cache test also verified that 1,000 requests introduced no additional
persistence reads.

## Azure deployment

The existing development resource group is `rg-pheasant-kb-dev-wus3-001`.
After explicit user approval, the IAM plan was refreshed and reviewed. It changed
only the existing public Entra application registration and created
`random_uuid.kb_role["Contributor"]`: one resource added, one changed, zero
destroyed. Reader/Admin role IDs and existing assignments were preserved.

The deployed immutable image is
`acrpheasantkbdevwus3001.azurecr.io/pheasant@sha256:1074c80affc45b89d821fcae1ab062d3b3b6cf651093121559764241f87635a8`.
All five application tiers reached their new ready revision:

| Tier | Ready revision |
| --- | --- |
| API | pheasant-kb--0000069 |
| Graph | pheasant-graph--0000063 |
| Indexer | pheasant-indexer--0000065 |
| Logger | pheasant-logger--0000063 |
| Worker | pheasant-worker--0000055 |

Configuration was generated through the setup wizard and compared through the
live schema before deployment. Only role fields changed semantically. Each
post-deployment template was compared with its snapshot: resources, scale rules,
sidecars, storage mounts and service identities were preserved. PostgreSQL and
NATS were not redeployed. The eight source definitions and corpus totals matched
before/after; scheduled-sync checkpoints and timestamps advanced normally.

Admin used a real Microsoft-signed token; Contributor and Reader used personal
keys narrowed from that authenticated identity. Direct sign-in by separate
Contributor/Reader directory accounts was not exercised. HTTP/MCP mutation tests
created uniquely named sources and artifacts, then removed them. All test keys
were revoked, including those used by the six Azure browser checks.

The original revision/configuration snapshots and image remain available for
rollback. The ignored local developer Terraform image input was updated to the
tested digest. Infrastructure export templates remain local as originally
requested; the ZIP does not include the developer deployment state or inputs.

## Reproduction and artifacts

Use the repository's documented full-suite commands, with pytest-asyncio enabled
and a separate test PostgreSQL database for PostgreSQL coverage. The live script
`scripts/verify_role_permissions.py` accepts credentials through environment
variables and supports optional uniquely named, cleaned-up source/artifact writes.
The infrastructure ZIP carries the same check as `scripts/role_smoke.py`.

Local detailed logs and sanitized reports are under `.tmp/rbac-*`, including
`rbac-linux-final.xml`, `rbac-final-regressions.xml`,
`rbac-policy-final-tests.log`, `rbac-live-microsoft-local.json`,
`rbac-ui-verification.json`, `rbac-1-4-final-regressions.xml` and
`rbac-interleaved-performance.json`. Azure role, browser, preservation, image and
latency reports are under `.tmp/azure-rbac-1.4/`.
They are excluded from the distributable, along with credentials, real dotenv
files, Terraform state/plans and provider caches. The ZIP includes a sanitized
copy of this report, an explicit inventory, internal SHA256SUMS and an external
checksum. Its release preflight requires the `roles-v1` image capability;
the version tag alone is insufficient.

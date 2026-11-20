# Host a region for someone else

One party runs the region (its cloud resources, its models, its scale). Another
uses it, and decides who else may. This page is how those two stay separate:
access the users grant themselves, a customer's own directory signing in, and a
catalog of models the operator provisions and the users pick from.

Everything here is off by default. A region that sets none of it is exactly
what it was.

The people on the other side of that line have their own page:
[Runbook: administer and use a hosted knowledge base](customer-runbook.md).

| Decision | Who makes it | Where |
| --- | --- | --- |
| Which models exist, their capacity and region; scale; networking | The operator | Deployment (`models.*`, Terraform) |
| Who the operator's own admins are | The operator | `security.entra.role_assignments` |
| Who else may use the region, and as what | Its Admins and Contributors | UI, `POST /access/grants`, MCP `grant_access` |
| Which customer directories may sign in | Its Admins | `POST /access/organizations`, MCP `connect_organization` |
| Which answering and judgement model answers a question | Whoever asks it | `model` / `decision_model` on the question |
| Which embedding model the region indexes with | Its Admins | `PUT /models/embedding`, MCP `select_embedding_model` |

## Access the users grant

```yaml
security:
  entra:
    enabled: true
    delegated_access: true
    role_assignments:            # the operator's admins; cannot be changed at runtime
      "<entra object id>": Admin
```

With `delegated_access` a role comes from a grant made while the region runs,
or from `role_assignments`, whichever is higher. A token's role claims are no
longer consulted, so the app registration needs no "assignment required" and
no app-role assignment per person.

| Caller | May grant | May change or remove |
| --- | --- | --- |
| Admin | Admin, Contributor, Reader, in any connected organization | Any grant |
| Contributor | Contributor, Reader, in their own organization | Grants of Contributor or Reader in their own organization |
| Reader | Nothing (`PERMISSION_DENIED`) | Nothing; cannot list who has access |

Nobody changes their own access. A role above the caller's ceiling is refused
with `ACCESS_ROLE_NOT_GRANTABLE`, including demoting someone who holds one.

A grant names a person by sign-in name. Until they first sign in it is
`pending`; the first sign-in binds it to their immutable Entra object id, so a
renamed account keeps its access and a recycled address does not inherit it.

```bash
curl -X POST "$URL/access/grants" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"email": "kim@contoso.com", "role": "Contributor"}'
```

The same over MCP: `grant_access(email, role)`, `revoke_access(email)`,
`list_access()`. Revoking, or lowering a role, reaches that person's sessions,
personal keys and connected MCP clients within seconds: every credential is
capped by the current grant on each request. Raising a role reaches their
browser session at once; a personal key or an MCP client keeps what it was
issued with until it is re-created.

## A customer's own directory

An Admin connects a customer's Microsoft Entra tenant once. From then on that
customer's people sign in with their organization's accounts, and its own
Admins and Contributors manage them. Nothing about the customer's directory is
configured in the operator's.

```bash
curl -X POST "$URL/access/organizations" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"tenant_id": "<their tenant id>", "name": "Contoso",
       "domains": ["contoso.com"], "default_role": "Reader"}'
```

The reply carries two links:

- `admin_consent_url`: the customer's directory admin opens it once to approve
  the app for their organization.
- `sign_in_url`: what their people use (`/auth/login?org=contoso.com`).

`default_role` (Reader or Contributor, never Admin) gives every member that
role. Without it, connecting admits nobody: members are granted one at a time.
With organizations connected, a sign-in that names none asks which one; MCP
clients get the same page during their authorization.

What holds the boundary:

- A token is verified against the keys and issuer of the tenant it claims, and
  only for the operator's tenant or a connected one. A token from any other
  directory is refused whatever it says.
- A sign-in sent to one tenant accepts an identity token from that tenant only.
- A grant is made in one tenant. The same sign-in name in another directory is
  a different person.
- Disconnecting an organization removes every grant made to its people.

The app registration must be multi-tenant (`AzureADMultipleOrgs`) for another
directory to sign in at all; the operator decides that at deployment.

## A catalog of models

```yaml
models:
  chat:
    - id: gpt-6-luna
      label: GPT-6 Luna
      family: OpenAI
      provider: azure                       # the OpenAI wire on a Foundry resource
      model: gpt-6-luna                     # the deployment
      base_url: https://<resource>.services.ai.azure.com/openai/v1
      api_key_env: FOUNDRY_CHAT_API_KEY
    - id: claude-opus-5-5
      label: Claude Opus 5.5
      family: Anthropic
      provider: anthropic                   # Anthropic's Messages wire
      model: claude-opus-5-5
      base_url: https://<resource>.services.ai.azure.com/anthropic
      api_key_env: FOUNDRY_CHAT_API_KEY
  default_chat: gpt-6-luna
  decisions:
    - {id: ms-decision-1, api: systemone, model: ms-decision-1, base_url: https://<resource>.services.ai.azure.com,
       sufficient_threshold: 0.16, relevant_threshold: 0.32, accept_threshold: 0.53}
    - {id: gpt-6-luna, api: decisions, model: gpt-6-luna, base_url: https://<resource>.services.ai.azure.com}
  default_decision: ms-decision-1
  embeddings:
    - {id: text-embedding-3-large, model: text-embedding-3-large, dimensions: 3072, base_url: https://<resource>.services.ai.azure.com/openai/v1}
    - {id: text-embedding-3-small, model: text-embedding-3-small, dimensions: 1536, base_url: https://<resource>.services.ai.azure.com/openai/v1}
  default_embedding: text-embedding-3-large
```

An entry is a wire and an address, not a vendor's client library: `provider`
is one of the HTTP shapes pheasant already speaks (`openai`, `azure`,
`anthropic`, `gemini`). Moving a model to another host is a `base_url` change.
An entry with no key on a Foundry host is called with the process's managed
identity.

**Picking.** `GET /models` (MCP `list_models`) lists what is offered, without
endpoints or variable names. A question names what it wants:

```bash
curl -X POST "$URL/assistant/chat" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"question": "How is failover configured?", "model": "claude-opus-5-5"}'
```

`ask_knowledge_base(..., model=, decision_model=)` is the same over MCP. Unset,
the defaults apply; the answer's `models` says which ran. An unknown id is
`UNKNOWN_MODEL`. `models.selectable: false` makes everyone use the defaults.

Settings tuned for the configured model (`assistant.reasoning_effort`, a
`grader_model`) are not carried to a different one. Each judgement model has
its own thresholds, because the two APIs' probabilities are not on one scale.
On the OpenAI wire a deployment should be named after its model: Luna-only
options are recognized by the name `gpt-6-luna`.

**Switching the embedding model.** One embedding model is in force for the
whole region, because a query is only comparable with passages embedded by the
same model. An application Admin changes it:

```bash
curl -X PUT "$URL/models/embedding" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"embedding": "text-embedding-3-small"}'
```

The choice is stored for every process. The indexer re-embeds every chunk it
already holds, without re-reading a source, before its next sync writes a
vector; `GET /models` reports `embedding.state` as `reindex_pending` until it
is `ready`. Until then semantic search is partial and answers lean on the text
and graph arms. Budget one embedding call per chunk.

## What it was used for

```yaml
observability:
  usage: true
models:
  currency: USD
  chat:
    - id: claude-opus-5-5
      # ...
      input_price: 4          # per million tokens: stated by the operator,
      output_price: 20        # never looked up
      cached_input_price: 0.2
```

`default_chat` and `default_decision` are the deployment's defaults. An Admin
can choose another catalog entry as everyone's default without a redeploy: the
Admin page's Models tab (**Make default**, **Restore deployment default**),
`PUT /models/defaults` or MCP `set_default_models`. The choice is a row every
replica reads on its next question; the catalog itself (which models exist and
their keys) stays the deployment's. People still choose another model for
their own conversation in the composer's model dialog.

With `observability.usage` the region keeps daily totals: tokens by model
(input, cached input, output), questions, searches, who asked and on which
surface, and how long answers and searches took. Totals only, never a question
or an answer. `GET /usage?days=30` (MCP `get_usage`, the Admin page's Usage
tab) reads them back for an Admin:

- **Activity**: questions, searches and active people, by day, by person and
  by organization.
- **Tokens and cost**: by model. Cost is tokens times the prices stated in the
  catalog. A model with no stated price is counted and listed under
  `unpriced_models`, so a total is never quietly short.
- **Response time**: answer and search p50 and p95, read off a fixed histogram
  (each figure is the upper bound of the range the percentile falls in).
- **Size**: documents, chunks and indexed bytes against
  `storage.knowledge_base_limit_gb`, and whether the knowledge base is full.

**Export CSV** and **Export JSON** on the Usage tab save these measures, and
this month's spend against its limit, as a file: a ZIP with one CSV per table
(`totals`, `by_day`, `by_model`, `by_organization`, `by_channel`, `people`,
`latency`, `spend`) plus `report.json`, or one JSON document. The same file is
`GET /usage/export?days=&format=csv|json` and MCP `export_usage`, which returns
it as `content` (base64 for the ZIP) with its `filename`. The amounts follow the
page's rule: cost and spend are null in an export by an Admin a customer
granted.

### The traces behind the totals

With `observability.interactions` on, every model, organization and person in
the Usage tab opens the sessions behind its numbers, and each session its
calls: the question and answer, the models that ran, the route, and the steps
the answer took with their tokens and timings, laid out as the chat's own step
trace. Nothing is computed ahead: each list is one query, run when it is
opened, and the period selector (7, 30, 90, 365 days) is how far back it reads.
The interaction ledger keeps `observability.interactions.hot_retention_days`
(7 by default), so a longer period shows what remains and says so.

A session is a conversation (the web UI's conversation, MCP's `session`); a
call made outside one stands alone. Each level offers:

- **A Log Analytics query.** Spans carry the call's W3C trace id as
  `OperationId` in the workspace's App Insights tables, so the query names the
  selection's trace ids under the period (`| where TimeGenerated > ago(30d)`),
  or a session's own time span. Copy it into the workspace's Logs. Spans carry
  no person, by design; the ledger is what joins a person to their traces.
- **Export JSONL.** The selection's ledger rows, one call per line, attributes
  included. MCP's `export_usage_traces` returns the same rows in pages of
  2,000 (`offset`, `has_more`), for an admin agent that cannot take a download.

A diagram asked for on its own (the chat's **Draw a diagram**, a redraw in
another shape, MCP `create_visual`) is a call like any question: its request
is the row's question, its conversation is the session, and its trace shows
the passages it drew from, the model and tokens that drew it, and what came
out (a diagram and its shape, the graph's edges when the model's drawing
could not be used, or why nothing was drawn).

The drill-down reads **Questions** by default: the questions and searches the
totals count. On a live region nearly every ledger row is the interface
polling for jobs and status, so **Every call** (`calls=all`) is there for
chasing a fault rather than for reading.

**Search** narrows either level with a simple wildcard pattern over each
call's question, answer, operation and session: `*` is any run of
characters, `?` is one, case is ignored, and the pattern may match anywhere,
so `deploy*worker` finds "how do I deploy a worker". The sessions list keeps
the sessions with a matching call and counts those calls; a session shows
only its matching calls, with **Show all calls** beside them; the export takes
the same search. The parameter is `match` on both surfaces.

The same reads are `GET /usage/sessions` and `GET /usage/sessions/{session}`
(MCP `list_usage_sessions`, `get_usage_session`), Admin only. The person
filter is `person` on both: on MCP a parameter named `principal` is always the
caller.

Cost is the operator's. It is shown to the local operator, the shared token and
the admins the deployment pinned (`role_assignments`); an Admin a customer
granted sees the same activity and tokens with `cost: null`.

Counting adds no database write to a request: totals are held in memory and
written in one additive statement at most every 30 seconds, on shutdown, and
before a read. A process that is killed loses at most that interval.

## A kill switch and a monthly spend limit

Two controls the operator holds over each knowledge base, in the Admin page's
**Controls** tab, `GET`/`PUT /region/controls` and MCP `get_region_controls` /
`set_region_controls`. An Admin reads them; only an **operator admin** changes
them: the shared token, the local operator, or an Admin the deployment pinned
(`security.entra.role_assignments`). An Admin granted inside the region is
refused with `403 OPERATOR_REQUIRED`, and sees whether access is suspended and
the share of the limit used, never the amounts.

**The kill switch** cuts access at once. Every request, from the UI, the HTTP
API or MCP, is refused with `403 ACCESS_SUSPENDED` and the reason the operator
gave, except an operator admin's, so whoever turned it on can turn it off.
`/auth/me` and `/auth/logout` still answer, and the web UI shows one "Access is
paused" page instead of a failing panel each. Every replica reads it within 5
seconds. Indexing, saved content and grants are untouched; to remove one person,
revoke their grant instead.

```bash
curl -X PUT $URL/region/controls -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"suspended": true, "reason": "Billing review"}'
curl -X PUT $URL/region/controls ... -d '{"suspended": false}'
```

**The spend limit** is what one knowledge base may spend on models in a
calendar month (UTC): its answering, judgement and embedding calls at the prices
the catalog states. It defaults to `models.monthly_spend_limit` (500), and an
operator admin raises or lowers it at runtime (`{"monthly_spend_limit": 750}`;
`null` returns to the default). At the limit:

| What | Happens |
| --- | --- |
| Answers, `assess_evidence`, dictation | Refused before they start: `402 SPEND_LIMIT_REACHED` |
| Any other chat, judgement or embedding call | Refused before it is sent |
| A sync that would embed new content (`incremental`, `full`) | Skipped, with `reason: spend limit reached`; nothing retries it |
| Search | Answers from text and graph; the vector arm drops out |
| Browsing, reading, exports | Unchanged |

Spend is counted from the usage totals (`observability.usage`, which must be
on) plus each process's calls not yet written, so a limit can be passed by what
is in flight across the replicas at that moment and never by more. A model with
no stated price counts nothing and is named in the reply. On the 1st the month
starts over. Each knowledge base of a fleet has its own switch and its own limit.

**Deep research has two limits of its own**, in the same row and behind the
same rule (operator admin only), on the same Controls tab, on desktop and
phone alike. `deep_answer_spend_limit` (default `models.deep_answer_spend_limit`,
5) is what one `@detailed` / `@deep` answer may spend: research wraps up at
95% of it, counting what writing will cost. `max_concurrent_deep_answers`
(default 2) is how many run at once per api process; past it a deep question
is `429 DEEP_RESEARCH_BUSY` and an ordinary one is still answered. `null`
returns either to the deployment's default. A granted Admin sees the
concurrency figure and not the amount.
[Deep research](conversations-and-visuals.md#deep-research) has the detail.

**Prices are set at runtime and apply from then on.** Each call is costed when
it is made, at the price in force then, and the cost is stored with its tokens.
When a provider changes its price, an operator admin changes it in the Models
tab's **Prices** section (`PUT /models/prices` with `{"prices": {"gpt-6-luna":
{"input": 2.5, "output": 10}}}`, or MCP `set_model_prices`). Fields you leave
out keep the catalog's, `null` returns a model to the catalog's price, and every
replica charges the new price within 5 seconds. Nothing already spent is
repriced: the month so far, the Usage tab and every export keep what each call
cost when it was made. The section keeps a history of who changed which price,
when, from what to what. Totals recorded before a release stored costs are
priced once, on its first start, at the prices in force then.

**The totals it reads cannot be taken back.** `usage_daily` is append-only in
the database itself, on SQLite and Postgres alike, and so is the fleet
directory's mirror of it. Triggers refuse every `DELETE`, every `TRUNCATE` and
any `UPDATE` that lowers a count or moves a row to another day, person or
knowledge base, whoever sends it: the region, a script or a statement typed
into the database. The region only ever adds to a total. Each start checks the
triggers and puts back any that are missing. A write the database refuses
(a dropped connection, say) keeps its rows for the next flush instead of
losing them. Only the database's owner can get past this, by dropping the
triggers or restoring an older backup, and that owner can already raise the
limit.

## A size limit

```yaml
storage:
  knowledge_base_limit_gb: 50
```

The limit is on **indexed content**: the sizes of the documents the knowledge
base holds, summed. It is the figure a person adding content can reason about,
and it falls the moment a document is removed. Unset, there is no limit.

At the limit the knowledge base keeps everything it has and takes no more:

| What | Happens |
| --- | --- |
| A sync that could add content (`incremental`, `full`), however it was started | Skipped, with `reason: knowledge base full`. Not a failure, so nothing retries it |
| `validate_only` and `repair` | Run: they add nothing |
| Submitted documents, the drop zone, quick add | Refused: `507 KNOWLEDGE_BASE_FULL`, with how to make room |
| Search, answers, removing a source, archiving documents | Unchanged |

A sync that is already running finishes what it began, so the limit can be
passed by one sync's worth and no more. Room comes back two ways: an Admin
removes a source or archives documents, or asks the operator for more
(`request_capacity` with `kind: storage`), who raises the setting. While a
knowledge base is full, a file deleted from a synced folder is not noticed
until there is room again, because noticing it is a sync; archive the document
instead.

It is not `storage.max_state_size_gb`, which bounds the graph's snapshot
history by evicting the oldest snapshot.

## Several knowledge bases

One region is one knowledge base, and stays so. A customer group that needs
several gets several small stacks on the operator's shared resources, tied
together by a **fleet directory** (a few tables in one database every stack can
reach).

```yaml
fleet:
  enabled: true
  slot: kb-02                     # this stack's name in the fleet
  url: https://kb-02.example.com
  dsn_env: PHEASANT_FLEET_DATABASE_URL
  claimed: false                  # true for a stack already in use
```

| Who | Does what | What it costs |
| --- | --- | --- |
| The operator | Provisions *slots*: stacks with nothing in them | A deployment. Idle slots run nothing |
| An Admin of any knowledge base | Claims a free slot for a new one (`POST /fleet/knowledge-bases`, MCP `create_knowledge_base`, the Admin page) | Nothing new: one row changes |
| An Admin | Files a capacity request when no slot is free, or one is filling up (`POST /fleet/requests`, MCP `request_capacity`) | Nothing: it is a record of an ask |
| The operator | Answers the request (`PATCH /fleet/requests/{id}`): approved, declined, fulfilled | Whatever they then choose to provision |

Whoever claims a slot becomes the new knowledge base's Admin, with anyone they
name from their own organization. That is all two knowledge bases share: each
has its own sources, documents, Contributors and Readers, granted inside it.
`409 FLEET_NO_CAPACITY` is the answer when every slot is in use. Nothing in the
region can add one.

`GET /fleet` (MCP `list_fleet`) shows an Admin the knowledge bases they
administer and how many slots are free. The operator also sees every slot and
each knowledge base's last thirty days: questions, people, tokens, cost and
answer p95, because every stack mirrors its usage totals into the directory.

Stacks that share a database server share its login: they are kept apart by
pheasant, not by database permissions. Releasing a slot is not automated,
because its data has to be removed first.

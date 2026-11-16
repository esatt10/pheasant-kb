# Runbook: administer and use a hosted knowledge base

For the people who use a knowledge base that somebody else hosts: its Admins
first, then Contributors, Readers, and anyone connecting an agent to it.

The party that hosts it (the *operator*) runs the servers, chooses which
models are available and sets how much it may hold. Everything else is yours:
who may use it, what it knows, and how it is asked. You never need the
operator to add a colleague.

The operator's side is [Host a region for someone else](hosted-regions.md).

## Who can do what

| | Reader | Contributor | Admin |
| --- | --- | --- | --- |
| Search, ask questions, browse documents, view diagrams, download | Yes | Yes | Yes |
| Add or remove sources, upload documents, sync, archive documents | No | Yes | Yes |
| Save answers as artifacts | No | Yes | Yes |
| Give access to others in your organization | No | Contributor or Reader | Any role |
| Remove or lower someone's access | No | Contributors and Readers in your organization | Anyone |
| Connect or disconnect an organization | No | No | Yes |
| Switch the embedding model, see usage, start a new knowledge base | No | No | Yes |
| Create personal keys for your own agents and scripts | Yes | Yes | Yes |

Nobody can change their own access, and nobody can grant a role above their
own. The full table is in [Roles and permissions](roles.md).

## 1. First sign-in (the first Admin)

1. The operator names you as the first Admin. You receive an invitation email
   from Microsoft. Accept it.
2. Open the address the operator gave you and sign in with that account.
3. You land in the Notebook: a chat on the left, sources and documents on the
   right. **Admin** in the top bar is where the rest of this page happens.

The first request after a quiet period can take about a minute and a half:
the knowledge base sleeps when unused. The page says it is waking.

## 2. Let your organization sign in with its own accounts

Do this once. Until you do, each person has to be invited by the operator.

1. **Admin, Access, Organizations.** Enter your Microsoft Entra tenant id, a
   name, and your email domains.
2. Under **Everyone in it gets**, choose Reader or Contributor if every member
   should get in automatically. Leave it empty to admit only the people you
   grant one by one. Then **Connect organization**.
3. You get two links:
   - **Consent link.** Send it to your Microsoft Entra administrator. They
     open it once to approve the app for your organization.
   - **Sign-in link.** This is what your colleagues use
     (`/auth/login?org=<your domain>`).

From then on your people sign in with their work accounts. A directory that
nobody connected cannot sign in at all.

The same over the API or from an agent:

```bash
curl -X POST "$URL/access/organizations" -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' \
  -d '{"tenant_id": "<tenant id>", "name": "Contoso", "domains": ["contoso.com"], "default_role": "Reader"}'
```

MCP: `connect_organization`, `disconnect_organization`.

## 3. Give people access

**Admin, Access, People.** Enter the person's sign-in name, pick a role, and
choose **Grant access**. The page only offers the roles you are allowed to grant.

- A grant shows as **pending** until the person first signs in. That sign-in
  ties it to their account for good, so a renamed account keeps its access.
- **Lowering or removing** access takes effect within seconds, on every
  browser session, personal key and connected agent that person has.
- **Raising** a role reaches their browser at once. A personal key or a
  connected agent keeps the role it was created with until they re-create it.

```bash
curl -X POST "$URL/access/grants" -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"email": "kim@contoso.com", "role": "Contributor"}'
curl "$URL/access" -H "Authorization: Bearer $KEY"                     # who has access
curl -X DELETE "$URL/access/grants/<grant id>" -H "Authorization: Bearer $KEY"
```

MCP: `grant_access(email, role)`, `revoke_access(email)`, `list_access()`.

| Refusal | Meaning |
| --- | --- |
| `PERMISSION_DENIED` | Your role cannot do this (a Reader granting, for example) |
| `ACCESS_ROLE_NOT_GRANTABLE` | The role is above yours, or the person already holds one above yours |
| `ORGANIZATION_NOT_CONNECTED` | The person's directory has not been connected (step 2) |

## 4. Put knowledge in

Contributors and Admins.

- **Add a source:** the **Add** box on the Sources page takes one line: a
  repository URL, a web page, a folder the operator mounted, or `gdrive:`.
- **Upload files:** drop them on the Sources page.
- **Watch it index:** the Syncs page shows each source's runs, its health as a
  percentage, and any file that could not be read, with the reason.
- **Remove a document on purpose:** open it in the document browser and choose
  Remove. It stays out of every later sync until you reinstate it.

Indexing is not instant. A source is searchable when its sync finishes, not
when you add it. An agent should wait for the sync (`wait_for_sync`) before
relying on what it added. See [Sources](sources.md) and
[Sync schedules](sync-schedules.md).

## 5. Ask it

- Type a question. Answers cite the passages they used; a citation opens the
  document at that passage.
- **Pick a model** from the menu beside the question box. Leave it alone and
  the default answers. Each question can use a different one.
- Start a message with a keyword to shape the answer: `@table`, `@steps`,
  `@compare`, `@diagram`, `@search` (ranked passages, no model), `@compose`
  (build a new file from documented parts). **@ keywords** in the chat lists
  them all.
- Narrow a question to a folder or file from the document browser, or leave
  documents out with `@exclude <name>`.

Details: [Chat and the UI](chat-and-ui.md) and
[Conversations and visuals](conversations-and-visuals.md).

## 6. Connect an agent or a script

The knowledge base is also an MCP server at `<address>/mcp`.

- **Agents that can open a browser** (Claude Code, Claude's custom connectors,
  VS Code): add the MCP address. The agent sends you to sign in, and the
  knowledge base asks whether that agent may act for you. It then has your
  role, no more.
- **Scripts and agents that cannot:** open the key icon in the top bar,
  **Personal keys**, create one (your role or a lower one), and send it as
  `Authorization: Bearer <key>` to the API or the MCP address. A key is shown
  once. Revoke it in the same place.

An agent picks models the same way a person does:
`ask_knowledge_base(question, model="claude-opus-5-5")`. `list_models` says
what is offered. [MCP tools](../mcp_tools.md) lists everything an agent can
call.

## 7. Models

**Admin, Models** shows what the operator made available: answering models,
judgement models (which decide whether the evidence is enough) and embedding
models, each with its default. Which ones exist is the operator's decision. If
you need one that is not listed, ask them.

**Switching the embedding model** (Admin only) changes how the whole knowledge
base is searched by meaning. It re-processes every stored passage, without
re-reading your sources. While that runs, the page says re-embedding is in
progress and answers lean on keyword and link search. Do it when the knowledge base is
quiet, and expect it to take from minutes to hours depending on size.

```bash
curl "$URL/models" -H "Authorization: Bearer $KEY"
curl -X PUT "$URL/models/embedding" -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"embedding": "text-embedding-3-small"}'
```

## 8. See how it is used

**Admin, Usage** (Admins): questions, searches and active people by day; tokens
by model; how long answers take (typical and slowest); and how full the
knowledge base is. These are totals only. No question or answer is kept there.
Cost is shown to the operator, not to you.

API: `GET /usage?days=30`. MCP: `get_usage`.

## 9. When it is full

The operator sets how much indexed content a knowledge base may hold. Usage
shows the share used. At the limit:

- Nothing you have is removed, and search and answers keep working.
- New uploads and new sources are refused with `KNOWLEDGE_BASE_FULL`.
- Syncs that would add content are skipped, and say why.

To make room, remove a source or archive documents you no longer need. Room
is available at once. To get more, ask the operator: **Admin, Knowledge
bases**, under **Ask for** choose *More room in this one (GB)*, say how many
and why, and **Send request to the operator**.

While a knowledge base is full, a file deleted from a synced folder is not
noticed until there is room again. Archive the document instead.

## 10. More knowledge bases

Admins. A second knowledge base is separate: its own sources, documents,
Contributors and Readers. You are its Admin, with anyone you name.

- **Admin, Knowledge bases, New knowledge base.** Name it and choose **Create
  knowledge base**. If the operator has a free slot, it is yours at once, with
  its own address.
- If none is free you get `FLEET_NO_CAPACITY`. Under **Ask for**, choose
  *More knowledge bases* and send the request. The operator decides, and their
  answer appears under **Capacity requests** on the same page. Nothing is
  provisioned automatically.

```bash
curl "$URL/fleet" -H "Authorization: Bearer $KEY"
curl -X POST "$URL/fleet/knowledge-bases" -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"name": "Field service", "admins": ["lee@contoso.com"]}'
curl -X POST "$URL/fleet/requests" -H "Authorization: Bearer $KEY" \
  -H 'content-type: application/json' -d '{"kind": "slots", "quantity": 1, "reason": "A team of 40 starting in March"}'
```

MCP: `list_fleet`, `create_knowledge_base`, `request_capacity`.

## 11. When someone leaves, or you do

- **A person leaves:** remove their grant (step 3). Every key and agent they
  connected stops working within seconds. Disabling their account in your
  directory also stops new sign-ins.
- **Your organization leaves:** an Admin disconnects it under Organizations.
  Every grant made to its people is removed at once. Ask the operator for an
  export of anything you need, and to remove your data.

## 12. If something is not working

| What you see | What it means | What to do |
| --- | --- | --- |
| A blank or "waking" page for a minute or two | The knowledge base was asleep | Wait. It opens by itself |
| "Which organization?" at sign-in | More than one directory is connected | Pick yours, or use your sign-in link |
| Sign-in is refused for your colleagues | Your directory has not approved the app | Send the consent link to your Entra administrator |
| A colleague signs in and is refused | They have no grant | Grant them a role (step 3) |
| No **Admin** in the top bar | You are a Reader | Ask an Admin or Contributor |
| A new source shows nothing | Its sync has not finished, or its files could not be read | Syncs page: the run, its health and the unreadable files |
| An answer says it found nothing | The content is not indexed, or the question is scoped to a folder | Check the scope chip above the question box, then the Syncs page |
| `KNOWLEDGE_BASE_FULL` | The size limit | Section 9 |
| `FLEET_NO_CAPACITY` | No free slot | Section 10 |
| `UNKNOWN_MODEL` | That model is not offered here | Pick one from the menu, or `list_models` |
| Answers are slower or vaguer than usual | The embedding model is being switched, or a large sync is running | Admin, Models says when re-embedding is in progress; the Syncs page shows the run |
| A refusal that says to retry | Temporary | Retry |

For anything about speed, models, or room that this page does not cover, the
operator is the one to ask: those are theirs to change.

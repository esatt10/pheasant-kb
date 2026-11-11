# Connect accounts and keys

A region can read private sources and call model services with accounts you
connect while it runs, from the web UI, an agent over MCP, or a script over
HTTP. Nothing goes into `.env` or a config file. This sits beside the
environment variables pheasant-kb already reads, and an operator's variable
still wins.

| Connect | Then | Read with |
|---|---|---|
| **GitHub**, **GitLab**, **Azure DevOps**: sign in with GitHub, GitLab or Microsoft, or a personal access token | Paste a private repository URL into **+ Add source** | Clones and fetches, signed for that host only |
| **Google Drive**: sign in with Google, a service account key, or an access token | Add `gdrive:` | Each Drive sync; a sign-in is refreshed as it expires |
| **API token**: a bearer token | Add an `api` or web source with the connection | Every request that source makes |
| **OpenAI**, **Azure AI Foundry**, **Anthropic**, **Gemini**: an API key | Nothing more | Embeddings, answers, judgements, captions, dictation |
| **Environment variable**: any name a setting reads | Nothing more | That setting (`api_key_env` and friends) |

## From the web UI

**+ Add source** has a row of service buttons under the field. Paste the
repository URL, press the button for its service (it opens by itself when an
account for that host is already connected), and enter the token in the panel
it expands. **Connect GitHub** checks the token with GitHub and shows the
account it names. **Add and index** then clones the repository with it.

For Google Drive, press **Google Drive**. The field fills in `gdrive:`, and
the panel offers the three ways to connect. **Sign in with Google** opens
Google's consent page and comes back to the Sources page.

The **Connections** section of the Sources page lists every connected account:
the account the service named, the last four characters, whether it checked
out, and which sources read with it. Its own row of buttons connects model keys
and anything else. **Test** asks the service again. **Remove** refuses while a
source reads with the account and asks first.

## From an agent (MCP)

```text
list_connections()                                    # what can be connected, and each method's fields
connect_service(provider="github", fields={"token": "github_pat_…"})
add_source(knowledge_base="pheasant-kb", target="https://github.com/acme/private", connection="github")
wait_for_sync(knowledge_base="pheasant-kb", source_name="private", since="…")
```

A Drive sign-in needs a person: `start_service_sign_in()` returns an
`authorize_url` for them to open, and the connection appears once they allow
access. A secret passed to `connect_service` passes through the agent's
conversation, so for a key you would not paste into a chat, use the web UI.

## From a script (HTTP)

```bash
TOKEN=$(docker compose exec api sh -c 'cat /run/pheasant/secrets/api-token')
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"provider":"openai","fields":{"api_key":"sk-…"}}' http://127.0.0.1:8765/connections
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"target":"https://github.com/acme/private","connection":"github","wait":false}' \
  http://127.0.0.1:8765/sources/quick-add
```

The routes are listed in [the HTTP reference](../reference/http-api.md).

## How it is held

- **Sealed, with a key kept elsewhere.** Each connection's secret is encrypted
  with AES-256-GCM in the database's `connections` table. The key is
  `PHEASANT_CREDENTIAL_KEY`, which `secrets-init` generates into the secrets
  volume on first boot. The api and the indexer hold it, the other tiers do
  not, and a preparation worker refuses to start with it. `docker compose run
  --rm backup` copies it with the other tokens, so a restore opens the
  connections it restores.
- **Never returned.** Every surface shows the provider, the account and the
  last four characters, and nothing else of the secret.
- **Checked before it is stored.** A token the service rejects is refused
  (`CONNECTION_REJECTED`) and not saved. A service the region cannot reach is
  saved with a note saying it was not checked.
- **Admins only** on a region with Entra roles (`CONNECTION_FORBIDDEN`
  otherwise).
- **Which one wins.** A source that names a connection reads with it. Otherwise
  an environment variable the operator set (`GITHUB_TOKEN`, `OPENAI_API_KEY`)
  wins over a connection standing in for it, and the Connections list says
  when that happens. With neither, the provider's only connection is used.

## Start from nothing

`OPENAI_API_KEY` is optional in `.env`. Without it the stack starts, the
Sources page offers **OpenAI** under Connections, and once it is connected
embeddings and answers use it. The indexer picks a new connection up within 15
seconds (`security.connections.cache_seconds`).

## Sign in with GitHub, Microsoft, GitLab or Google

Each service with a sign-in shows it as the first option in its panel, beside
the token: **Sign in with GitHub**, **Sign in with Microsoft** (Azure DevOps),
**Sign in with GitLab**, **Sign in with Google** (Drive). Pressing it opens the
service's own sign-in page and comes back to the Sources page connected. The
password never reaches the region; what is sealed is the token the service
issues, and a token that expires is refreshed as syncs need it.

A sign-in needs an OAuth client, registered once per region with the
service. Its callback is the region's address plus
`/connections/oauth/callback`: `http://127.0.0.1:8765/connections/oauth/callback`
locally, `https://<your host>/connections/oauth/callback` deployed. The panel
shows the exact value when no client is set.

| Service | Register | Scopes | Set on the api and indexer |
|---|---|---|---|
| GitHub | [New OAuth app](https://github.com/settings/applications/new): "Authorization callback URL" is the callback | `repo read:user` | `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET` |
| Microsoft (Azure DevOps) | An Entra app registration: platform *Mobile and desktop applications* with the callback as redirect URI, accounts in any directory and personal Microsoft accounts, API permission Azure DevOps `user_impersonation` | Azure DevOps, `offline_access` | `MICROSOFT_OAUTH_CLIENT_ID` (a public client: no secret) |
| GitLab | [Applications](https://gitlab.com/-/user_settings/applications), confidential | `read_repository read_user` | `GITLAB_OAUTH_CLIENT_ID`, `GITLAB_OAUTH_CLIENT_SECRET` |
| Google | [Credentials](https://console.cloud.google.com/apis/credentials): OAuth client *Web application*, Drive API enabled | `drive.readonly` | `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` |

In pheasant-kb these go in `.env`; `compose.yaml` hands them to the api and
the indexer and to no other tier. Without one, the panel takes a client for
that sign-in, or the service connects with a token as before.
`security.connections.microsoft_tenant` (default `common`) narrows the
Microsoft sign-in to one tenant.

GitHub's OAuth apps have no read-only scope for private repositories, so a
sign-in grants `repo`; a fine-grained personal access token is the narrower
choice. A Google service account needs no browser: share the folders with its
email address and paste its JSON key.

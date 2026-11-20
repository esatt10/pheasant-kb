# OneDrive and SharePoint

Connect a Microsoft 365 account once, then paste any OneDrive or SharePoint
link into **+ Add source**: a whole site, its *Site contents*, a document
library, a folder, a list, the site's pages or one page, or a sharing link.
pheasant reads what the link names and everything beneath it, and the site
appears in the taxonomy view as folders, the way a web collection's pages do.

## What a site becomes

One source is one site: a SharePoint team or communication site, or one
person's OneDrive. Inside it:

| Path | What it is |
|---|---|
| `<library>/<folder>/.../<file>` | Every file in a document library (`Documents/Specs/plan.docx`), including folders shared into it from another drive |
| `Lists/<list>/<id>-<title>.md` | One document per list item: its title, created and modified times, a link back, and each field under its display name (rich text read as words, people and lookups by name) |
| `Lists/<list>/<id>-<title>/<attachment>` | Each attachment of that item, read like any other file. The item's document links it, so the graph has the edge and **Links** shows it |
| `Pages/<page>.html` | Each modern page, rendered from its canvas: text web parts as written, other web parts by the words they show. The viewer shows it as **Page** beside its **Text** |

Every format pheasant reads is indexed (Word, PowerPoint, Excel, PDF, text,
Markdown, images and audio where captioning and transcription are on); a video
or an unknown format is left out without being downloaded. Set `include` on
the source to narrow it.

## Add it

1. **Connect Microsoft 365.** Paste the link into **+ Add source**; the
   **Microsoft 365** toggle under the field opens by itself. Choose one:
   - **Sign in with Microsoft 365**: a person signs in, and pheasant reads what
     that person can read. Needs the region's OAuth client (below).
   - **App registration**: an Entra app with the *application* permission
     `Sites.Read.All` (or `Sites.Selected`, granted per site) and admin
     consent: its tenant ID, client ID and a client secret. No browser, so an
     agent can connect it.
   - **Access token**: a Graph token pasted as it is. It expires within about
     an hour; sign in or use an app to keep it.
2. **Add and index.** The source is named after the site
   (`sharepoint-contoso-marketing`, `onedrive-contoso-jane-contoso-com`, or
   `onedrive` for a personal Microsoft account).
3. **Add more of the same site.** Paste another link into the same site, or
   use **Edit links** (MCP `edit_source_urls`, `POST /sources/{name}/urls`):
   it joins the site's source rather than making a second one. Removing a link
   removes what it brought in, at the next sync.

Links pheasant understands, all on `<tenant>.sharepoint.com` or
`<tenant>-my.sharepoint.com`:

| You paste | pheasant reads |
|---|---|
| `https://contoso.sharepoint.com/sites/Marketing`, its home page, or *Site contents* (`/_layouts/15/viewlsts.aspx`) | Every library, list and page of the site |
| `.../Shared Documents`, a folder in it, or a library view (`Forms/AllItems.aspx?id=...`) | That library or folder, recursively |
| `.../Shared Documents/Specs/plan.docx` | That file |
| `.../Lists/Tasks` (or any of its views) | The list's items and their attachments |
| `.../SitePages` / `.../SitePages/Onboarding.aspx` | Every page / that page |
| A sharing link (`/:f:/s/...`, `/:w:/g/...`, `1drv.ms`, `onedrive.live.com`) | Whatever it shares, at the path its library would give it |
| `https://contoso-my.sharepoint.com/personal/jane_contoso_com/...` | Jane's OneDrive, or the folder named |

From an agent: `add_source` with the link (and `connection` naming the
Microsoft 365 connection from `list_connections`), then `wait_for_sync`.
From the CLI: `pheasant up https://contoso.sharepoint.com/sites/Marketing`.
In a config file:

```yaml
sources:
  - name: sharepoint-contoso-marketing
    type: sharepoint
    path: /unused
    include: ["**/*"]
    urls:
      - https://contoso.sharepoint.com/sites/Marketing/Shared%20Documents/Specs
      - https://contoso.sharepoint.com/sites/Marketing/Lists/Tasks
    connector:
      connection: microsoft-365        # or api_key_env: MICROSOFT_GRAPH_TOKEN
```

## Sign-in: the region's OAuth client

**Sign in with Microsoft 365** uses the same Entra app registration as the
Azure DevOps sign-in (`MICROSOFT_OAUTH_CLIENT_ID`, a public client; set it on
the api and the indexer). Give it the *delegated* Microsoft Graph permissions
`Files.Read.All`, `Sites.Read.All` and `User.Read`, and the SharePoint
permission `AllSites.Read` for list attachments. Its redirect URI is the
region's `/connections/oauth/callback`.
`security.connections.microsoft_tenant` (default `common`) narrows it to one
tenant; set `organizations` if personal accounts should not be offered.

## Keeping it current

Each sync lists the links again. A file, item or page whose version (a drive
item's `cTag`, a list item's `eTag`, a page's last-modified time) has not moved
is skipped before anything is downloaded, so a second sync of an unchanged
site reads nothing. Something deleted, moved out, or no longer shared leaves
the index at the next sync.

A link that cannot be read (removed, or not shared with the connection) is
named in the source's health as unreadable, under `_unreadable/`, and that sync
removes nothing: a listing that is missing one link's content cannot say what
was deleted. A refused credential (401) fails the sync with "sign in again".

## Limits

- **List attachments** are served by SharePoint's own REST API, not Microsoft
  Graph, so they need a second token for the SharePoint host. A sign-in gets
  it from the same refresh token. An app registration's *client secret* is
  refused by SharePoint's REST API (it requires a certificate), and a pasted
  Graph token cannot reach it unless a SharePoint token is pasted beside it.
  Then the items are indexed and each says its attachments could not be read.
- **Classic pages** (wiki and web part pages in older sites) are not modern
  pages and are not read; files in the *Site Pages* library are skipped.
- **Permissions** are not mirrored: everyone the region serves can search what
  the connection could read. Connect an account (or grant an app
  `Sites.Selected`) that reads only what the region's users should see.
- **Large tenants:** each sync walks every folder it was given (Graph pages of
  200). Point a source at the libraries and lists that matter rather than at
  the tenant root.

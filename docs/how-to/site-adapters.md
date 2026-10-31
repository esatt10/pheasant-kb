# Add a site adapter

A web collection indexes what a URL's server returns. For a JavaScript
single-page application that is an empty shell: a `<div>`, a script bundle and
a `<noscript>` asking for JavaScript. The text a browser shows arrives later,
when that script calls the site's API. pheasant runs no page scripts, so a
page like that indexes as "Please enable JavaScript" and nothing else.

A **site adapter** fixes this for one site. Given a page URL, it fetches the
same document from the site's own API and hands it to the normal pipeline.
This page explains how to write one, using the Terraform Registry adapter
(`src/pheasant/sync/web_adapters.py`) as the worked example.

## When an adapter is the right fix

Check three things first:

1. **The page really is a shell.** A sync logs `… is a JavaScript application
   shell` for the page, and the source's checkpoint lists it under
   `script_shells`. You can also see it directly:

    ```bash
    curl -s https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/ai_foundry \
      | python -c "import sys,re; print(re.sub(r'<[^>]+>',' ',sys.stdin.read())[-400:])"
    ```

    If the text you want is in that output, the problem is extraction (see
    `ingestion.extractor.html_chrome`), not a shell.

2. **The site serves the document without a browser.** You need a public URL
   that returns the content as JSON, Markdown or HTML, with no login, cookie
   or token that only a browser session holds. To find it, open the page in a
   browser, open the developer tools' **Network** tab, filter by
   **Fetch/XHR**, reload, and look for the request whose response holds the
   page's text.

3. **Nothing simpler exists.** If the site's source is a public repository,
   such as provider docs on GitHub, a `repository` source may be the better
   answer: it is versioned, incremental, and needs no code. An adapter is for
   when the reader wants what the *page URL* names, such as `latest` following
   new releases.

!!! note "Not a headless browser"
    An adapter makes ordinary HTTP requests to an API and reads data. It does
    not run the page's JavaScript. That keeps indexing deterministic, and it
    keeps a fetched site's code out of the indexer.

## The Terraform Registry case

What the page shows is the registry's API at work:

| Step | Request | Returns |
|---|---|---|
| The page | `GET /providers/hashicorp/azurerm/latest/docs/resources/ai_foundry` | An Ember shell with no documentation |
| 1. The provider's doc pages | `GET /v1/providers/hashicorp/azurerm` (or `…/azurerm/{version}`) | JSON: the version and every doc page as `{id, category, slug, language, …}` |
| 2. One page | `GET /v2/provider-docs/{id}` | JSON: `data.attributes.content`, the page's Markdown source |

So the adapter takes `namespace`, `name`, `version`, `category` and `slug`
from the page path. It reads the listing for that provider version, finds the
entry whose `category` and `slug` match (preferring `language: hcl`), and
fetches that entry's Markdown. The listing is about 400 KB for `azurerm`, and
a collection usually lists several pages of one provider, so the adapter reads
it once per sync and keeps it in `cache`.

## The contract

An adapter is a class that satisfies the `SiteAdapter` protocol in
`src/pheasant/sync/web_adapters.py`:

| Member | Meaning |
|---|---|
| `name` | A stable identifier, such as `"terraform-registry"`. It is stored in item metadata and in the source fingerprint. |
| `suffix` | The format the adapter returns, such as `".md"`. It is appended to the artifact path at listing time, which picks the chunking profile and the viewer. |
| `mime_type` | The content type of what `fetch` returns, such as `"text/markdown"`. |
| `matches(url)` | Whether this adapter answers for `url`. Decided by the URL alone: no requests, no state. |
| `fetch(url, fetch, cache)` | Returns the document. See the rules below. |

`fetch` returns a dict in the same shape `connectors._urlopen` does:

```python
{
    "not_modified": False,
    "content": b"...",            # the document itself, as bytes
    "mime_type": self.mime_type,
    "etag": None,                 # or the API's validators, if it sends any
    "last_modified": None,
    "headers": {...},             # the final response's headers
    "adapter": self.name,
    "api_url": doc_url,           # the request the content came from
}
```

The rules for `fetch`:

- **Make every request through the `fetch` argument**, never `urllib`
  directly. It carries the source's `connector.headers` and timeout, and it
  is the seam the offline tests replace.
- **Raise `SiteAdapterError` when the URL names nothing the site has.** The
  engine records that as the page's read error, keeps what was indexed before,
  and goes on with the other pages. Do not fall back to fetching the HTML
  page: that indexes the shell, which is the thing the adapter exists to
  prevent. Do not raise `ConnectorUnavailable` either, because that fails the
  whole source.
- **Use `cache` for anything shared between pages** (a listing, a version
  lookup). The connector empties it at the start of every sync.
- **Return the document, not a rendering of it.** If the API holds Markdown,
  return Markdown with `suffix = ".md"`. The pipeline already chunks it by
  heading and the UI already renders it. Return HTML with `suffix = ".html"`
  only when that is what the site stores.
- **Keep it deterministic.** No timestamps, request ids or other per-fetch
  noise in `content`. The sha256 of `content` decides whether the page
  changed, so noise re-indexes the page on every check.

## Step by step

### 1. Write the class

Add it to `src/pheasant/sync/web_adapters.py`, beside
`TerraformRegistryAdapter`. In outline:

```python
class ExampleDocsAdapter:
    """``docs.example.com`` pages, from the site's content API."""

    name = "example-docs"
    suffix = ".md"
    mime_type = "text/markdown"
    _route = re.compile(r"^/guides/(?P<slug>[^/]+)/?$")

    def matches(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.hostname == "docs.example.com" and bool(self._route.match(parsed.path))

    def fetch(self, url: str, fetch: Fetch, cache: dict[str, Any]) -> dict[str, Any]:
        slug = self._route.match(urlparse(url).path)["slug"]
        api_url = f"https://docs.example.com/api/pages/{quote(slug, safe='')}"
        response = fetch(api_url)
        body = _json(response, api_url).get("markdown")
        if not isinstance(body, str) or not body.strip():
            raise SiteAdapterError(f"docs.example.com has no page {slug!r}: {url}")
        return {
            "not_modified": False,
            "content": body.encode("utf-8"),
            "mime_type": self.mime_type,
            "etag": None,
            "last_modified": None,
            "headers": response.get("headers") or {},
            "adapter": self.name,
            "api_url": api_url,
        }
```

Match narrowly. The Terraform adapter answers only for
`/providers/{ns}/{name}/{version}/docs[/{category}/{slug}]` and leaves
modules and provider landing pages alone. A URL the adapter cannot serve
should be indexed the ordinary way, not refused.

Escape every path segment you put into an API URL with `quote(…, safe="")`,
since it comes from a URL someone typed.

### 2. Register it

Add an instance to `SITE_ADAPTERS`. The first adapter whose `matches` returns
`True` answers for a URL, so put a more specific adapter before a broader one
for the same host.

### 3. Bump `WEB_ADAPTERS_VERSION`

The version is part of the source fingerprint, but only for collections that
list a URL some adapter matches. Bumping it makes exactly those collections
re-read their pages once, on the next sync. Bump it whenever an adapter
changes what text a page produces, adding a new adapter included. Without the
bump, a page already checked recently stays on its old schedule, and its new
`.md` artifact is not read until the page is next due.

### 4. Test it offline

`pytest` never touches the network. Copy the pattern in
`tests/test_web_site_adapters.py`: replace `web_connector._urlopen` with a
dict of URL to response, then sync a real `SyncEngine` over a
`web_collection`. Cover at least:

- a page indexes from the API's content, under the adapter's suffix, with the
  page URL as its `path`, and the shell is never requested;
- anything shared is fetched once per sync (`requests.count(LISTING) == 1`);
- a URL the site does not have becomes that page's entry in
  `result.details["unreadable"]`, and the other pages still index;
- `matches` accepts the URLs it should and rejects its near misses (another
  path on the same host, another host);
- any slug containing a `.` keeps its whole name (the suffix is appended,
  not substituted).

Then run the suites the connector already has:

```bash
pytest -q tests/test_web_site_adapters.py tests/test_web_page_registration.py \
  tests/test_sync_connectors.py tests/test_sync_idempotency.py
```

### 5. Verify against the real site

A fixture cannot tell you the API's real shape. Run the connector against the
live site once before opening a pull request:

```python
import tempfile
from pathlib import Path
from pheasant.config.schema import SourceConfig, SourceConnectorSettings, SourceType
from pheasant.persistence.state_store import StateStore
from pheasant.sync.web_connector import WebCollectionConnector

source = SourceConfig(
    name="check", type=SourceType.web_collection, path=Path("/unused"),
    urls=["https://docs.example.com/guides/getting-started"],
    connector=SourceConnectorSettings(allow_experimental=True, request_timeout_seconds=30),
)
connector = WebCollectionConnector(source, StateStore(Path(tempfile.mkdtemp()) / "s.db"))
connector.begin_sync("full")
for item in connector.list_items():
    payload = connector.read_item(item)
    print(item.relative_path, payload.mime_type, payload.size_bytes)
    print(payload.content.decode()[:500])
```

Then add the URL to a running region and check the document in the taxonomy
view: it should open in the Markdown (or HTML) view with its real headings,
and a search for a phrase from deep in the page should find it.

### 6. Document it

Add the site to the list under
[JavaScript application pages](sources.md#javascript-application-pages), and
to the `site_adapters` row of the
[`sources[].connector` table](../configuration.md#sourcesconnector).

## Checklist

- [ ] The page is a shell (`script_shells`), and a public API serves its content.
- [ ] `matches` is narrow and makes no requests.
- [ ] Every request goes through `fetch`; shared data uses `cache`.
- [ ] A missing page raises `SiteAdapterError`; nothing falls back to the shell.
- [ ] `content` is the document, deterministic, with a matching `suffix`.
- [ ] Added to `SITE_ADAPTERS`, and `WEB_ADAPTERS_VERSION` bumped.
- [ ] Offline tests cover the cases above; existing connector suites pass.
- [ ] Checked once against the live site.
- [ ] The site is listed in `sources.md` and `configuration.md`.

Turning adapters off for one source is `connector.site_adapters: false`. That
source then indexes exactly what each URL returns.

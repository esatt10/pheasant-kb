"""A page whose server sends a JavaScript shell is read from its site's API.

Found on a live region: every Terraform Registry doc page indexed as
"Terraform Registry / Please enable Javascript to use this application",
because the registry is an Ember app and its HTML holds no documentation.
The adapter reads the page's Markdown from the registry's own API; a shell
from a site with no adapter is said to be one. Offline: the registry is a
dict of URL -> response behind ``web_connector._urlopen``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import (
    PLACEHOLDER_SOURCE_PATH,
    ExtractorSettings,
    IngestionSettings,
    PheasantConfig,
    PheasantSettings,
    SourceConfig,
    SourceConnectorSettings,
    SourceSyncSettings,
    SourceType,
)
from pheasant.ingestion.html_text import looks_like_script_shell
from pheasant.sync import web_connector
from pheasant.sync.engine import SyncEngine
from pheasant.sync.fingerprint import source_fingerprint
from pheasant.sync.web_adapters import adapter_for

PAGE = "https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/ai_foundry"
PROJECT = PAGE + "_project"
LISTING = "https://registry.terraform.io/v1/providers/hashicorp/azurerm"

SHELL = (
    b"<!DOCTYPE html><html><head><title>Terraform Registry</title>"
    b'<script src="/assets/vendor.js"></script></head><body>'
    b"<noscript>Please enable Javascript to use this application</noscript>"
    b'<div id="ember-root"></div></body></html>'
)

FOUNDRY_MD = (
    "---\nsubcategory: Machine Learning\n---\n\n# azurerm_ai_foundry\n\n"
    "Manages a Microsoft Foundry Hub (classic) resource.\n\n## Arguments Reference\n\n"
    "* `storage_account_id` - (Required) The quokkahub storage account.\n"
)
PROJECT_MD = "# azurerm_ai_foundry_project\n\nManages a Foundry project.\n"


def _json(payload: Any) -> dict[str, Any]:
    return {
        "not_modified": False,
        "content": json.dumps(payload).encode(),
        "mime_type": "application/vnd.api+json",
        "etag": None,
        "last_modified": None,
        "headers": {"Cache-Control": "public, max-age=30"},
    }


def _doc(content: str) -> dict[str, Any]:
    return _json({"data": {"attributes": {"content": content, "truncated": False}}})


def _registry() -> dict[str, dict[str, Any]]:
    docs = [
        {"id": "1", "category": "resources", "slug": "ai_foundry", "language": "hcl"},
        {"id": "2", "category": "resources", "slug": "ai_foundry_project", "language": "hcl"},
        {"id": "3", "category": "overview", "slug": "index", "language": "hcl"},
    ]
    return {
        LISTING: _json({"version": "5.12.0", "docs": docs}),
        "https://registry.terraform.io/v2/provider-docs/1": _doc(FOUNDRY_MD),
        "https://registry.terraform.io/v2/provider-docs/2": _doc(PROJECT_MD),
        PAGE: {
            "not_modified": False,
            "content": SHELL,
            "mime_type": "text/html",
            "etag": None,
            "last_modified": None,
            "headers": {},
        },
    }


@pytest.fixture()
def requests(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    served = _registry()
    asked: list[str] = []

    def fake_urlopen(url: str, headers: dict[str, str], timeout: int, **_: Any) -> dict:
        asked.append(url)
        if url not in served:
            raise OSError(f"404 {url}")
        return served[url]

    monkeypatch.setattr(web_connector, "_urlopen", fake_urlopen)
    return asked


def _source(urls: list[str], *, site_adapters: bool = True) -> SourceConfig:
    return SourceConfig(
        name="tf",
        type=SourceType.web_collection,
        path=Path(PLACEHOLDER_SOURCE_PATH),
        urls=urls,
        sync=SourceSyncSettings(on_startup=False),
        connector=SourceConnectorSettings(allow_experimental=True, site_adapters=site_adapters),
    )


def _engine(tmp_path: Path, source: SourceConfig) -> SyncEngine:
    return SyncEngine(
        PheasantConfig(
            pheasant=PheasantSettings(
                name="adapters",
                state_path=tmp_path / "state",
                workspace_root=tmp_path,
                exports_path=tmp_path / "exports",
            ),
            ingestion=IngestionSettings(extractor=ExtractorSettings(html_text=True)),
            sources=[source],
        )
    )


def _indexed(engine: SyncEngine) -> dict[str, str]:
    rows = engine.state.rows(
        "SELECT a.relative_path, a.path, c.text FROM chunks c "
        "JOIN artifacts a ON a.id = c.artifact_id WHERE a.source_id = ?",
        ("tf",),
    )
    text: dict[str, str] = {}
    for row in rows:
        text[row["relative_path"]] = text.get(row["relative_path"], "") + row["text"]
        assert row["path"].startswith("https://registry.terraform.io/providers/")
    return text


def test_a_registry_page_is_indexed_from_its_markdown(tmp_path: Path, requests: list[str]) -> None:
    engine = _engine(tmp_path, _source([PAGE, PROJECT]))

    result = engine.sync_source("tf", "full")

    assert result.indexed_artifacts == 2
    text = _indexed(engine)
    assert sorted(text) == [
        "registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/ai_foundry.md",
        "registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/"
        "ai_foundry_project.md",
    ]
    foundry = text[sorted(text)[0]]
    assert "quokkahub storage account" in foundry
    assert "enable Javascript" not in foundry
    # The page's HTML is never asked for, and the provider's listing (every
    # doc page it has) once for both pages.
    assert PAGE not in requests
    assert requests.count(LISTING) == 1
    hits = engine.search_context("quokkahub storage account", max_results=3)["results"]
    # Found by its content, and named by the page a person would open.
    assert hits and hits[0]["path"] == PAGE


def test_a_page_the_registry_does_not_list_is_that_pages_read_error(
    tmp_path: Path, requests: list[str]
) -> None:
    missing = PAGE.replace("ai_foundry", "no_such_resource")
    engine = _engine(tmp_path, _source([PAGE, missing]))

    result = engine.sync_source("tf", "full")

    assert result.indexed_artifacts == 1
    [failure] = result.details["unreadable"]
    assert failure["path"].endswith("no_such_resource.md")
    assert "lists no resources/no_such_resource page" in failure["error"]


def test_with_adapters_off_the_shell_is_indexed_and_said(
    tmp_path: Path, requests: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    engine = _engine(tmp_path, _source([PAGE], site_adapters=False))

    with caplog.at_level(logging.WARNING, logger="pheasant.sync.connectors"):
        engine.sync_source("tf", "full")

    [(path, text)] = _indexed(engine).items()
    assert path.endswith("ai_foundry.txt")
    assert "enable Javascript" in text
    assert requests == [PAGE]
    assert "JavaScript application shell" in caplog.text
    checkpoint = engine.state.get_source_checkpoint("tf")
    assert checkpoint is not None
    assert PAGE in json.dumps(checkpoint)


def test_registry_urls_map_to_their_docs() -> None:
    adapter = adapter_for(PAGE)
    assert adapter is not None and adapter.suffix == ".md"
    assert adapter_for("https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs")
    assert adapter_for(PAGE + "?product_intent=terraform")
    # Not a provider doc page: modules, the provider's landing page, another host.
    assert adapter_for("https://registry.terraform.io/modules/Azure/aks/azurerm/latest") is None
    assert adapter_for("https://registry.terraform.io/providers/hashicorp/azurerm/latest") is None
    assert adapter_for(PAGE.replace("registry.terraform.io", "example.com")) is None


def test_a_dotted_slug_keeps_its_whole_name(tmp_path: Path, requests: list[str]) -> None:
    guide = PAGE.replace("resources/ai_foundry", "guides/3.0-upgrade-guide")
    engine = _engine(tmp_path, _source([guide]))
    connector = web_connector.WebCollectionConnector(engine.config.sources[0], engine.state)
    [item] = connector.list_items()
    # Appended, not substituted: `.0-upgrade-guide` is not a suffix to replace.
    assert item.relative_path.endswith("/guides/3.0-upgrade-guide.md")


def test_only_a_collection_listing_an_adapted_url_changes_fingerprint() -> None:
    plain = ["https://example.com/a"]
    assert source_fingerprint(_source(plain)) == source_fingerprint(
        _source(plain, site_adapters=False)
    )
    assert source_fingerprint(_source([PAGE])) != source_fingerprint(
        _source([PAGE], site_adapters=False)
    )


@pytest.mark.parametrize(
    ("page", "shell"),
    [
        (SHELL, True),
        # A React root with a bundle and nothing else.
        (b'<html><body><div id="root"></div><script src="/main.js"></script></body></html>', True),
        # A real page that also loads a script.
        (
            b"<html><body><h1>Field engineering</h1><p>"
            + b"The team embeds with customers and ships integrations. " * 8
            + b'</p><script src="/analytics.js"></script></body></html>',
            False,
        ),
        # Short, but no script and no ask for one: a short page, not a shell.
        (b"<html><body><p>Moved to /docs.</p></body></html>", False),
    ],
)
def test_script_shells_are_recognised(page: bytes, shell: bool) -> None:
    assert looks_like_script_shell(page) is shell

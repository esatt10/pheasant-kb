"""A long PDF opens by range, and the agent dialog names the deployed region.

Both were found on the Azure deployment, from a phone. A book inside an
uploaded ZIP had to download whole (58 MB) before pdf.js drew anything,
because a member answered no ``Range`` request; a plain file did answer
them, but re-hashed itself on every one. And "Connect a coding agent"
offered ``http://0.0.0.0:8765/mcp`` and a stdio command naming a config
file inside the container.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.inventory_routes import ranged_bytes
from pheasant.api.mcp_connect import base_url, remote_configs, request_origin
from pheasant.services import raw_bytes, source_browser
from tests.test_source_browser import ARCHIVE, PDF, _region

MEMBER = "bundle.zip/papers/Graph Algorithms.pdf"


@pytest.fixture(autouse=True)
def _fresh_caches() -> None:
    raw_bytes.clear()


# ---------------------------------------------------------------------------
# Ranges
# ---------------------------------------------------------------------------


def _ranged(content: bytes, spec: str | None, if_range: str | None = None) -> Any:
    return ranged_bytes(
        content,
        media_type="application/pdf",
        headers={"ETag": '"abc"'},
        range_header=spec,
        if_range=if_range,
    )


def test_one_range_is_a_206_of_exactly_those_bytes() -> None:
    body = bytes(range(256)) * 4
    first = _ranged(body, "bytes=10-19")
    assert first.status_code == 206
    assert first.body == body[10:20]
    assert first.headers["content-range"] == f"bytes 10-19/{len(body)}"
    assert first.headers["accept-ranges"] == "bytes"

    assert _ranged(body, "bytes=-16").body == body[-16:]
    assert _ranged(body, "bytes=1000-").body == body[1000:]
    # An end past the file is the end of the file, as RFC 9110 says.
    assert _ranged(body, "bytes=1020-5000").body == body[1020:]


def test_what_a_range_cannot_be_answered_with() -> None:
    body = b"x" * 100
    beyond = _ranged(body, "bytes=100-120")
    assert beyond.status_code == 416
    assert beyond.headers["content-range"] == "bytes */100"
    # Several ranges, a malformed one, or a stale If-Range: the whole body.
    for spec, if_range in (("bytes=0-1,5-6", None), ("bytes=a-b", None), ("bytes=0-9", '"old"')):
        whole = _ranged(body, spec, if_range)
        assert whole.status_code == 200 and whole.body == body, spec
    assert _ranged(body, "bytes=0-9", '"abc"').status_code == 206


def test_a_zip_member_answers_ranges_and_is_inflated_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = _region(tmp_path, archive=ARCHIVE)
    reads: list[str] = []
    real = source_browser._indexed_bytes

    def counting(row: Any, limit: int | None) -> bytes | None:
        reads.append(str(row["relative_path"]))
        return real(row, limit)

    monkeypatch.setattr(source_browser, "_indexed_bytes", counting)
    client = mine["client"]
    whole = client.get("/documents/raw", params={"path": MEMBER})
    assert whole.status_code == 200 and whole.content == PDF
    assert whole.headers["accept-ranges"] == "bytes"
    for spec, expected in (
        ("bytes=0-1023", PDF[:1024]),
        (f"bytes=-{len(PDF) // 2}", PDF[-(len(PDF) // 2) :]),
    ):
        part = client.get("/documents/raw", params={"path": MEMBER}, headers={"Range": spec})
        assert part.status_code == 206, part.text
        assert part.content == expected
    assert reads == [MEMBER], "every request after the first must be served from the cache"


def test_a_rewritten_archive_is_never_served_from_the_cache(tmp_path: Path) -> None:
    mine = _region(tmp_path, archive=ARCHIVE)
    assert mine["client"].get("/documents/raw", params={"path": MEMBER}).status_code == 200
    archive = mine["workspace"] / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, body in ARCHIVE.items():
            bundle.writestr(name, body + b"tampered" if isinstance(body, bytes) else body)
    assert mine["client"].get("/documents/raw", params={"path": MEMBER}).status_code == 404


def test_a_file_is_hashed_once_per_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mine = _region(tmp_path)
    hashed: list[int] = []
    real = raw_bytes.hashlib.sha256

    class Counting:
        @staticmethod
        def sha256(*args: Any) -> Any:
            hashed.append(1)
            return real(*args)

    monkeypatch.setattr(raw_bytes, "hashlib", Counting)
    for _ in range(3):
        response = mine["client"].get(
            "/documents/raw", params={"path": "img/logo.png"}, headers={"Range": "bytes=0-7"}
        )
        assert response.status_code == 206
    assert len(hashed) == 1

    (mine["workspace"] / "img" / "logo.png").write_bytes(b"\x89PNG edited")
    assert mine["client"].get("/documents/raw", params={"path": "img/logo.png"}).status_code == 404


def test_the_member_cache_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(raw_bytes, "MEMBER_CACHE_BYTES", 10)
    archive = tmp_path / "a.zip"
    archive.write_bytes(b"zip")
    for member in ("one", "two", "three"):
        assert raw_bytes.member_bytes(archive, member, "s", lambda: b"12345") == b"12345"
    assert raw_bytes._member_bytes <= 10
    # Larger than the whole cache: served, never kept.
    assert raw_bytes.member_bytes(archive, "big", "s", lambda: b"x" * 11) == b"x" * 11
    assert all(key[3] != "big" for key in raw_bytes._members)


# ---------------------------------------------------------------------------
# Connect a coding agent
# ---------------------------------------------------------------------------


class _Config:
    class server:  # noqa: N801 - mirrors the config's attribute path
        host = "0.0.0.0"
        port = 8765


def test_the_url_is_where_the_region_is_reached_never_its_bind_address() -> None:
    headers = {"host": "kb.example.com", "x-forwarded-proto": "https"}
    origin = request_origin(headers, "http", "10.0.0.4:8765")
    assert origin == "https://kb.example.com"
    assert base_url(_Config, "", origin) == "https://kb.example.com"
    # A configured public URL wins over whatever the request says.
    assert base_url(_Config, "https://public.example.com/", origin) == "https://public.example.com"
    # Asked from nowhere usable, the bind address 0.0.0.0 is never offered.
    assert base_url(_Config, "", "http://0.0.0.0:8765") == "http://127.0.0.1:8765"


def test_remote_configs_carry_a_header_only_without_oauth() -> None:
    url = "https://kb.example.com/mcp"
    oauth = remote_configs("kb", url, oauth=True, token=True)
    assert oauth["claude-code-command"] == f"claude mcp add --transport http kb {url}"
    assert json.loads(oauth["claude-code"]) == {"mcpServers": {"kb": {"type": "http", "url": url}}}
    assert json.loads(oauth["vscode"]) == {"servers": {"kb": {"type": "http", "url": url}}}

    token = remote_configs("kb", url, oauth=False, token=True)
    claude = json.loads(token["claude-code"])["mcpServers"]["kb"]
    # Read from the environment, so no key is written into a committed file.
    assert claude["headers"] == {"Authorization": "Bearer ${PHEASANT_API_TOKEN}"}
    vscode = json.loads(token["vscode"])
    assert vscode["servers"]["kb"]["headers"]["Authorization"] == "Bearer ${input:pheasant-token}"
    assert vscode["inputs"][0]["password"] is True
    assert "--header" in token["claude-code-command"]


def test_mcp_info_from_a_deployed_origin_is_remote(tmp_path: Path) -> None:
    mine = _region(tmp_path, server={"host": "0.0.0.0"})
    app = mine["client"].app
    remote = TestClient(app, base_url="https://kb.example.com").get("/mcp/info").json()
    assert remote["mode"] == "remote"
    assert remote["streamable_http_url"] == "https://kb.example.com/mcp"
    assert "0.0.0.0" not in json.dumps(remote["client_configs"])
    assert remote["client_configs"]["claude-code-command"].endswith("https://kb.example.com/mcp")
    assert remote["auth"] == {"oauth": False, "api_token": False, "personal_keys": False}

    local = TestClient(app, base_url="http://localhost:8765").get("/mcp/info").json()
    assert local["mode"] == "local"
    assert local["streamable_http_url"] == "http://localhost:8765/mcp"
    assert json.loads(local["client_configs"]["claude-code"])["mcpServers"]

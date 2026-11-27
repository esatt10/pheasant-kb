"""Network-free stand-ins for the services the harness reaches through connections.

Each fake answers the exact endpoints the harness's adapters call, checks the
credential the way the real service would (a wrong or missing token is a 401),
and records every request it received -- method, URL, headers, body -- so a
test can assert what was sent, to which host, carrying what.

``Network`` routes by host and stands in for ``pheasant_harness.connections.transport``.
A request to a host no fake owns is recorded and answered with a page that tries
to look like a success, so a test that asserts "never contacted" means it.
"""
# ruff: noqa: E501

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from pheasant_harness.connections import Response

GITHUB_TOKEN = "ghp_SENTINELgithub0harness0token0do0not0leak"
DRIVE_TOKEN = "ya29.SENTINELdrive0harness0token0do0not0leak"
GRAPH_TOKEN = "eyJ0SENTINELgraph0harness0token0do0not0leak"
SECRETS = (GITHUB_TOKEN, DRIVE_TOKEN, GRAPH_TOKEN)


def git_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


@dataclass
class Call:
    method: str
    url: str
    headers: dict
    body: bytes | None

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()

    @property
    def path(self) -> str:
        return urlsplit(self.url).path

    @property
    def authorization(self) -> str | None:
        return next((v for k, v in self.headers.items() if k.lower() == "authorization"), None)


def _json(status: int, value: Any, headers: dict | None = None) -> Response:
    return Response(
        status, {"Content-Type": "application/json", **(headers or {})}, json.dumps(value).encode()
    )


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


@dataclass
class Commit:
    sha: str
    files: dict[str, bytes]
    message: str
    date: str
    parent: str | None


class FakeGitHub:
    host = "api.github.com"

    def __init__(self, token: str = GITHUB_TOKEN):
        self.token = token
        self.repos: dict[str, dict] = {}
        self.truncate_trees = False
        self.corrupt_blobs: set[str] = set()
        self.echo_token = False
        self.redirect_blobs_to: str | None = None
        self._clock = 0

    def _stamp(self) -> str:
        self._clock += 1
        return f"2026-11-{10 + self._clock // 50:02d}T09:{self._clock % 60:02d}:00Z"

    def commit(
        self,
        repo: str,
        branch: str,
        files: dict[str, str | bytes],
        message: str = "edit",
        *,
        replace: bool = False,
    ) -> str:
        state = self.repos.setdefault(repo, {"branches": {}, "commits": {}})
        parent = state["branches"].get(branch)
        current = {} if replace or parent is None else dict(state["commits"][parent].files)
        for path, content in files.items():
            if content is None:
                current.pop(path, None)
            else:
                current[path] = content.encode() if isinstance(content, str) else content
        sha = hashlib.sha1(
            f"{repo}{branch}{message}{sorted(current.items())}{self._clock}".encode()
        ).hexdigest()
        state["commits"][sha] = Commit(sha, current, message, self._stamp(), parent)
        state["branches"][branch] = sha
        return sha

    def head(self, repo: str, branch: str) -> Commit:
        state = self.repos[repo]
        return state["commits"][state["branches"][branch]]

    def _find_blob(self, repo: str, oid: str) -> bytes | None:
        for commit in self.repos[repo]["commits"].values():
            for data in commit.files.values():
                if git_sha(data) == oid:
                    return data
        return None

    def handle(self, call: Call) -> Response:
        if call.authorization != f"Bearer {self.token}":
            return _json(401, {"message": "Bad credentials"})
        parts = urlsplit(call.url)
        query = parse_qs(parts.query)
        match = re.match(r"^/repos/([^/]+)/([^/]+)/(.*)$", parts.path)
        if not match:
            return _json(404, {"message": "Not Found"})
        repo = f"{unquote(match[1])}/{unquote(match[2])}"
        rest = match[3]
        if repo not in self.repos:
            return _json(404, {"message": "Not Found"})
        state = self.repos[repo]
        if call.method == "GET" and rest.startswith("commits/"):
            ref = unquote(rest[len("commits/") :])
            sha = state["branches"].get(ref) or (ref if ref in state["commits"] else None)
            if ref == "HEAD":
                sha = state["branches"].get("main")
            if sha is None:
                return _json(422, {"message": "No commit found"})
            commit = state["commits"][sha]
            return _json(
                200,
                {
                    "sha": sha,
                    "commit": {
                        "tree": {"sha": "t" + sha[1:]},
                        "message": commit.message,
                        "committer": {"date": commit.date},
                    },
                },
            )
        if call.method == "GET" and rest.startswith("git/trees/"):
            sha = "c" + rest[len("git/trees/") :][1:]
            commit = next((c for s, c in state["commits"].items() if s[1:] == sha[1:]), None)
            if commit is None:
                return _json(404, {"message": "Not Found"})
            dirs = sorted(
                {
                    "/".join(p.split("/")[:i])
                    for p in commit.files
                    for i in range(1, p.count("/") + 1)
                }
            )
            tree = [{"path": d, "mode": "040000", "type": "tree", "sha": "d" * 40} for d in dirs]
            tree += [
                {"path": p, "mode": "100644", "type": "blob", "sha": git_sha(d), "size": len(d)}
                for p, d in sorted(commit.files.items())
            ]
            if self.truncate_trees:
                tree = tree[: max(1, len(tree) // 2)]
            return _json(200, {"sha": sha, "tree": tree, "truncated": self.truncate_trees})
        if call.method == "GET" and rest.startswith("git/blobs/"):
            oid = rest[len("git/blobs/") :]
            if self.redirect_blobs_to:
                return Response(302, {"Location": self.redirect_blobs_to + oid}, b"")
            data = self._find_blob(repo, oid)
            if data is None:
                return _json(404, {"message": "Not Found"})
            if oid in self.corrupt_blobs:
                data = data + b"\n# tampered in transit\n"
            if self.echo_token:
                data = data + f"\n<!-- debug: Authorization {call.authorization} -->\n".encode()
            return Response(200, {"Content-Type": "application/vnd.github.raw"}, data)
        if rest.startswith("contents/"):
            path = unquote(rest[len("contents/") :])
            if call.method == "GET":
                branch = (query.get("ref") or ["main"])[0]
                head = state["branches"].get(branch)
                data = state["commits"][head].files.get(path) if head else None
                if data is None:
                    return _json(404, {"message": "Not Found"})
                return _json(
                    200,
                    {
                        "path": path,
                        "sha": git_sha(data),
                        "content": base64.b64encode(data).decode(),
                    },
                )
            if call.method == "PUT":
                body = json.loads(call.body or b"{}")
                branch = body["branch"]
                head = state["branches"].get(branch)
                existing = state["commits"][head].files.get(path) if head else None
                if existing is not None and body.get("sha") != git_sha(existing):
                    return _json(409, {"message": "sha does not match"})
                if existing is None and body.get("sha"):
                    return _json(422, {"message": "sha given for a file that does not exist"})
                data = base64.b64decode(body["content"])
                if head is None:
                    base = state["branches"]["main"]
                    state["branches"][branch] = base
                sha = self.commit(repo, branch, {path: data}, body["message"])
                return _json(
                    201 if existing is None else 200,
                    {
                        "content": {"path": path, "sha": git_sha(data)},
                        "commit": {"sha": sha, "message": body["message"]},
                    },
                )
        if call.method == "GET" and rest == "commits":
            branch = (query.get("sha") or ["main"])[0]
            path = (query.get("path") or [""])[0]
            sha = state["branches"].get(branch)
            out = []
            while sha:
                commit = state["commits"][sha]
                parent = state["commits"].get(commit.parent) if commit.parent else None
                if commit.files.get(path) != (parent.files.get(path) if parent else None):
                    out.append({"sha": sha, "commit": {"message": commit.message}})
                sha = commit.parent
            return _json(200, out)
        return _json(404, {"message": "Not Found"})


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------

FOLDER = "application/vnd.google-apps.folder"
GDOC = "application/vnd.google-apps.document"


@dataclass
class DriveFile:
    id: str
    name: str
    mime: str
    parent: str | None
    content: bytes = b""
    modified: str = "2026-11-20T10:00:00.000Z"
    app_properties: dict = field(default_factory=dict)
    trashed: bool = False


class FakeDrive:
    host = "www.googleapis.com"

    def __init__(self, token: str = DRIVE_TOKEN):
        self.token = token
        self.files: dict[str, DriveFile] = {}
        self._next = 0
        self.page_size = 200
        self.serve_stale_bytes: set[str] = set()

    def _id(self) -> str:
        self._next += 1
        return f"drv{self._next:05d}"

    def folder(self, name: str, parent: str | None = None) -> str:
        fid = self._id()
        self.files[fid] = DriveFile(fid, name, FOLDER, parent)
        return fid

    def file(
        self, parent: str, name: str, content: str | bytes, mime: str = "text/markdown"
    ) -> str:
        fid = self._id()
        self.files[fid] = DriveFile(
            fid, name, mime, parent, content.encode() if isinstance(content, str) else content
        )
        return fid

    def doc(self, parent: str, name: str, text: str) -> str:
        return self.file(parent, name, text, GDOC)

    def edit(self, fid: str, content: str | bytes, modified: str) -> None:
        self.files[fid].content = content.encode() if isinstance(content, str) else content
        self.files[fid].modified = modified

    def _meta(self, f: DriveFile) -> dict:
        out = {"id": f.id, "name": f.name, "mimeType": f.mime, "modifiedTime": f.modified}
        if f.mime != FOLDER and not f.mime.startswith("application/vnd.google-apps."):
            out["size"] = str(len(f.content))
            out["md5Checksum"] = hashlib.md5(f.content).hexdigest()
        if f.app_properties:
            out["appProperties"] = dict(f.app_properties)
        return out

    def _query(self, q: str) -> list[DriveFile]:
        parent = re.search(r"'([^']+)' in parents", q)
        name = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
        op = re.search(r"key='pheasantOperation' and value='([^']+)'", q)
        out = []
        for f in self.files.values():
            if f.trashed or (parent and f.parent != parent[1]):
                continue
            if name and f.name != name[1].replace("\\'", "'"):
                continue
            if op and f.app_properties.get("pheasantOperation") != op[1]:
                continue
            out.append(f)
        return sorted(out, key=lambda f: f.name)

    def handle(self, call: Call) -> Response:
        if call.authorization != f"Bearer {self.token}":
            return _json(401, {"error": {"code": 401, "message": "Invalid Credentials"}})
        parts = urlsplit(call.url)
        query = parse_qs(parts.query)
        path = parts.path
        if path == "/drive/v3/files" and call.method == "GET":
            found = self._query(query["q"][0])
            start = int((query.get("pageToken") or ["0"])[0])
            page = found[start : start + self.page_size]
            body = {"files": [self._meta(f) for f in page]}
            if start + self.page_size < len(found):
                body["nextPageToken"] = str(start + self.page_size)
            return _json(200, body)
        match = re.match(r"^/drive/v3/files/([^/]+)(/export)?$", path)
        if match and call.method == "GET":
            f = self.files.get(unquote(match[1]))
            if f is None:
                return _json(404, {"error": {"code": 404}})
            if match[2]:
                return Response(200, {"Content-Type": "text/plain"}, f.content)
            if query.get("alt") == ["media"]:
                data = f.content
                if f.id in self.serve_stale_bytes:
                    data = b"stale replica bytes"
                return Response(200, {"Content-Type": f.mime}, data)
            return _json(200, self._meta(f))
        if path.startswith("/upload/drive/v3/files"):
            boundary = re.search(r"boundary=(\S+)", call.headers.get("Content-Type", ""))[1]
            chunks = (call.body or b"").split(f"--{boundary}".encode())
            meta = json.loads(chunks[1].split(b"\r\n\r\n", 1)[1].strip())
            media = chunks[2].split(b"\r\n\r\n", 1)[1][: -len(b"\r\n")]
            if call.method == "POST":
                fid = self.file(meta["parents"][0], meta["name"], media, "text/markdown")
            else:
                fid = unquote(path.rsplit("/", 1)[1])
                self.files[fid].content = media
            self.files[fid].app_properties.update(meta.get("appProperties") or {})
            return _json(200, {"id": fid, "md5Checksum": hashlib.md5(media).hexdigest()})
        return _json(404, {"error": {"code": 404}})


# ---------------------------------------------------------------------------
# Microsoft Graph (OneDrive / SharePoint document libraries)
# ---------------------------------------------------------------------------


class FakeGraph:
    host = "graph.microsoft.com"
    download_host = "contoso.sharepoint.com"

    def __init__(self, token: str = GRAPH_TOKEN):
        self.token = token
        self.drives: dict[str, dict[str, bytes]] = {}
        self.page_size = 200
        self.next_link_host: str | None = None
        self.download_redirect_host: str | None = None

    def _children(self, drive: str, folder: str) -> list[dict]:
        files = self.drives[drive]
        prefix = folder + "/" if folder else ""
        names: dict[str, dict] = {}
        for path, data in files.items():
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix) :]
            head = rest.split("/", 1)[0]
            item_path = prefix + head
            if "/" in rest:
                names[head] = {"id": "f-" + item_path, "name": head, "folder": {"childCount": 1}}
            else:
                names[head] = {
                    "id": "i-" + item_path,
                    "name": head,
                    "size": len(data),
                    "cTag": "c" + hashlib.sha1(data).hexdigest()[:10],
                    "lastModifiedDateTime": "2026-11-21T08:00:00Z",
                    "file": {"hashes": {"sha256Hash": hashlib.sha256(data).hexdigest().upper()}},
                }
        return [names[k] for k in sorted(names)]

    def handle(self, call: Call) -> Response:
        parts = urlsplit(call.url)
        if call.host == self.download_host or call.host == self.download_redirect_host:
            if call.authorization is not None:
                return Response(400, {}, b"a pre-signed download URL must not carry a bearer token")
            drive, item = parts.path.split("/")[2], unquote(parts.path.split("/", 3)[3])
            return Response(
                200, {"Content-Type": "application/octet-stream"}, self.drives[drive][item[2:]]
            )
        if call.authorization != f"Bearer {self.token}":
            return _json(401, {"error": {"code": "InvalidAuthenticationToken"}})
        path = unquote(parts.path)
        query = parse_qs(parts.query)
        match = re.match(r"^/v1\.0/drives/([^/]+)/root(?::/(.*):)?/children$", path)
        if match:
            drive, folder = match[1], match[2] or ""
            children = self._children(drive, folder)
            start = int((query.get("$skiptoken") or ["0"])[0])
            page = children[start : start + self.page_size]
            body: dict = {"value": page}
            if start + self.page_size < len(children):
                host = self.next_link_host or self.host
                body["@odata.nextLink"] = (
                    f"https://{host}{parts.path}?$skiptoken={start + self.page_size}"
                )
            return _json(200, body)
        match = re.match(r"^/v1\.0/drives/([^/]+)/items/(.+)/content$", path)
        if match:
            host = self.download_redirect_host or self.download_host
            return Response(
                302, {"Location": f"https://{host}/dl/{match[1]}/{match[2]}?tempauth=signed"}, b""
            )
        match = re.match(r"^/v1\.0/drives/([^/]+)/items/(.+)$", path)
        if match:
            data = self.drives[match[1]].get(match[2][2:])
            return _json(200, {"cTag": "c" + hashlib.sha1(data or b"").hexdigest()[:10]})
        return _json(404, {"error": {"code": "itemNotFound"}})


# ---------------------------------------------------------------------------
# The network
# ---------------------------------------------------------------------------


class Network:
    def __init__(self, *services: Any):
        self.services = {s.host: s for s in services}
        for s in services:
            if isinstance(s, FakeGraph):
                self.services[s.download_host] = s
        self.calls: list[Call] = []
        self.unowned: list[Call] = []

    def __call__(self, method, url, headers, body, timeout, max_bytes) -> Response:
        call = Call(method, url, dict(headers), body)
        self.calls.append(call)
        service = self.services.get(call.host)
        if service is None:
            for s in self.services.values():
                if isinstance(s, FakeGraph) and call.host == s.download_redirect_host:
                    service = s
        if service is None:
            self.unowned.append(call)
            return Response(200, {}, b'{"sha": "' + b"e" * 40 + b'", "ok": true, "files": []}')
        response = service.handle(call)
        if len(response.body) > max_bytes:
            return Response(response.status, response.headers, response.body[:max_bytes], True)
        return response

    def to(self, host: str) -> list[Call]:
        return [c for c in self.calls if c.host == host]

    def writes(self) -> list[Call]:
        return [c for c in self.calls if c.method in ("PUT", "POST", "PATCH", "DELETE")]

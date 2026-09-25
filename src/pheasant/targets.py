"""Turn anything a user can name into a source entry.

``pheasant up ~/notes https://github.com/me/proj https://docs.example.com``
should just work, so this module answers one question: given an arbitrary
string, what source does the user mean? It classifies by shape — scheme,
git-ness, whether the path exists on disk and what it contains — and hands
back a ``ResolvedTarget`` the config renderer can write out verbatim.

Two extras make the "folder collection" case a one-liner:

* a glob (``~/clients/*``) expands to one source per matching directory;
* ``--split`` over a parent directory does the same without a glob,

so a directory of directories becomes a directory of *sources*, each
independently syncable, rather than one undifferentiated blob.

Remote git repositories are cloned once into ``<state>/sources/<slug>``
and then indexed as an ordinary local repository — the connector layer
never learns a new trick.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from pheasant.config.schema import SourceConfig, SourceType
from pheasant.git_auth import (
    git_env as _git_env,
)
from pheasant.git_auth import (
    github_authentication_error as _github_authentication_error,
)
from pheasant.git_auth import (
    github_token as _github_token,
)
from pheasant.git_auth import (
    is_github_https_url as _is_github_https_url,
)

GIT_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "git.sr.ht")

# Explicit override prefixes, so an unusual target is still a one-liner:
# `pheasant up notion:workspace` or `pheasant up web:https://…`.
TYPE_PREFIXES = {
    "repo": SourceType.repository,
    "repository": SourceType.repository,
    "git": SourceType.repository,
    "vault": SourceType.obsidian_vault,
    "obsidian": SourceType.obsidian_vault,
    "folder": SourceType.document_folder,
    "docs": SourceType.document_folder,
    "markdown": SourceType.markdown_folder,
    "notes": SourceType.markdown_folder,
    "file": SourceType.single_file,
    "web": SourceType.web_collection,
    "api": SourceType.api,
    "s3": SourceType.s3,
    "memory": SourceType.memory,
}

MARKDOWN_INCLUDES = ["**/*.md", "**/*.markdown"]
GIT_COMMAND_TIMEOUT_SECONDS = 600


class TargetError(ValueError):
    """A target string could not be resolved to a source."""


@dataclass
class ResolvedTarget:
    """One source entry, ready to render into a config."""

    name: str
    type: str
    path: str
    description: str
    include: list[str] | None = None
    urls: list[str] = field(default_factory=list)
    connector: dict | None = None
    # Remote repos: clone this before the first sync. None for local targets.
    clone_url: str | None = None
    # GitHub tree URLs clone the repository here, then expose only subpath.
    clone_path: str | None = None
    clone_ref: str | None = None
    # True when `path` is a real local directory/file that must exist.
    local: bool = True

    def to_source_dict(self) -> dict:
        payload: dict = {
            "name": self.name,
            "type": self.type,
            "path": self.path,
            "description": self.description,
        }
        if self.include:
            payload["include"] = list(self.include)
        if self.urls:
            payload["urls"] = list(self.urls)
        if self.connector:
            payload["connector"] = dict(self.connector)
        if self.clone_url:
            payload["repo"] = {
                "clone_url": self.clone_url,
                "clone_path": self.clone_path or self.path,
                "clone_ref": self.clone_ref,
            }
        return payload


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "source"


def is_git_url(spec: str) -> bool:
    """Remote git remotes, in all the shapes people actually paste."""
    if spec.startswith(("git@", "ssh://", "git://")):
        return True
    if spec.endswith(".git"):
        return True
    parsed = urlparse(spec)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        host = parsed.netloc.split("@")[-1].split(":")[0].lower()
        if host.removeprefix("www.") in GIT_HOSTS:
            parts = [p for p in parsed.path.split("/") if p]
            return len(parts) == 2 or (
                host.removeprefix("www.") == "github.com" and len(parts) >= 4 and parts[2] == "tree"
            )
    return False


#: Transports a clone URL may name. Everything else — notably git's
#: ``ext::`` helper, which runs an arbitrary shell command — is refused
#: before it reaches ``git clone``.
CLONE_SCHEMES = frozenset({"http", "https", "ssh", "git", "git+ssh"})

#: ``user@host:path``, the scp-like remote form git accepts without a scheme.
SCP_LIKE = re.compile(r"^[A-Za-z0-9._~+-]+@[A-Za-z0-9.-]+:(?!//).+$")


def validate_clone_url(url: str) -> str:
    """Refuse a clone URL that ``git clone`` must not be handed.

    ``clone_url`` reaches ``git clone`` as an argv element, and the string
    comes from whatever the caller typed (``pheasant up <x>``, or the
    unauthenticated ``POST /sources/quick-add``). Two shapes are dangerous
    regardless of how git happens to be configured on the host:

    * a leading ``-`` is parsed by git as an *option*, not a URL, which turns
      "clone this" into "run git with flags I chose";
    * a transport helper such as ``ext::`` names a command for git to run.
      Current git refuses ``ext`` by default, but that is git's defense, not
      ours, and it is a configuration flag away from being off.

    So the allowlist is positive: a known remote scheme, or the scp-like
    ``user@host:path`` form.
    """
    candidate = url.strip()
    if not candidate:
        raise TargetError("empty clone URL")
    if candidate.startswith("-"):
        raise TargetError(
            f"refusing to clone {url!r}: a URL starting with '-' would be read by git as an option"
        )
    parsed = urlparse(candidate)
    scheme = parsed.scheme.lower()
    if scheme:
        if scheme not in CLONE_SCHEMES:
            raise TargetError(
                f"refusing to clone {url!r}: unsupported transport {scheme!r} "
                f"(allowed: {', '.join(sorted(CLONE_SCHEMES))})"
            )
        if scheme in {"http", "https"} and (parsed.username or parsed.password):
            raise TargetError(
                "refusing a clone URL containing credentials; put a GitHub token in "
                "GITHUB_TOKEN/GH_TOKEN (or configure SSH) so secrets are never persisted"
            )
        return candidate
    if SCP_LIKE.match(candidate):
        return candidate
    raise TargetError(
        f"refusing to clone {url!r}: expected a "
        f"{'/'.join(sorted(CLONE_SCHEMES))} URL or a user@host:path remote"
    )


def repo_name_from_url(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return slugify(tail)


def detect_local_type(path: Path) -> SourceType:
    """Classify a local path by what it actually holds."""
    if path.is_file():
        return SourceType.single_file
    if (path / ".obsidian").is_dir():
        return SourceType.obsidian_vault
    if (path / ".git").is_dir():
        return SourceType.repository
    if _is_mostly_markdown(path):
        return SourceType.markdown_folder
    return SourceType.document_folder


def _is_mostly_markdown(path: Path) -> bool:
    """A notes folder is markdown-dominant; a mixed folder is not.

    Bounded scan — a shallow sample is enough to pick an include list, and
    walking a huge tree just to choose a default is not worth the latency.
    """
    markdown = 0
    other = 0
    try:
        for entry in list(path.rglob("*"))[:400]:
            if not entry.is_file() or entry.name.startswith("."):
                continue
            if entry.suffix.lower() in (".md", ".markdown"):
                markdown += 1
            else:
                other += 1
    except OSError:
        return False
    return markdown > 0 and markdown >= other


def _describe(source_type: SourceType, origin: str) -> str:
    return f"Auto-detected {source_type.value} from {origin}"


def _local_target(path: Path, *, name: str | None, forced: SourceType | None) -> ResolvedTarget:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise TargetError(f"path does not exist: {resolved}")
    source_type = forced or detect_local_type(resolved)
    include = None
    if source_type is SourceType.markdown_folder:
        include = list(MARKDOWN_INCLUDES)
    elif source_type is SourceType.document_folder:
        # A user who points setup/up at a mixed folder is explicitly asking
        # pheasant to inspect that folder.  The schema's conservative default
        # is intentionally code/Markdown-only, so carry an explicit broad
        # include here; the extractor/captioner/transcriber gates recognize it
        # and secret-file exclusions still apply at sync time.
        include = ["**/*"]
    elif source_type is SourceType.single_file:
        include = [resolved.name]
    return ResolvedTarget(
        name=name or slugify(resolved.stem if resolved.is_file() else resolved.name),
        type=source_type.value,
        path=str(resolved.parent if resolved.is_file() else resolved),
        description=_describe(source_type, str(resolved)),
        include=include,
    )


def _git_target(url: str, clone_root: Path, *, name: str | None) -> ResolvedTarget:
    original_url = url
    parsed = urlparse(url)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    tree_ref: str | None = None
    subpath: Path | None = None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host == "github.com" and len(parts) >= 4 and parts[2] == "tree":
        owner, repository, _, tree_ref, *subparts = parts
        if any(part in {"", ".", ".."} or "/" in part or "\\" in part for part in subparts):
            raise TargetError("GitHub repository subpath cannot contain '.' or '..'")
        url = f"{parsed.scheme}://github.com/{owner}/{repository}"
        subpath = Path(*subparts) if subparts else None
    url = validate_clone_url(url)
    repo = name or repo_name_from_url(url)
    destination = (clone_root / repo).resolve()
    return ResolvedTarget(
        name=repo,
        type=SourceType.repository.value,
        path=str(destination / subpath) if subpath else str(destination),
        description=f"Git repository cloned from {original_url}",
        clone_url=url,
        clone_path=str(destination),
        clone_ref=tree_ref,
    )


def _web_target(url: str, workspace: Path, *, name: str | None) -> ResolvedTarget:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    parts = [part for part in parsed.path.split("/") if part]
    if host == "github.com" and len(parts) >= 3 and parts[2] == "tree":
        # This is a guardrail as well as an error message. GitHub tree URLs
        # must go through _git_target; treating one as a web collection later
        # fails at WebCollectionConnector._require_experimental_enabled and
        # hides the real classification problem behind a traceback.
        raise TargetError(
            "GitHub /tree/ URLs are repository paths, not web collections; "
            "use https://github.com/<owner>/<repo>/tree/<ref>/<path>"
        )
    label = name or slugify(parsed.netloc or "web")
    return ResolvedTarget(
        name=label,
        type=SourceType.web_collection.value,
        # Web sources still carry a path (the cache/anchor dir); the URLs
        # are what actually gets fetched.
        path=str((workspace / "web" / label).resolve()),
        description=f"Web collection seeded from {url}",
        # No `include`: the connector admits every listed URL unless one is
        # chosen, and a glob list here dropped the very URL being added when
        # it was a .pdf. The experimental opt-in is the user's: naming a URL
        # on the command line is asking for it to be fetched, and without it
        # the first sync of the generated config failed.
        urls=[url],
        connector={"allow_experimental": True},
        local=False,
    )


def _s3_target(url: str, workspace: Path, *, name: str | None) -> ResolvedTarget:
    parsed = urlparse(url)
    label = name or slugify(parsed.netloc or "s3")
    return ResolvedTarget(
        name=label,
        type=SourceType.s3.value,
        path=str((workspace / "s3" / label).resolve()),
        description=f"S3 bucket {url}",
        urls=[url],
        local=False,
    )


def _plugin_target(kind: str, value: str, workspace: Path, *, name: str | None) -> ResolvedTarget:
    """A connector-plugin source (notion, slack, gdrive, …) by name.

    Step 31.1 resolves unknown type strings through the entry-point
    registry at dispatch time, so `up notion:my-workspace` needs no
    special-casing here beyond carrying the type through.
    """
    label = name or slugify(value or kind)
    return ResolvedTarget(
        name=label,
        type=kind,
        path=str((workspace / kind / label).resolve()),
        description=f"{kind} connector source ({value})" if value else f"{kind} connector source",
        local=False,
    )


def expand_specs(specs: list[str], *, split: bool = False) -> list[str]:
    """Expand globs and, with ``--split``, a parent directory's children."""
    expanded: list[str] = []
    for spec in specs:
        if any(ch in spec for ch in "*?[") and "://" not in spec:
            root = Path(spec).expanduser()
            base = root.parent if root.parent != root else Path(".")
            matches = sorted(p for p in base.glob(root.name) if p.is_dir() or p.is_file())
            if not matches:
                raise TargetError(f"no paths matched: {spec}")
            expanded.extend(str(p) for p in matches)
            continue
        if split and "://" not in spec:
            candidate = Path(spec).expanduser()
            if candidate.is_dir():
                children = sorted(
                    p for p in candidate.iterdir() if p.is_dir() and not p.name.startswith(".")
                )
                if children:
                    expanded.extend(str(p) for p in children)
                    continue
        expanded.append(spec)
    return expanded


def resolve_target(
    spec: str,
    *,
    clone_root: Path,
    workspace: Path,
    name: str | None = None,
) -> ResolvedTarget:
    """Classify one target string into a source entry."""
    spec = spec.strip()
    if not spec:
        raise TargetError("empty target")

    forced: SourceType | None = None
    prefix, separator, remainder = spec.partition(":")
    if separator and prefix.lower() in TYPE_PREFIXES and not remainder.startswith("//"):
        forced = TYPE_PREFIXES[prefix.lower()]
        spec = remainder or spec
    elif separator and prefix.lower() not in TYPE_PREFIXES and "//" not in remainder:
        # `notion:workspace`-style plugin target; a bare Windows drive
        # letter (`C:\…`) is one character and never a plugin name.
        if len(prefix) > 1 and re.fullmatch(r"[a-z][a-z0-9_-]*", prefix.lower()):
            local_candidate = Path(spec).expanduser()
            if not local_candidate.exists():
                return _plugin_target(prefix.lower(), remainder, workspace, name=name)

    if spec.startswith("s3://") or forced is SourceType.s3:
        return _s3_target(spec, workspace, name=name)
    if forced is SourceType.repository and "://" in spec or is_git_url(spec):
        return _git_target(spec, clone_root, name=name)
    parsed = urlparse(spec)
    if parsed.scheme in ("http", "https"):
        if forced is SourceType.api:
            target = _web_target(spec, workspace, name=name)
            return ResolvedTarget(
                name=target.name,
                type=SourceType.api.value,
                path=target.path,
                description=f"API collection at {spec}",
                urls=[spec],
                local=False,
            )
        return _web_target(spec, workspace, name=name)

    return _local_target(Path(spec), name=name, forced=forced)


def resolve_targets(
    specs: list[str],
    *,
    clone_root: Path,
    workspace: Path,
    split: bool = False,
    name: str | None = None,
) -> list[ResolvedTarget]:
    """Resolve every spec, de-duplicating the source names they produce."""
    expanded = expand_specs(specs, split=split)
    single = len(expanded) == 1
    targets: list[ResolvedTarget] = []
    used: set[str] = set()
    for spec in expanded:
        target = resolve_target(
            spec,
            clone_root=clone_root,
            workspace=workspace,
            name=name if single else None,
        )
        base = target.name
        suffix = 2
        while target.name in used:
            target.name = f"{base}-{suffix}"
            suffix += 1
        used.add(target.name)
        targets.append(target)
    return targets


def managed_target(source: SourceConfig) -> ResolvedTarget | None:
    """Rehydrate the clone recipe persisted under ``sources[].repo``."""

    repo = getattr(source, "repo", None)
    clone_url = str(getattr(repo, "clone_url", "") or "").strip()
    if not clone_url:
        return None
    path = str(source.path)
    return ResolvedTarget(
        name=str(source.name),
        type=SourceType.repository.value,
        path=path,
        description=str(getattr(source, "description", "") or ""),
        clone_url=clone_url,
        clone_path=str(getattr(repo, "clone_path", "") or path),
        clone_ref=str(getattr(repo, "clone_ref", "") or "") or None,
    )


def _run_git(destination: Path, args: list[str], clone_url: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git", "-C", str(destination), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            env=_git_env(clone_url),
        )
    except subprocess.TimeoutExpired as exc:
        raise TargetError(
            f"git {' '.join(args[:2])} timed out after {GIT_COMMAND_TIMEOUT_SECONDS}s "
            f"for {destination}"
        ) from exc
    if (
        result.returncode != 0
        and _is_github_https_url(clone_url)
        and _github_token()
        and _github_authentication_error(result)
    ):
        try:
            anonymous_result = subprocess.run(
                ["git", "-C", str(destination), *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=GIT_COMMAND_TIMEOUT_SECONDS,
                # No URL means _git_env does not inject the configured token.
                env=_git_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise TargetError(
                f"anonymous git {' '.join(args[:2])} timed out after "
                f"{GIT_COMMAND_TIMEOUT_SECONDS}s for {destination}"
            ) from exc
        if anonymous_result.returncode == 0:
            return anonymous_result
        raise TargetError(
            f"git {' '.join(args[:2])} failed for {destination}: GitHub rejected the configured "
            "GITHUB_TOKEN/GH_TOKEN and anonymous access was also denied; update the token "
            "or use an SSH remote for a private repository"
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git command failed").strip()
        raise TargetError(f"git {' '.join(args[:2])} failed for {destination}: {detail}")
    return result


def _git_output(destination: Path, args: list[str], clone_url: str = "") -> str:
    return _run_git(destination, args, clone_url).stdout.strip()


def _try_git_output(destination: Path, args: list[str]) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(destination), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        env=_git_env(),
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _tracking_ref(destination: Path, requested_ref: str | None) -> str:
    upstream = _try_git_output(destination, ["rev-parse", "--symbolic-full-name", "@{upstream}"])
    if upstream:
        return upstream
    candidates: list[str] = []
    if requested_ref:
        candidates.extend([f"refs/remotes/origin/{requested_ref}", f"refs/tags/{requested_ref}"])
    origin_head = _try_git_output(destination, ["symbolic-ref", "refs/remotes/origin/HEAD"])
    if origin_head:
        candidates.append(origin_head)
    for candidate in candidates:
        if _try_git_output(destination, ["rev-parse", "--verify", candidate]):
            return candidate
    raise TargetError(
        f"managed repository at {destination} has no upstream tracking ref; "
        "configure the branch upstream or add it again with a GitHub tree URL"
    )


def _canonical_remote(url: str) -> str:
    return url.strip().rstrip("/").removesuffix(".git")


def _fast_forward_target(target: ResolvedTarget, destination: Path, clone_url: str) -> None:
    origin = _git_output(destination, ["remote", "get-url", "origin"], clone_url)
    if _canonical_remote(origin) != _canonical_remote(clone_url):
        raise TargetError(
            f"managed repository {target.name!r} points at {origin!r}, not its configured "
            f"remote {clone_url!r}; remove/re-add the source instead of indexing the wrong clone"
        )
    dirty = _git_output(destination, ["status", "--porcelain"], clone_url)
    if dirty:
        raise TargetError(
            f"managed repository {target.name!r} has local working-tree changes; "
            "commit/stash them or use a separate local repository source"
        )
    _run_git(destination, ["fetch", "--prune", "origin"], clone_url)
    remote_ref = _tracking_ref(destination, target.clone_ref)
    local_commit = _git_output(destination, ["rev-parse", "HEAD"], clone_url)
    remote_commit = _git_output(destination, ["rev-parse", remote_ref], clone_url)
    if local_commit != remote_commit:
        ancestor = subprocess.run(
            [
                "git",
                "-C",
                str(destination),
                "merge-base",
                "--is-ancestor",
                local_commit,
                remote_commit,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            env=_git_env(clone_url),
        )
        if ancestor.returncode != 0:
            raise TargetError(
                f"managed repository {target.name!r} is ahead of or diverged from {remote_ref}; "
                "Pheasant will not discard local commits"
            )
        _run_git(destination, ["merge", "--ff-only", "--quiet", remote_commit], clone_url)
    updated = _git_output(destination, ["rev-parse", "HEAD"], clone_url)
    if updated != remote_commit:
        raise TargetError(
            f"managed repository {target.name!r} did not reach {remote_ref} ({remote_commit})"
        )


def managed_repository_state(target: ResolvedTarget) -> dict[str, object]:
    """Return local/remote commit evidence after a managed update."""

    destination = Path(target.clone_path or target.path)
    local_commit = _git_output(destination, ["rev-parse", "HEAD"], target.clone_url or "")
    remote_ref = _tracking_ref(destination, target.clone_ref)
    remote_commit = _git_output(destination, ["rev-parse", remote_ref], target.clone_url or "")
    branch = _git_output(destination, ["rev-parse", "--abbrev-ref", "HEAD"], target.clone_url or "")
    return {
        "managed": True,
        "remote_url": target.clone_url,
        "requested_ref": target.clone_ref,
        "tracking_ref": remote_ref,
        "branch": branch,
        "local_commit": local_commit,
        "remote_commit": remote_commit,
        "fresh": local_commit == remote_commit,
    }


def refresh_managed_repository(source: SourceConfig) -> dict[str, object] | None:
    """Materialize/update a URL-backed source and return freshness evidence."""

    target = managed_target(source)
    if target is None:
        return None
    fetch_target(target)
    state = managed_repository_state(target)
    if not state["fresh"]:  # defensive: _fast_forward_target already enforces this
        raise TargetError(
            f"managed repository {target.name!r} is not at its fetched remote revision"
        )
    return state


def fetch_target(target: ResolvedTarget) -> str | None:
    """Materialize a remote target locally. Returns a status line, or None.

    Idempotent: an existing clean clone is fetched and fast-forwarded rather
    than re-cloned. Dirty, ahead, and divergent checkouts fail visibly rather
    than being indexed as though they represented the remote.
    """
    if not target.clone_url:
        return None
    # Re-check at the boundary: a ResolvedTarget can also be built by hand or
    # rehydrated from config, so the validation must not live only in the
    # classifier that usually produces one.
    clone_url = validate_clone_url(target.clone_url)
    if shutil.which("git") is None:
        raise TargetError("git is required to clone a remote repository but was not found on PATH")
    destination = Path(target.clone_path or target.path)
    if (destination / ".git").is_dir():
        _fast_forward_target(target, destination, clone_url)
        source_path = Path(target.path)
        if not source_path.exists():
            raise TargetError(
                f"GitHub repository path does not exist at ref {target.clone_ref!r}: {source_path}"
            )
        return f"{target.name}: reusing existing clone at {source_path}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "git",
        "clone",
        "--quiet",
        # Knowledge indexing needs the checked-out tree, not the repository's
        # entire history. A depth-one clone preserves tracked commit evidence
        # while avoiding huge history packs.
        "--depth",
        "1",
        "--no-tags",
        *(["--branch", target.clone_ref, "--single-branch"] if target.clone_ref else []),
        "--",
        clone_url,
        str(destination),
    ]
    try:
        result = subprocess.run(
            # `--` ends option parsing, so nothing after it can be read as a flag.
            command,
            capture_output=True,
            text=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            env=_git_env(clone_url),
        )
    except subprocess.TimeoutExpired as exc:
        raise TargetError(
            f"git clone timed out after {GIT_COMMAND_TIMEOUT_SECONDS}s for {target.name!r}"
        ) from exc
    if (
        result.returncode != 0
        and _is_github_https_url(clone_url)
        and _github_token()
        and _github_authentication_error(result)
        # Git normally removes a failed clone directory. Do not remove an
        # unexpected path just to attempt the anonymous fallback.
        and not destination.exists()
    ):
        try:
            anonymous_result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=GIT_COMMAND_TIMEOUT_SECONDS,
                # No URL means _git_env does not inject the configured token.
                env=_git_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise TargetError(
                "anonymous git clone timed out after "
                f"{GIT_COMMAND_TIMEOUT_SECONDS}s for {target.name!r}"
            ) from exc
        if anonymous_result.returncode == 0:
            result = anonymous_result
        else:
            raise TargetError(
                f"could not clone {target.clone_url}: GitHub rejected the configured "
                "GITHUB_TOKEN/GH_TOKEN and anonymous access was also denied; update the token "
                "or use an SSH remote for a private repository"
            )
    if result.returncode != 0:
        raise TargetError(
            f"could not clone {target.clone_url}: {result.stderr.strip() or 'git clone failed'}"
        )
    source_path = Path(target.path)
    if not source_path.exists():
        raise TargetError(
            f"GitHub repository path does not exist at ref {target.clone_ref!r}: {source_path}"
        )
    return f"{target.name}: cloned {target.clone_url} → {source_path}"

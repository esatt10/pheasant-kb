from __future__ import annotations

import argparse
import json
import re
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)
STABLE_SEMVER_TAG_RE = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)$"
)


@dataclass(frozen=True)
class Replacement:
    path: Path
    pattern: re.Pattern[str]
    replacement: str
    label: str
    #: How many matches the pattern must have. 0 means "every match, and at
    #: least one" — a file that names the image once per service (the compose
    #: fleet names it three times) would otherwise be half-rewritten, which is
    #: worse than not rewriting it at all.
    occurrences: int = 1


def project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(data["project"]["version"])
    validate_semver(version)
    return version


def validate_semver(version: str) -> None:
    if not SEMVER_RE.fullmatch(version):
        raise SystemExit(f"pyproject.toml project.version is not valid semver: {version}")


def stable_semver_tuple(version: str) -> tuple[int, int, int]:
    match = STABLE_SEMVER_TAG_RE.fullmatch(version)
    if not match or version.startswith("v"):
        raise SystemExit(
            "Published container versions must use stable MAJOR.MINOR.PATCH semver "
            f"from pyproject.toml; got {version}"
        )
    return tuple(int(match.group(part)) for part in ("major", "minor", "patch"))


def parse_stable_semver_tag(tag: str) -> tuple[int, int, int] | None:
    match = STABLE_SEMVER_TAG_RE.fullmatch(tag)
    if not match:
        return None
    return tuple(int(match.group(part)) for part in ("major", "minor", "patch"))


def bump(version: str, part: str) -> str:
    match = SEMVER_RE.fullmatch(version)
    if not match or "-" in version or "+" in version:
        raise SystemExit(
            "Automatic major/minor/patch bumps require a stable MAJOR.MINOR.PATCH version"
        )
    major = int(match.group("major"))
    minor = int(match.group("minor"))
    patch = int(match.group("patch"))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise SystemExit(f"Unknown bump part: {part}")


def apply_replacement(text: str, replacement: Replacement) -> tuple[str, bool]:
    updated, count = replacement.pattern.subn(
        replacement.replacement, text, count=replacement.occurrences
    )
    if count == 0:
        raise SystemExit(f"Could not find {replacement.label} in {replacement.path}")
    if replacement.occurrences and count != replacement.occurrences:
        raise SystemExit(
            f"Expected {replacement.occurrences} occurrence(s) of {replacement.label} "
            f"in {replacement.path}, found {count}"
        )
    return updated, updated != text


def set_pyproject_version(version: str) -> None:
    validate_semver(version)
    path = ROOT / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    updated, changed = apply_replacement(
        text,
        Replacement(
            path=path.relative_to(ROOT),
            pattern=re.compile(r'(?m)^(version\s*=\s*)"[^"]+"'),
            replacement=rf'\g<1>"{version}"',
            label="project.version",
        ),
    )
    if changed:
        path.write_text(updated, encoding="utf-8")


def replacements(version: str) -> list[Replacement]:
    image = f"ghcr.io/esatt10/pheasant:{version}"
    return [
        Replacement(
            path=Path("deploy/helm/Chart.yaml"),
            pattern=re.compile(r"(?m)^(version:\s*)[^\r\n]+"),
            replacement=rf"\g<1>{version}",
            label="Helm chart version",
        ),
        Replacement(
            path=Path("deploy/helm/Chart.yaml"),
            pattern=re.compile(r'(?m)^(appVersion:\s*)"[^\r\n"]+"'),
            replacement=rf'\g<1>"{version}"',
            label="Helm appVersion",
        ),
        Replacement(
            path=Path("deploy/helm/values.yaml"),
            pattern=re.compile(
                r"(?ms)^(image:\r?\n(?:[ \t]+[^\r\n]*\r?\n)*?[ \t]+tag:\s*)[^\r\n#]+"
            ),
            replacement=rf"\g<1>{version}",
            label="Helm image tag",
        ),
        Replacement(
            path=Path("deploy/kubernetes/deployment.yaml"),
            pattern=re.compile(r"ghcr\.io/esatt10/pheasant:[0-9A-Za-z._+-]+"),
            replacement=image,
            label="Kubernetes deployment image",
        ),
        Replacement(
            path=Path("deploy/compose/docker-compose.yml"),
            pattern=re.compile(r"ghcr\.io/esatt10/pheasant:[0-9A-Za-z._+-]+"),
            replacement=image,
            label="Docker Compose default image",
        ),
        Replacement(
            path=Path("pheasant.example.yaml"),
            pattern=re.compile(
                r"(?ms)^(deployment:\r?\n(?:[ \t]+[^\r\n]*\r?\n)*?[ \t]+image_tag:\s*)[^\r\n#]+"
            ),
            replacement=rf"\g<1>{version}",
            label="example compose image tag",
        ),
        # Everything below names a published image in a file someone actually
        # runs. They were unmanaged until the fresh Compose file was found
        # three releases behind on 0.9.0 — a stale default tag is not a typo,
        # it silently starts an old build that has none of the fixes the
        # checkout it came with advertises.
        *_image_reference_replacements(
            version,
            {
                Path("deploy/compose/docker-compose.fresh.yml"): 1,
                Path("deploy/compose/docker-compose.advanced.yml"): 1,
                Path("deploy/compose/docker-compose.scale.yml"): 7,
                Path("deploy/compose/docker-compose.pheasant-lab.yml"): 7,
                Path("deploy/kubernetes/scaled/api-deployment.yaml"): 1,
                Path("deploy/kubernetes/scaled/graph-deployment.yaml"): 1,
                Path("deploy/kubernetes/scaled/indexer-statefulset.yaml"): 1,
                Path("deploy/kubernetes/scaled/observability/logger-deployment.yaml"): 1,
                Path("deploy/kubernetes/scaled/worker-deployment.yaml"): 1,
                Path("deploy/kubernetes/scaled/exports-cronjob.yaml"): 1,
            },
        ),
        # Commented-out examples, but they are the values a user copies into a
        # real .env — and a copied 0.8.0 pins them to 0.8.0.
        Replacement(
            path=Path("deploy/compose/.env.example"),
            pattern=re.compile(r"ghcr\.io/esatt10/pheasant:[0-9A-Za-z._+-]+"),
            replacement=image,
            label="example .env image",
        ),
        # The UI image, which this script did not rewrite for three releases
        # while `tests/test_version_alignment.py` scanned for it — the checker
        # and the writer disagreeing about scope, which is the same drift the
        # file-list derivation above was introduced to end. It went unnoticed
        # because the two are keyed differently: the writer matches a
        # *pattern* per file, the "does the writer cover the scan" test matches
        # *paths*, so a file listed here with a pattern that misses half its
        # references looks covered to both.
        Replacement(
            path=Path("deploy/compose/.env.example"),
            pattern=re.compile(r"ghcr\.io/esatt10/pheasant-ui:[0-9A-Za-z._+-]+"),
            replacement=f"ghcr.io/esatt10/pheasant-ui:{version}",
            label="example .env UI image",
        ),
    ]


def _image_reference_replacements(version: str, paths: dict[Path, int]) -> list[Replacement]:
    """One ``ghcr.io/esatt10/pheasant:<tag>`` rewrite per file.

    The expected occurrence count is spelled out per file rather than inferred:
    adding a service to the compose fleet without pinning its image would
    otherwise pass silently, and an unpinned service is the one that drifts.
    """
    return [
        Replacement(
            path=path,
            pattern=re.compile(r"ghcr\.io/esatt10/pheasant:[0-9A-Za-z._+-]+"),
            replacement=f"ghcr.io/esatt10/pheasant:{version}",
            label="published image reference",
            occurrences=count,
        )
        for path, count in paths.items()
    ]


def managed_paths() -> list[str]:
    """Every file a release rewrites, pyproject.toml included.

    The publish workflow stages exactly this list. It used to hand-maintain
    its own copy, which is the standing trap: a file added here but not there
    is rewritten on the release runner, never committed, and fails the next
    `--check` on main.
    """
    paths = {"pyproject.toml"} | {str(item.path) for item in replacements(project_version())}
    return sorted(paths)


def sync_generated_versions(version: str, write: bool) -> list[str]:
    validate_semver(version)
    mismatches: list[str] = []
    writes: dict[Path, str] = {}
    for replacement in replacements(version):
        path = ROOT / replacement.path
        original = writes.get(path, path.read_text(encoding="utf-8"))
        updated, changed = apply_replacement(original, replacement)
        writes[path] = updated
        if changed:
            mismatches.append(str(replacement.path))

    if write:
        for path, text in writes.items():
            if path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
        return []
    return sorted(set(mismatches))


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        url_part, _, rel_part = part.partition(";")
        if 'rel="next"' in rel_part:
            return url_part.strip()[1:-1]
    return None


def _github_api_pages(url: str, token: str | None) -> list[dict]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    results: list[dict] = []
    next_url: str | None = url
    while next_url:
        request = Request(next_url, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                page = json.loads(response.read().decode("utf-8"))
                if not isinstance(page, list):
                    raise SystemExit(f"GitHub API returned an unexpected response for {next_url}")
                results.extend(page)
                next_url = _next_link(response.headers.get("Link"))
        except HTTPError:
            raise
        except URLError as exc:
            raise SystemExit(f"Could not query GitHub Packages: {exc}") from exc
    return results


def fetch_container_versions(
    owner: str,
    package_name: str,
    token: str | None,
    api_url: str,
) -> list[dict]:
    encoded_owner = quote(owner, safe="")
    encoded_package = quote(package_name, safe="")
    base = api_url.rstrip("/")
    errors: list[str] = []
    auth_errors: list[str] = []

    for owner_scope in ("orgs", "users"):
        url = (
            f"{base}/{owner_scope}/{encoded_owner}/packages/container/"
            f"{encoded_package}/versions?per_page=100"
        )
        try:
            return _github_api_pages(url, token)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 404:
                errors.append(f"{owner_scope}: 404")
                continue
            if exc.code in {401, 403}:
                auth_errors.append(f"{owner_scope}: HTTP {exc.code} {detail}")
                continue
            raise SystemExit(
                f"GitHub Packages query failed for {owner_scope}/{owner}: HTTP {exc.code} {detail}"
            ) from exc

    if auth_errors:
        raise SystemExit(
            "GitHub Packages query requires a token with package read access:\n"
            + "\n".join(auth_errors)
        )

    print(
        f"No existing GHCR package found for {owner}/{package_name}; "
        f"treating {project_version()} as the first published version.",
        file=sys.stderr,
    )
    if errors:
        print("Checked package scopes: " + ", ".join(errors), file=sys.stderr)
    return []


def container_tags(package_versions: list[dict]) -> set[str]:
    tags: set[str] = set()
    for version in package_versions:
        metadata = version.get("metadata", {})
        container = metadata.get("container", {})
        for tag in container.get("tags", []) or []:
            tags.add(str(tag))
    return tags


#: Tags every published package is expected to carry besides its version.
#: `latest` is what the untagged `docker run ghcr.io/esatt10/pheasant` in the
#: README resolves to, so its absence is a broken front door, not a detail.
REQUIRED_FLOATING_TAGS = ("latest",)


def check_image_version_published(
    version: str,
    existing_tags: set[str],
    package: str = "pheasant",
    floating: tuple[str, ...] = REQUIRED_FLOATING_TAGS,
) -> None:
    """Assert the registry really holds what the release is about to record.

    The inverse of :func:`check_image_version_increment`, and the reason the
    release commit now happens last: main pins this version into every compose
    file and manifest, so recording it before the push has landed is how a
    checkout ends up referencing an image that does not exist.
    """
    missing = [tag for tag in (version, *floating) if tag not in existing_tags]
    if missing:
        raise SystemExit(
            f"{package} is missing published tag(s) {missing} after the push; "
            f"refusing to record {version} in main. Found: {sorted(existing_tags)[:10]}"
        )
    print(f"Verified published: {package}:{version} (and {', '.join(floating)}).")


def check_image_version_increment(version: str, existing_tags: set[str]) -> None:
    current = stable_semver_tuple(version)
    if version in existing_tags or f"v{version}" in existing_tags:
        raise SystemExit(f"Container image tag already exists for version {version}")

    semver_tags = {
        tag: parsed for tag in existing_tags if (parsed := parse_stable_semver_tag(tag)) is not None
    }
    if not semver_tags:
        print(f"No prior stable semver image tags found; accepting {version}.")
        return

    max_tag, max_version = max(semver_tags.items(), key=lambda item: item[1])
    if current <= max_version:
        raise SystemExit(
            f"pyproject.toml version {version} must be greater than the highest "
            f"published image semver tag {max_tag}"
        )

    print(f"Release image version accepted: {version} > {max_tag}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Keep generated pheasant version references aligned."
    )
    parser.add_argument(
        "--print", dest="print_version", action="store_true", help="Print pyproject version."
    )
    parser.add_argument("--check", action="store_true", help="Check generated version references.")
    parser.add_argument(
        "--list-paths",
        dest="list_paths",
        action="store_true",
        help="Print every file this script rewrites, one per line.",
    )
    parser.add_argument(
        "--write", action="store_true", help="Refresh generated version references."
    )
    parser.add_argument(
        "--bump",
        choices=("major", "minor", "patch"),
        help="Bump pyproject version and refresh generated references.",
    )
    parser.add_argument(
        "--set", dest="set_version", help="Set pyproject version and refresh generated references."
    )
    parser.add_argument(
        "--check-ghcr-tags",
        action="store_true",
        help=(
            "Require pyproject version to be a new image tag greater than existing "
            "GHCR semver tags."
        ),
    )
    parser.add_argument(
        "--require-ghcr-tags",
        action="store_true",
        help="Require the version (and latest) to already be published for --package.",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=6,
        help="Attempts for --require-ghcr-tags; the packages API can lag a push.",
    )
    parser.add_argument("--owner", help="GitHub owner that owns the container package.")
    parser.add_argument("--package", default="pheasant", help="GitHub container package name.")
    parser.add_argument("--token", help="GitHub token for package tag lookup.")
    parser.add_argument("--api-url", default="https://api.github.com", help="GitHub API base URL.")
    args = parser.parse_args(argv)

    if args.list_paths:
        for managed in managed_paths():
            print(managed)
        return 0

    version = project_version()
    if args.bump and args.set_version:
        raise SystemExit("Use either --bump or --set, not both")
    if args.bump:
        version = bump(version, args.bump)
        set_pyproject_version(version)
    if args.set_version:
        validate_semver(args.set_version)
        version = args.set_version
        set_pyproject_version(version)

    if args.print_version:
        print(version)

    should_write = args.write or bool(args.bump or args.set_version)
    mismatches = sync_generated_versions(version, write=should_write)
    if mismatches:
        print("Version references are not aligned with pyproject.toml:", file=sys.stderr)
        for path in mismatches:
            print(f"  - {path}", file=sys.stderr)
        print("Run: python scripts/sync_version.py --write", file=sys.stderr)
        return 1

    if args.check_ghcr_tags:
        if not args.owner:
            raise SystemExit("--owner is required with --check-ghcr-tags")
        versions = fetch_container_versions(args.owner, args.package, args.token, args.api_url)
        check_image_version_increment(version, container_tags(versions))

    if args.require_ghcr_tags:
        if not args.owner:
            raise SystemExit("--owner is required with --require-ghcr-tags")
        # The packages API can trail a push by a few seconds, and a release
        # that fails here has already published — so retry before concluding
        # the push did not land.
        for attempt in range(1, max(1, args.attempts) + 1):
            tags = container_tags(
                fetch_container_versions(args.owner, args.package, args.token, args.api_url)
            )
            if version in tags and all(tag in tags for tag in REQUIRED_FLOATING_TAGS):
                break
            if attempt < max(1, args.attempts):
                print(
                    f"{args.package}:{version} not visible yet (attempt {attempt}); retrying.",
                    file=sys.stderr,
                )
                time.sleep(5)
        check_image_version_published(version, tags, package=args.package)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

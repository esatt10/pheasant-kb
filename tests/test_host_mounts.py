"""Making an arbitrary host directory reachable from inside a container.

Acceptance:

1. The container's mount table is parsed correctly, including the octal
   escaping the kernel applies to paths with spaces.
2. A host path that IS mounted resolves to the container path it landed on,
   with the longest matching mount winning.
3. A host path that is NOT mounted returns a remedy — a compose volume, a
   ``docker run`` flag, and the allow-list entry it also needs — rather than a
   bare "does not exist".
4. ``pheasant mount`` merges into an existing override file instead of
   replacing it, and is idempotent.
5. Outside a container, none of this gets in the way.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.deployment.mounts import (
    Mount,
    host_to_container,
    in_container,
    mount_suggestion,
    read_mount_table,
    render_compose_override,
    resolve_host_path,
    suggested_container_path,
)

MOUNTINFO = """\
25 30 0:22 / /proc rw,nosuid,relatime shared:5 - proc proc rw
30 24 8:1 / / rw,relatime - overlay overlay rw
101 30 259:1 /Users/me/notes /workspace ro,relatime - ext4 /dev/nvme0n1p1 ro
102 30 259:1 /Users/me /data rw,relatime - ext4 /dev/nvme0n1p1 rw
103 30 259:1 /srv/my\\040docs /mnt/docs rw,relatime - ext4 /dev/nvme0n1p1 rw
"""


@pytest.fixture()
def mountinfo(tmp_path: Path) -> Path:
    path = tmp_path / "mountinfo"
    path.write_text(MOUNTINFO, encoding="utf-8")
    return path


# ------------------------------------------------------------------ parsing


def test_mount_table_is_parsed(mountinfo: Path) -> None:
    mounts = read_mount_table(mountinfo)
    points = {mount.mount_point: mount for mount in mounts}
    assert points["/workspace"].source == "/Users/me/notes"
    assert points["/workspace"].read_only is True
    assert points["/data"].read_only is False
    assert points["/data"].fstype == "ext4"


def test_octal_escapes_in_paths_are_decoded(mountinfo: Path) -> None:
    """The kernel escapes space/tab/newline/backslash; a raw compare fails."""
    mounts = read_mount_table(mountinfo)
    docs = next(m for m in mounts if m.mount_point == "/mnt/docs")
    assert docs.source == "/srv/my docs"


def test_a_missing_mount_table_is_not_fatal(tmp_path: Path) -> None:
    assert read_mount_table(tmp_path / "nope") == []


# ------------------------------------------------------------------ mapping


def test_a_mounted_host_path_resolves_to_its_container_path(mountinfo: Path) -> None:
    mounts = read_mount_table(mountinfo)
    assert host_to_container("/Users/me/notes", mounts) == "/workspace"
    assert host_to_container("/Users/me/notes/inbox/a.md", mounts) == "/workspace/inbox/a.md"


def test_the_longest_matching_mount_wins(mountinfo: Path) -> None:
    """/Users/me is mounted at /data and /Users/me/notes at /workspace.

    A path under notes must resolve through /workspace, not through the
    shallower mount that also technically contains it.
    """
    mounts = read_mount_table(mountinfo)
    assert host_to_container("/Users/me/notes/x.md", mounts) == "/workspace/x.md"
    assert host_to_container("/Users/me/other/y.md", mounts) == "/data/other/y.md"


def test_the_containers_own_root_filesystem_is_not_treated_as_the_hosts(
    mountinfo: Path,
) -> None:
    """`/` here is an overlay — the container's root, not the host's."""
    mounts = read_mount_table(mountinfo)
    assert host_to_container("/etc/passwd", mounts) is None


def test_an_unmounted_host_path_has_no_container_path(mountinfo: Path) -> None:
    mounts = read_mount_table(mountinfo)
    assert host_to_container("/Volumes/backup/archive", mounts) is None


@pytest.mark.parametrize("separator", ["/", "\\"])
@pytest.mark.parametrize(
    "vm_source",
    [
        # Docker Desktop has surfaced a Windows drive under all of these,
        # depending on version — and under the drive-less POSIX form when the
        # bind is declared that way.
        "/c/work/docs",
        "/host_mnt/c/work/docs",
        "/run/desktop/mnt/host/c/work/docs",
        "/work/docs",
    ],
)
def test_a_windows_path_matches_however_docker_exposed_the_drive(
    vm_source: str, separator: str
) -> None:
    """Picking one VM prefix works on some machines and fails on the rest.

    Failing here means telling a Windows user that their mounted directory is
    not mounted, so every plausible form is tried.
    """
    typed = f"C:{separator}work{separator}docs"
    mounts = [Mount(mount_point="/data/docs", source=vm_source, fstype="ext4", read_only=True)]
    assert host_to_container(typed, mounts) == "/data/docs"


def test_a_windows_subpath_keeps_its_tail(mountinfo: Path) -> None:
    mounts = [Mount(mount_point="/data/docs", source="/c/work/docs", fstype="ext4", read_only=True)]
    assert host_to_container("C:\\work\\docs\\spec.md", mounts) == "/data/docs/spec.md"


# ------------------------------------------------------------------ remedies


def test_a_remedy_names_the_mount_and_the_allowlist_entry() -> None:
    remedy = mount_suggestion("/Volumes/backup/archive")
    assert remedy["container_path"] == "/data/archive"
    assert remedy["compose_volume"] == "/Volumes/backup/archive:/data/archive:ro"
    assert "-v" in remedy["docker_run_flag"]
    # A mount alone is a half-fix: the path becomes visible and the API still
    # refuses to register a source under it.
    assert remedy["config_patch"]["security"]["allow_workspace_roots"] == ["/data/archive"]
    assert remedy["cli"].startswith("pheasant mount ")


def test_suggested_container_path_uses_the_directory_name() -> None:
    assert suggested_container_path("/Users/me/client-notes") == "/data/client-notes"
    assert suggested_container_path("C:/work/docs") == "/data/docs"


def test_resolve_reports_native_outside_a_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PHEASANT_IN_CONTAINER", "0")
    report = resolve_host_path(str(tmp_path))
    assert report["status"] == "native"
    assert report["exists"] is True
    assert report["remedy"] is None


def test_resolve_reports_visible_for_a_mounted_path(
    mountinfo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PHEASANT_IN_CONTAINER", "1")
    report = resolve_host_path("/Users/me/notes", read_mount_table(mountinfo))
    assert report["status"] == "visible"
    assert report["container_path"] == "/workspace"


def test_resolve_reports_not_mounted_with_a_remedy(
    mountinfo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PHEASANT_IN_CONTAINER", "1")
    report = resolve_host_path("/Volumes/backup", read_mount_table(mountinfo))
    assert report["status"] == "not_mounted"
    assert report["remedy"]["container_path"] == "/data/backup"


def test_an_unreadable_mount_table_reports_unknown_not_not_mounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claiming "not mounted" when we cannot tell sends the user to fix
    something that may not be broken."""
    monkeypatch.setenv("PHEASANT_IN_CONTAINER", "1")
    report = resolve_host_path("/Users/me/notes", [])
    assert report["status"] == "unknown"


def test_in_container_honours_the_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHEASANT_IN_CONTAINER", "yes")
    assert in_container() is True
    monkeypatch.setenv("PHEASANT_IN_CONTAINER", "no")
    assert in_container() is False


# ------------------------------------------------------- compose override


def test_override_is_generated_for_a_new_file() -> None:
    text = render_compose_override({"/Users/me/notes": "/data/notes"})
    data = yaml.safe_load(text)
    assert data["services"]["pheasant"]["volumes"] == ["/Users/me/notes:/data/notes:ro"]


def test_override_merges_into_an_existing_file_and_keeps_other_edits() -> None:
    existing = yaml.safe_dump(
        {
            "services": {
                "pheasant": {
                    "volumes": ["/a:/data/a:ro"],
                    "environment": {"KEEP": "me"},
                }
            }
        }
    )
    text = render_compose_override({"/b": "/data/b"}, existing=existing)
    data = yaml.safe_load(text)
    entry = data["services"]["pheasant"]
    assert entry["volumes"] == ["/a:/data/a:ro", "/b:/data/b:ro"]
    # An override file is where people keep their own edits.
    assert entry["environment"] == {"KEEP": "me"}


def test_mounting_the_same_target_twice_updates_rather_than_appends() -> None:
    first = render_compose_override({"/a": "/data/notes"})
    second = render_compose_override({"/moved": "/data/notes"}, existing=first)
    volumes = yaml.safe_load(second)["services"]["pheasant"]["volumes"]
    assert volumes == ["/moved:/data/notes:ro"]


def test_a_malformed_override_is_rejected_rather_than_silently_replaced() -> None:
    with pytest.raises(ValueError):
        render_compose_override({"/a": "/data/a"}, existing="services: not-a-mapping\n")


# ------------------------------------------------------------------ the CLI


def test_host_path_route_reports_a_native_path(loaded_config, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PHEASANT_IN_CONTAINER", "0")
    client = TestClient(create_app(config=loaded_config))
    response = client.get("/fs/host-path", params={"path": str(tmp_path)})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "native"
    assert body["in_container"] is False


def test_host_path_route_returns_a_remedy_for_an_unmounted_path(
    loaded_config, monkeypatch, mountinfo: Path
) -> None:
    monkeypatch.setenv("PHEASANT_IN_CONTAINER", "1")
    monkeypatch.setattr("pheasant.deployment.mounts.MOUNTINFO_PATH", str(mountinfo))
    client = TestClient(create_app(config=loaded_config))
    response = client.get("/fs/host-path", params={"path": "/Volumes/backup"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_mounted"
    assert body["remedy"]["compose_volume"] == "/Volumes/backup:/data/backup:ro"

"""The entrypoint sizes BLAS thread pools to the container's CPU quota, and only then.

Runs the shell function itself, lifted out of ``docker-entrypoint.sh``, against
a fake cgroup tree -- both v2 (``cpu.max``) and v1 (``cpu.cfs_*``) spellings,
since a host decides which one a container sees.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


def _function() -> str:
    match = re.search(r"^cpu_quota_threads\(\) \{\n.*?^\}\n", ENTRYPOINT, re.M | re.S)
    assert match, "docker-entrypoint.sh no longer defines cpu_quota_threads"
    return match.group(0)


def _threads(root: Path) -> str | None:
    script = _function() + "cpu_quota_threads || echo none\n"
    out = subprocess.run(
        ["sh", "-c", "set -eu\n" + script],
        env={"PHEASANT_CGROUP_ROOT": str(root), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return None if out == "none" else out


def _cores() -> int:
    return int(subprocess.run(["nproc"], capture_output=True, text=True).stdout.strip() or 1)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("100000 100000\n", "1"),  # compose `cpus: 1.0`
        ("150000 100000\n", "2"),  # `cpus: 1.5` rounds up
        ("50000 100000\n", "1"),  # a fraction is still one thread
        ("max 100000\n", None),  # no quota: leave numpy's own choice alone
    ],
)
def test_cgroup_v2(tmp_path: Path, content: str, expected: str | None) -> None:
    (tmp_path / "cpu.max").write_text(content)
    if expected is not None:
        expected = str(min(int(expected), _cores()))
    assert _threads(tmp_path) == expected


@pytest.mark.parametrize(("quota", "expected"), [("100000", "1"), ("-1", None)])
def test_cgroup_v1(tmp_path: Path, quota: str, expected: str | None) -> None:
    (tmp_path / "cpu").mkdir()
    (tmp_path / "cpu" / "cpu.cfs_quota_us").write_text(quota + "\n")
    (tmp_path / "cpu" / "cpu.cfs_period_us").write_text("100000\n")
    assert _threads(tmp_path) == expected


def test_never_above_the_core_count(tmp_path: Path) -> None:
    (tmp_path / "cpu.max").write_text("6400000 100000\n")
    assert _threads(tmp_path) == str(_cores())


def test_no_cgroup_files_means_no_change(tmp_path: Path) -> None:
    assert _threads(tmp_path) is None


def test_an_operator_setting_wins() -> None:
    # `: "${VAR:=value}"` assigns only when unset or empty.
    assert ': "${OPENBLAS_NUM_THREADS:=$blas_threads}"' in ENTRYPOINT
    assert '"${LANCE_CPU_THREADS:=$blas_threads}"' in ENTRYPOINT
    assert "export OPENBLAS_NUM_THREADS OMP_NUM_THREADS MKL_NUM_THREADS LANCE_CPU_THREADS" in (
        ENTRYPOINT
    )

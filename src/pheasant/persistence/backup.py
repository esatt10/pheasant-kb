"""State backup + restore (Synapse step 21.6, session A).

A backup is a zstd-compressed tar of the *durable* region state:

- ``pheasant.db`` — a **consistent** SQLite snapshot taken via ``VACUUM INTO``
  a temp file (never a raw copy of the live db, which can tear under WAL).
  All manifests live in this db (Synapse 21.2), so they ride along for free.
- ``graphs/`` — ``graph.latest.json`` + every ``graph.<ts>.json.zst`` snapshot.
- ``contract.latest.json`` — the published semantic contract, when present.
- ``events/`` — the append-only NDJSON sync-event stream, when present.
- ``vectors/`` — the per-region vector index (Synapse 21.4), when present.

``/state`` is user data: restore is **safe by default** — it refuses to clobber
a non-empty target unless ``force=True``, and it never partially destroys the
target. It unpacks into a sibling temp dir, validates, then atomically swaps the
old directory aside (renamed to ``*.replaced-<ts>``, never deleted) before moving
the restored tree into place.
"""

from __future__ import annotations

import os
import sqlite3
import tarfile
import tempfile
from pathlib import Path

#: `pheasant backup` archives a SQLite database and the state directory. On
#: Postgres it used to write that same archive with no database in it and
#: report success: a backup that cannot restore the index, discovered only
#: when someone needs it. The database and /state belong in one backup taken
#: together, which is what the pheasant-kb recipe does with pg_dump.
POSTGRES_BACKUP_REFUSAL = (
    "ERROR: pheasant cannot {action} this region: its database is Postgres, and "
    "`pheasant backup`/`restore` cover SQLite state only.\n"
    "pheasant-kb: docker compose run --rm backup   (restore: docker compose run --rm "
    "restore <name>). See docs/pheasant-kb/operate.md#back-up-and-restore.\n"
    "Elsewhere: pg_dump the database and archive the state directory together."
)

# Members of the state dir that a backup captures (relative to the state root).
DB_MEMBER = "pheasant.db"
DIR_MEMBERS = ("graphs", "events", "vectors", "manifests")
FILE_MEMBERS = ("contract.latest.json",)


def _vacuum_into(sqlite_path: Path, dest: Path) -> None:
    """Write a consistent snapshot of the live db to ``dest`` via VACUUM INTO."""

    if dest.exists():
        dest.unlink()
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()


def create_backup(
    state_path: str | Path,
    out_path: str | Path,
    sqlite_path: str | Path | None = None,
) -> Path:
    """Create a ``.tar.zst`` backup of the durable state at ``out_path``.

    The original state directory is read-only here — nothing under
    ``state_path`` is modified.
    """

    import zstandard as zstd

    state = Path(state_path)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    db_src = Path(sqlite_path) if sqlite_path is not None else state / DB_MEMBER

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db_snapshot = tmp / DB_MEMBER
        if db_src.exists():
            _vacuum_into(db_src, db_snapshot)

        tar_path = tmp / "backup.tar"
        with tarfile.open(tar_path, "w") as tar:
            if db_snapshot.exists():
                tar.add(db_snapshot, arcname=DB_MEMBER)
            for name in DIR_MEMBERS:
                member = state / name
                if member.exists():
                    tar.add(member, arcname=name)
            for name in FILE_MEMBERS:
                member = state / name
                if member.exists():
                    tar.add(member, arcname=name)

        compressor = zstd.ZstdCompressor(level=10)
        tmp_out = out.with_suffix(out.suffix + ".tmp")
        with tar_path.open("rb") as src, tmp_out.open("wb") as dst:
            compressor.copy_stream(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp_out, out)
    return out


def _is_within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract guarding against path traversal / absolute members.

    The name check alone is not sufficient: a member named ``a`` that is a
    *symlink* to ``/etc`` passes it (``dest/a`` is lexically inside ``dest``),
    and a later member ``a/b`` then writes through the link. Reject link
    members whose target escapes, and hand extraction the stdlib ``data``
    filter, which additionally strips absolute/traversing links, device nodes
    and setuid bits. (``data`` is the default from Python 3.14; asking for it
    explicitly makes the behavior the same on 3.11–3.13.)
    """

    for member in tar.getmembers():
        member_path = dest / member.name
        if member.name.startswith("/") or not _is_within(dest, member_path):
            raise ValueError(f"Unsafe path in backup archive: {member.name}")
        if (member.issym() or member.islnk()) and not _is_within(
            dest, member_path.parent / member.linkname
        ):
            raise ValueError(f"Unsafe link in backup archive: {member.name} -> {member.linkname}")
    tar.extractall(dest, filter="data")  # noqa: S202 - members validated above


def restore_backup(
    in_path: str | Path,
    state_path: str | Path,
    *,
    force: bool = False,
    now: str | None = None,
) -> Path:
    """Restore a ``.tar.zst`` backup into ``state_path``.

    Safe by default: refuses a non-empty target unless ``force=True`` and never
    leaves the target partially destroyed. Decompression + extraction happen in
    a sibling temp dir; only after a successful, validated extraction is the
    existing target moved aside (``*.replaced-<ts>``, preserved) and the
    restored tree swapped in.
    """

    import zstandard as zstd

    src = Path(in_path)
    target = Path(state_path)
    if not src.exists():
        raise FileNotFoundError(f"Backup archive not found: {src}")

    target_nonempty = target.exists() and any(target.iterdir())
    if target_nonempty and not force:
        raise FileExistsError(
            f"Refusing to restore into non-empty state dir {target} without --force"
        )

    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".pheasant-restore-", dir=parent))
    try:
        # Decompress the .tar.zst into a temp tar, then extract into staging.
        with tempfile.TemporaryDirectory(dir=parent) as tmpdir:
            tar_path = Path(tmpdir) / "restore.tar"
            decompressor = zstd.ZstdDecompressor()
            with src.open("rb") as fh, tar_path.open("wb") as out:
                decompressor.copy_stream(fh, out)
            with tarfile.open(tar_path, "r") as tar:
                _safe_extract(tar, staging)
        # Validate the restored db before touching the live target.
        db_path = staging / DB_MEMBER
        if db_path.exists():
            _integrity_check(db_path)
    except Exception:
        _rmtree(staging)
        raise

    # Atomic-ish swap: move the existing target aside (preserved), then move the
    # staged tree into place. Never delete the user's original.
    if target.exists():
        from pheasant.ingestion.pipeline import utc_now

        stamp = (now or utc_now()).replace(":", "-")
        replaced = parent / f"{target.name}.replaced-{stamp}"
        os.replace(target, replaced)
    os.replace(staging, target)
    return target


def _integrity_check(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    if not row or row[0] != "ok":
        raise ValueError(f"Restored database failed integrity_check: {row}")


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)

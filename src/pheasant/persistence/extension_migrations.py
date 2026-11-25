"""Apply installed extensions' independent storage revisions under the schema lock."""

from datetime import UTC, datetime

from pheasant.extensions import installed_extensions


def migrate_extensions(state) -> None:
    for extension in installed_extensions():
        if not extension.migrations:
            continue
        key = f"extension:{extension.id}"
        rows = state.rows("SELECT value FROM pheasant_schema_meta WHERE key=?", (key,))
        applied = int(rows[0]["value"]) if rows else 0
        if not 0 <= applied <= len(extension.migrations):
            raise RuntimeError(f"Unsupported storage revision {applied} for {extension.id}")
        for revision, migration in enumerate(extension.migrations[applied:], start=applied + 1):
            with state.conn:
                for statement in migration:
                    state.conn.execute(statement)
                state.conn.execute(
                    "INSERT INTO pheasant_schema_meta(key, value, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (key, str(revision), datetime.now(UTC).isoformat()),
                )

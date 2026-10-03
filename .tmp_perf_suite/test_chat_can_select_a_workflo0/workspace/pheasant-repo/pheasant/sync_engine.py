"""Sample sync engine fixture used by acceptance tests."""


class SyncEngine:
    """Coordinates idempotent indexing for configured sources."""

    def sync_source(self, source_name: str, mode: str = "incremental") -> dict:
        return {"source": source_name, "mode": mode, "status": "synced"}

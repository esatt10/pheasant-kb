from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pheasant.config.schema import PheasantConfig


@dataclass(frozen=True)
class StatePaths:
    state: Path
    graphs: Path
    manifests: Path
    snapshots: Path
    locks: Path
    logs: Path
    cache: Path
    exports: Path

    @classmethod
    def from_config(cls, config: PheasantConfig) -> StatePaths:
        state = config.pheasant.state_path
        return cls(
            state=state,
            graphs=config.storage.graph_path or state / "graphs",
            manifests=config.storage.manifest_path or state / "manifests",
            snapshots=state / "snapshots",
            locks=state / "locks",
            logs=state / "logs",
            cache=state / "cache",
            exports=config.pheasant.exports_path,
        )

    def ensure(self) -> None:
        for path in [
            self.state,
            self.graphs,
            self.manifests,
            self.snapshots,
            self.locks,
            self.logs,
            self.cache,
            self.exports,
        ]:
            path.mkdir(parents=True, exist_ok=True)

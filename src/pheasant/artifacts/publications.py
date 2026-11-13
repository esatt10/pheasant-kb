"""Immutable publication domain facade; persistence is shared with migrations."""

from pheasant.persistence.artifact_publications import (
    deprecate,
    keep_version,
    publish,
    status,
    version,
)

__all__ = ["deprecate", "keep_version", "publish", "status", "version"]

"""Shared constants and exceptions for the osmface assembler."""
from __future__ import annotations

OUTER = "outer"
INNER = "inner"
ROLES = (OUTER, INNER)

# per-relation result statuses
COMPLETE = "complete"
INCOMPLETE = "incomplete"
TOPOLOGY_CONFLICT = "topology_conflict"
STATUSES = (COMPLETE, INCOMPLETE, TOPOLOGY_CONFLICT)

# staging states (meta table, input side)
STAGING_LOADING = "loading"
STAGING_LOADED = "loaded"
STAGING_FAILED = "failed"

# package states (manifest, output side)
PKG_BUILDING = "building"
PKG_COMPLETE = "complete"
PKG_FAILED = "failed"

# cap on the number of detail entries embedded in a single reason
MAX_REPORT = 20


def flip_role(role: str) -> str:
    """Nested-role composition: an inner reference flips the roles below it."""
    if role == OUTER:
        return INNER
    if role == INNER:
        return OUTER
    raise ValueError(f"unknown role: {role!r}")


class OsmFaceError(Exception):
    """Base class for expected, user-facing failures."""


class LoadError(OsmFaceError):
    """Unrecoverable load problem (I/O, pre-existing database)."""


class StagingStateError(OsmFaceError):
    """Raised when assemble is requested on a non-loaded staging database."""


class ExportError(OsmFaceError):
    """Raised when the output package cannot be written safely."""


class TopologyConflict(OsmFaceError):
    """A geometric/topological conflict isolated to one relation.

    Carries a stable machine-readable ``code`` plus a JSON-serializable
    ``detail`` mapping.  Never silently pick a branch: anything ambiguous
    raises this and the whole relation is isolated as topology_conflict.
    """

    def __init__(self, code: str, detail: dict | None = None):
        self.code = code
        self.detail = detail or {}
        super().__init__(f"{code}: {self.detail}")

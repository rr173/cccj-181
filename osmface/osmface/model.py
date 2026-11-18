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

# historical result state for a relation that does not exist at a revision
ABSENT = "absent"

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


class HistoryError(OsmFaceError):
    """Base class for the revision/change subsystem."""


class NotHistoryError(HistoryError):
    """The database has not been enabled for history yet."""


class AlreadyHistoryError(HistoryError):
    """History is already enabled (idempotent enable still succeeds)."""


class BatchError(HistoryError):
    """A change batch is malformed or semantically invalid (no revision used)."""


class RevisionConflict(HistoryError):
    """base_revision is stale, or a concurrent writer committed first."""


class BatchIdConflict(HistoryError):
    """A batch_id was already received with different input bytes."""


class BatchInProgress(HistoryError):
    """A writer holds the apply lock; retry rather than partially commit."""


class WorkcopyError(OsmFaceError):
    """Base class for offline work-copy failures (checkout/edit/preview)."""


class WorkcopyConflict(WorkcopyError):
    """The edit file's base_edit is stale or the copy is otherwise unusable."""


class PackageError(OsmFaceError):
    """A transport package is missing pieces, corrupt, or mis-typed."""


class MergeError(OsmFaceError):
    """Base class for merge plan/ruling/confirmation failures."""


class MergeConflict(MergeError):
    """The warehouse head moved past the plan's pinned head, or the merge
    collides with a different already-received merge identity."""


class SyncPoint:
    """Deterministic test hook for crash/interleave acceptance tests.

    When an environment variable (default ``OSMFACE_SYNC_DIR``) points at a
    directory, the apply pipeline writes a marker file at a named phase and
    blocks until a ``proceed`` marker appears.  A ``crash`` marker makes the
    process exit hard instead, simulating termination at exactly that phase.
    No random sleeps are involved.
    """

    def __init__(self, directory=None, env_var="OSMFACE_SYNC_DIR"):
        import os

        self.directory = directory if directory is not None else os.environ.get(env_var)
        self.env_var = env_var

    def _path(self, name):
        import os

        return os.path.join(self.directory, name)

    def reach(self, phase: str) -> None:
        if not self.directory:
            return
        import os
        import time

        os.makedirs(self.directory, exist_ok=True)
        crash_marker = self._path(f"crash-{phase}")
        wait_marker = self._path(f"wait-{phase}")
        proceed_marker = self._path(f"proceed-{phase}")
        # announce that this phase has been reached; flush so another process
        # observing the directory never sees a stale/empty marker
        with open(self._path(f"reached-{phase}"), "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
            fh.flush()
            os.fsync(fh.fileno())
        # blocking is opt-in per phase: a test plants a crash or wait marker;
        # ordinary runs never stop here, so no random sleeps are needed
        if not os.path.exists(crash_marker) and not os.path.exists(wait_marker):
            return
        if os.path.exists(crash_marker):
            # deterministic simulated termination at this phase
            os._exit(17)
        deadline = time.monotonic() + float(
            os.environ.get("OSMFACE_SYNC_TIMEOUT", "120")
        )
        while not os.path.exists(proceed_marker):
            if os.path.exists(crash_marker):
                os._exit(17)
            if time.monotonic() > deadline:
                raise HistoryError(f"sync point {phase!r} timed out")
            time.sleep(0.01)


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

"""osmface — offline OSM-style relation-to-polygon assembler."""
from .api import (
    apply_changes,
    assemble_database,
    enable_history,
    export_revision_package,
    get_receipt,
    inspect_database,
    inspect_package,
    load_database,
)
from .model import (
    ABSENT,
    COMPLETE,
    INCOMPLETE,
    TOPOLOGY_CONFLICT,
    BatchError,
    BatchIdConflict,
    HistoryError,
    NotHistoryError,
    OsmFaceError,
    RevisionConflict,
    SyncPoint,
)

__all__ = [
    "load_database",
    "assemble_database",
    "inspect_database",
    "inspect_package",
    # revisioned warehouse
    "enable_history",
    "apply_changes",
    "get_receipt",
    "export_revision_package",
    "SyncPoint",
    # statuses / errors
    "COMPLETE",
    "INCOMPLETE",
    "TOPOLOGY_CONFLICT",
    "ABSENT",
    "OsmFaceError",
    "HistoryError",
    "NotHistoryError",
    "BatchError",
    "BatchIdConflict",
    "RevisionConflict",
]
__version__ = "2.0.0"

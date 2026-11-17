"""osmface — offline OSM-style relation-to-polygon assembler."""
from .api import (
    assemble_database,
    inspect_database,
    inspect_package,
    load_database,
)
from .model import (
    COMPLETE,
    INCOMPLETE,
    TOPOLOGY_CONFLICT,
    OsmFaceError,
)

__all__ = [
    "load_database",
    "assemble_database",
    "inspect_database",
    "inspect_package",
    "COMPLETE",
    "INCOMPLETE",
    "TOPOLOGY_CONFLICT",
    "OsmFaceError",
]
__version__ = "1.0.0"

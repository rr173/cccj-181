"""Persistent spatial candidate index for historical face geometry.

The index is a *candidate* index only.  Every query is a two-stage process:

1. a SQLite R-tree (``face_rtree``) returns the stored face bounding boxes
   overlapping the query window, plus a small bypass table for boxes that
   cannot be represented safely in an R-tree;
2. the Shapely geometry actually persisted at the queried revision
   (``face_history`` / ``rel_result_history``) performs the exact
   ``covers`` / ``intersects`` decision.

A bounding-box hit is never reported as a face hit: holes and concave gaps
are eliminated exclusively by the second stage.

Why a bypass is needed
----------------------
SQLite stores R-tree coordinates as 32-bit integers, i.e. with float32
precision.  The R-tree write path rounds *outward* (the stored box always
contains the true box) inside the finite float32 range, so ordinary
coordinates cannot lose a real hit to inward rounding.  Two cases remain
that no R-tree row can represent safely:

* a bound whose magnitude exceeds the finite float32 range
  (|x| > 3.4028234663852886e38): SQLite stores ``inf`` and the row becomes
  unmatchable by finite query windows;
* NaN bounds (defensive: assembly only emits finite coordinates).

Such face boxes are recorded in ``face_rtree_bypass`` with their true
float64 bounds and are returned as candidates on every query of the same
revision.  The bypass is expected to stay empty on real data; its existence
keeps the no-false-negative guarantee unconditional.  Every bypass
insertion is counted in ``spatial_index_meta.bypass_rows`` per revision, so
the condition is auditable rather than silent.

Revision consistency
--------------------
The index is maintained inside the same write transaction as the revision
it describes (revision 0 at ``enable_history`` time, then every apply and
merge-confirm commit).  A revision's head marker is only advanced after its
index rows have been written, so readers can never observe a head revision
whose index is missing or stale; any index write failure rolls the whole
revision back.  Old revisions keep their own index rows forever.
"""
from __future__ import annotations

import json
import math
import sqlite3

from .model import COMPLETE, OsmFaceError

# inclusive magnitude limit of coordinates safely representable in the
# R-tree's float32 storage
_F32_MAX = 3.4028234663852886e38

SPATIAL_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS face_rtree
    USING rtree(id, minx, maxx, miny, maxy);
CREATE TABLE IF NOT EXISTS face_rtree_rows(
    rowid       INTEGER PRIMARY KEY,
    revision    INTEGER NOT NULL,
    relation_id INTEGER NOT NULL,
    face_index  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_face_rtree_rows_rev
    ON face_rtree_rows(revision);
CREATE TABLE IF NOT EXISTS face_rtree_bypass(
    id          INTEGER PRIMARY KEY,
    revision    INTEGER NOT NULL,
    relation_id INTEGER NOT NULL,
    face_index  INTEGER NOT NULL,
    minx        REAL NOT NULL,
    maxx        REAL NOT NULL,
    miny        REAL NOT NULL,
    maxy        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_face_bypass_rev
    ON face_rtree_bypass(revision);
CREATE TABLE IF NOT EXISTS spatial_index_meta(
    revision     INTEGER PRIMARY KEY,
    rtree_rows   INTEGER NOT NULL,
    bypass_rows  INTEGER NOT NULL,
    built_at     TEXT NOT NULL
);
"""

SPATIAL_TABLES = (
    "face_rtree",
    "face_rtree_rows",
    "face_rtree_bypass",
    "spatial_index_meta",
)


class SpatialQueryError(OsmFaceError):
    """A spatial query parameter is malformed or the index is inconsistent."""


# ---------------------------------------------------------------------------
# bounding-box safety and helpers
# ---------------------------------------------------------------------------

def _is_finite(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def box_is_indexable(minx, miny, maxx, maxy) -> bool:
    """True when all four bounds fit the R-tree's finite float32 range.

    The R-tree rounds outward inside this range, so an indexable box can
    never suffer inward rounding.  Anything else must use the bypass.
    """
    for v in (minx, miny, maxx, maxy):
        if not math.isfinite(v) or abs(v) > _F32_MAX:
            return False
    return True


def _face_bounds(geojson: str):
    """Bounds of one persisted face (a GeoJSON Polygon mapping)."""
    geom = json.loads(geojson)
    if geom.get("type") != "Polygon":
        # face rows are always individual Polygons; guard the contract
        raise SpatialQueryError(
            f"face_history holds unexpected geometry type: {geom.get('type')!r}"
        )
    minx = miny = math.inf
    maxx = maxy = -math.inf
    for ring in geom["coordinates"]:
        for x, y in ring:
            if x < minx:
                minx = x
            if x > maxx:
                maxx = x
            if y < miny:
                miny = y
            if y > maxy:
                maxy = y
    return minx, miny, maxx, maxy


# ---------------------------------------------------------------------------
# index maintenance (always called inside the revision write transaction)
# ---------------------------------------------------------------------------

def ensure_spatial_schema(conn) -> None:
    conn.executescript(SPATIAL_SCHEMA)


def index_revision(conn, revision: int) -> dict:
    """Build the spatial index rows for one freshly written revision.

    The index must cover *every* complete face effective at the revision.
    Relations recomputed by this revision carry fresh ``face_history`` rows
    for ``revision``; untouched relations keep their older face rows, so
    their index entries are carried forward from the previous revision's
    index and then the recomputed entries are replaced wholesale.

    Relations that are absent, incomplete, in topology conflict or simply
    have no faces at this revision contribute nothing (their carried
    entries are replaced by nothing), so they can never be returned as
    complete-face hits.  Returns ``{"rtree_rows", "bypass_rows"}``.
    """
    from . import db as dbmod

    ensure_spatial_schema(conn)
    rtree_boxes = []
    rtree_meta = []
    bypass_rows = []

    # 1. carry forward entries for relations not recomputed at this revision
    if revision > 0:
        carried = conn.execute(
            "SELECT m.relation_id, m.face_index, r.minx, r.maxx,"
            " r.miny, r.maxy"
            " FROM face_rtree_rows m JOIN face_rtree r ON r.id = m.rowid"
            " WHERE m.revision = ? AND m.relation_id NOT IN ("
            "   SELECT relation_id FROM rel_result_history WHERE revision = ?)",
            (revision - 1, revision),
        ).fetchall()
        for row in carried:
            rtree_boxes.append((row["minx"], row["maxx"],
                                row["miny"], row["maxy"]))
            rtree_meta.append((revision, row["relation_id"], row["face_index"]))
        conn.execute(
            "INSERT INTO face_rtree_bypass(revision, relation_id, face_index,"
            " minx, maxx, miny, maxy)"
            " SELECT ?, relation_id, face_index, minx, maxx, miny, maxy"
            " FROM face_rtree_bypass"
            " WHERE revision = ? AND relation_id NOT IN ("
            "   SELECT relation_id FROM rel_result_history WHERE revision = ?)",
            (revision, revision - 1, revision),
        )

    # 2. index the freshly persisted faces of every relation recomputed here
    face_rows = conn.execute(
        "SELECT f.relation_id, f.face_index, f.geojson, r.status"
        " FROM face_history f"
        " JOIN rel_result_history r"
        "   ON r.revision = f.revision AND r.relation_id = f.relation_id"
        " WHERE f.revision = ?"
        " ORDER BY f.relation_id, f.face_index",
        (revision,),
    ).fetchall()
    for row in face_rows:
        # only complete relations own queryable faces; the join keeps this
        # honest even if a future writer ever stores faces for another state
        if row["status"] != COMPLETE:
            continue
        minx, miny, maxx, maxy = _face_bounds(row["geojson"])
        if box_is_indexable(minx, miny, maxx, maxy):
            rtree_boxes.append((minx, maxx, miny, maxy))
            rtree_meta.append((revision, row["relation_id"], row["face_index"]))
        else:
            bypass_rows.append(
                (revision, row["relation_id"], row["face_index"],
                 minx, maxx, miny, maxy)
            )
    # paired rowids: the R-tree virtual table only accepts (id, bounds), so
    # revision/relation/face identity lives in the companion table and joins
    # on the same integer rowid
    if rtree_boxes:
        first_id = (
            conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM face_rtree")
            .fetchone()[0]
        )
        conn.executemany(
            "INSERT INTO face_rtree(id, minx, maxx, miny, maxy)"
            " VALUES(?,?,?,?,?)",
            [
                (first_id + i, bounds[0], bounds[1], bounds[2], bounds[3])
                for i, bounds in enumerate(rtree_boxes)
            ],
        )
        conn.executemany(
            "INSERT INTO face_rtree_rows(rowid, revision, relation_id, face_index)"
            " VALUES(?,?,?,?)",
            [
                (first_id + i, meta_rev, rid, face_index)
                for i, (meta_rev, rid, face_index) in enumerate(rtree_meta)
            ],
        )
    if bypass_rows:
        conn.executemany(
            "INSERT INTO face_rtree_bypass(revision, relation_id, face_index,"
            " minx, maxx, miny, maxy) VALUES(?,?,?,?,?,?,?)",
            bypass_rows,
        )
    total_bypass = conn.execute(
        "SELECT COUNT(*) FROM face_rtree_bypass WHERE revision = ?", (revision,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO spatial_index_meta(revision, rtree_rows, bypass_rows,"
        " built_at) VALUES(?,?,?,?)",
        (revision, len(rtree_boxes), total_bypass, dbmod.utcnow()),
    )
    return {"rtree_rows": len(rtree_boxes), "bypass_rows": total_bypass}


def index_version(conn, revision: int):
    """The persisted index descriptor for a revision, or None if absent."""
    return conn.execute(
        "SELECT revision, rtree_rows, bypass_rows, built_at"
        " FROM spatial_index_meta WHERE revision = ?",
        (revision,),
    ).fetchone()


# ---------------------------------------------------------------------------
# query parameter validation
# ---------------------------------------------------------------------------

def validate_point(x, y):
    if not (_is_finite(x) and _is_finite(y)):
        raise SpatialQueryError(
            f"point coordinates must be finite numbers, got x={x!r}, y={y!r}"
        )
    return float(x), float(y)


def validate_bounds(minx, miny, maxx, maxy):
    vals = (minx, miny, maxx, maxy)
    if not all(_is_finite(v) for v in vals):
        raise SpatialQueryError(
            "range bounds must be finite numbers, got "
            f"minx={minx!r}, miny={miny!r}, maxx={maxx!r}, maxy={maxy!r}"
        )
    minx, miny, maxx, maxy = (float(v) for v in vals)
    # zero width / zero height ranges are explicitly allowed
    if minx > maxx or miny > maxy:
        raise SpatialQueryError(
            "range minima must not exceed maxima: "
            f"minx={minx}, miny={miny}, maxx={maxx}, maxy={maxy}"
        )
    return minx, miny, maxx, maxy


# ---------------------------------------------------------------------------
# candidate retrieval (stage 1)
# ---------------------------------------------------------------------------

def candidate_faces(conn, revision: int, minx, miny, maxx, maxy):
    """Distinct stored (relation_id, face_index) candidates for one window.

    Combines the R-tree overlap set with the revision's bypass set,
    de-duplicated.  Bypass boxes are re-checked with their true float64
    bounds here so the bypass itself never introduces false positives beyond
    "candidate" status; the exact Shapely stage remains authoritative.
    """
    rows = conn.execute(
        "SELECT m.relation_id, m.face_index"
        " FROM face_rtree r JOIN face_rtree_rows m ON m.rowid = r.id"
        " WHERE m.revision = ?"
        " AND r.minx <= ? AND r.maxx >= ?"
        " AND r.miny <= ? AND r.maxy >= ?",
        (revision, maxx, minx, maxy, miny),
    ).fetchall()
    candidates = {(r["relation_id"], r["face_index"]) for r in rows}
    rtree_count = len(candidates)
    bypass = conn.execute(
        "SELECT relation_id, face_index, minx, maxx, miny, maxy"
        " FROM face_rtree_bypass WHERE revision = ?",
        (revision,),
    ).fetchall()
    bypass_count = 0
    for r in bypass:
        if (
            r["minx"] <= maxx
            and r["maxx"] >= minx
            and r["miny"] <= maxy
            and r["maxy"] >= miny
        ):
            bypass_count += 1
            candidates.add((r["relation_id"], r["face_index"]))
    return candidates, {
        "rtree_candidates": rtree_count,
        "bypass_candidates": bypass_count,
        "bypass_indexed": len(bypass),
        "candidate_faces": len(candidates),
    }


# ---------------------------------------------------------------------------
# exact geometry loading (stage 2)
# ---------------------------------------------------------------------------

def load_candidate_geometries(conn, revision: int, candidates):
    """Load the persisted Shapely geometry for candidate faces only.

    Returns ``{relation_id: {face_index: shapely geometry}}``.  Geometry is
    taken from the same revision as the index candidates, never from the
    current head.
    """
    from shapely.geometry import shape

    out: dict = {}
    for rid, face_index in sorted(candidates):
        row = conn.execute(
            "SELECT geojson FROM face_history"
            " WHERE revision = ? AND relation_id = ? AND face_index = ?",
            (revision, rid, face_index),
        ).fetchone()
        if row is None:
            # index and geometry are written in one transaction; a missing
            # row signals corruption rather than an empty result
            raise SpatialQueryError(
                f"spatial index references missing face revision={revision}"
                f" relation={rid} face_index={face_index}; database may be"
                " corrupt"
            )
        out.setdefault(rid, {})[face_index] = shape(json.loads(row["geojson"]))
    return out


# ---------------------------------------------------------------------------
# snapshot management and revision pinning
# ---------------------------------------------------------------------------

class _ReadSnapshot:
    """Hold one SQLite read snapshot for the duration of a query.

    History tables are append-only and every read filters by the pinned
    revision, but taking an explicit deferred transaction makes the fixed
    snapshot guarantee observable: a writer committing mid-query cannot move
    any read of this query to a newer snapshot.
    """

    def __init__(self, conn):
        self.conn = conn
        self.opened = False

    def __enter__(self):
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN")
            self.opened = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.opened:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        return False


def _pin(conn, revision):
    # lazy import: history.py maintains this module, never the other way
    from . import history as hist

    hist._require_history(conn)
    pinned = hist._pin_revision(conn, revision)
    head = hist.head_revision(conn)
    meta = index_version(conn, pinned)
    if meta is None:
        raise SpatialQueryError(
            f"spatial index for revision {pinned} is missing; the warehouse"
            " is inconsistent (head revision, geometry and index must share"
            " one commit)"
        )
    return pinned, head


def _query_geometry(kind, params):
    from shapely.geometry import LineString, Point, box

    if kind == "point":
        x, y = validate_point(params["x"], params["y"])
        return Point(x, y), {"x": x, "y": y}
    minx, miny, maxx, maxy = validate_bounds(
        params["minx"], params["miny"], params["maxx"], params["maxy"]
    )
    if minx == maxx and miny == maxy:
        geom = Point(minx, miny)
    elif minx == maxx:
        geom = LineString([(minx, miny), (minx, maxy)])
    elif miny == maxy:
        geom = LineString([(minx, miny), (maxx, miny)])
    else:
        geom = box(minx, miny, maxx, maxy)
    return geom, {"minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy}


def _run_spatial_query(conn, kind, params, revision, predicate):
    # validate and canonicalize parameters before touching any table so a
    # malformed query never reaches SQLite as a confusing low-level error
    query_geom, clean_params = _query_geometry(kind, params)
    if kind == "point":
        minx = maxx = clean_params["x"]
        miny = maxy = clean_params["y"]
    else:
        minx, miny, maxx, maxy = (
            clean_params["minx"], clean_params["miny"],
            clean_params["maxx"], clean_params["maxy"],
        )

    pinned, head = _pin(conn, revision)
    with _ReadSnapshot(conn):
        candidates, cand_stats = candidate_faces(
            conn, pinned, minx, miny, maxx, maxy
        )
        candidate_relations = {rid for rid, _face in candidates}
        geometries = load_candidate_geometries(conn, pinned, candidates)
        hits: dict = {}
        exact_checks = 0
        for rid in sorted(geometries):
            for face_index in sorted(geometries[rid]):
                exact_checks += 1
                # the stored face is the covering/intersecting subject:
                # face.covers(point) / face.intersects(window); a point can
                # never cover a polygon, so the argument order matters
                if predicate(geometries[rid][face_index], query_geom):
                    hits.setdefault(rid, []).append(face_index)
    return {
        "query": kind,
        "revision": pinned,
        "head_revision": head,
        "params": clean_params,
        "index": {
            "candidate_faces": cand_stats["candidate_faces"],
            "candidate_relations": len(candidate_relations),
            "rtree_candidates": cand_stats["rtree_candidates"],
            "bypass_candidates": cand_stats["bypass_candidates"],
            "bypass_indexed": cand_stats["bypass_indexed"],
        },
        "exact_checks": exact_checks,
        "hit_count": len(hits),
        "hits": [
            {"relation_id": rid, "faces": hits[rid]} for rid in sorted(hits)
        ],
    }


def query_point(conn, x, y, revision=None) -> dict:
    """Which complete faces cover point (x, y) at one pinned revision.

    Boundary counts as covered; hole interiors do not.  A relation appears
    at most once, with every covering face index listed.
    """
    return _run_spatial_query(
        conn, "point", {"x": x, "y": y}, revision,
        lambda face_geom, query_geom: face_geom.covers(query_geom),
    )


def query_range(conn, minx, miny, maxx, maxy, revision=None) -> dict:
    """Which complete faces intersect the rectangle [minx,maxx]x[miny,maxy].

    Mere boundary contact counts as an intersection; zero-width or
    zero-height ranges are accepted.  A relation appears at most once, with
    every intersecting face index listed.
    """
    return _run_spatial_query(
        conn, "range",
        {"minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy},
        revision,
        lambda face_geom, query_geom: face_geom.intersects(query_geom),
    )


def spatial_index_state(conn) -> dict:
    """Per-revision index descriptors, for inspection and consistency tests."""
    rows = conn.execute(
        "SELECT revision, rtree_rows, bypass_rows, built_at"
        " FROM spatial_index_meta ORDER BY revision"
    ).fetchall()
    return {
        "revisions": [
            {"revision": r["revision"], "rtree_rows": r["rtree_rows"],
             "bypass_rows": r["bypass_rows"], "built_at": r["built_at"]}
            for r in rows
        ],
        "head_revision": conn.execute(
            "SELECT head_revision FROM rev_meta WHERE id = 1"
        ).fetchone()["head_revision"],
    }

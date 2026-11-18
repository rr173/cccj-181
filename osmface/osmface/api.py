"""Public Python API.

    import osmface
    osmface.load_database("staging.db", ["area.jsonl"], force=True)
    osmface.assemble_database("staging.db", out_dir="out/area")
    osmface.inspect_database("staging.db", relation_id=200)
    osmface.inspect_package("out/area")
"""
from __future__ import annotations

import json

from . import db as dbmod
from .assemble import (
    DEFAULT_MAX_BOUNDARY_NODES,
    DEFAULT_MAX_BOUNDARY_WAYS,
    assemble,
)
from .export import export_package, export_revision, read_manifest
from . import history as hist
from . import merge as mergemod
from . import spatial
from . import workcopy as wcmod
from .model import ABSENT, STATUSES


def load_database(db_path: str, files, force: bool = False) -> dict:
    """Load JSONL input files into a fresh staging database."""
    conn = dbmod.init_db(db_path, force=force)
    try:
        return dbmod.load_files(conn, list(files))
    finally:
        conn.close()


def assemble_database(
    db_path: str,
    out_dir: str | None = None,
    force: bool = False,
    max_boundary_ways: int = DEFAULT_MAX_BOUNDARY_WAYS,
    max_boundary_nodes: int = DEFAULT_MAX_BOUNDARY_NODES,
) -> dict:
    """Assemble all relations; optionally export a warehouse package."""
    conn = dbmod.connect(db_path)
    try:
        summary, results = assemble(conn, max_boundary_ways, max_boundary_nodes)
        if out_dir is not None:
            manifest = export_package(conn, results, out_dir, force=force)
            summary["package"] = {
                "path": out_dir,
                "state": manifest["state"],
                "counts": manifest["counts"],
            }
            dbmod.set_meta(conn, "last_output", summary["package"])
            conn.commit()
        return summary
    finally:
        conn.close()


def inspect_database(db_path: str, relation_id: int | None = None,
                     faults: bool = False, revision: int | None = None) -> dict:
    """Structured view over the staging database and assemble results.

    On a history-enabled database, ``revision`` pins the whole query to one
    revision: statuses, diagnostics, faces and provenance all come from that
    same revision.  When omitted, the head revision is fixed once at query
    start, so a concurrent commit can never mix old and new state.
    """
    conn = dbmod.connect(db_path)
    try:
        if hist.is_history_enabled(conn):
            info = _history_overview(conn, revision)
            if faults:
                info["faults"] = dbmod.fetch_faults(conn)
            if relation_id is not None:
                info["relation"] = hist.inspect_relation(
                    conn, info["revision"], relation_id
                )
            return info

        if revision is not None:
            raise hist.NotHistoryError(
                "database is not history-enabled; revision queries require"
                " enable_history() first"
            )
        info = {
            "staging_state": dbmod.get_meta(conn, "staging_state"),
            "counts": dbmod.get_meta(conn, "counts"),
            "merged_duplicates": dbmod.get_meta(conn, "merged_duplicates"),
            "fault_count": dbmod.get_meta(conn, "fault_count"),
            "sources": dbmod.get_meta(conn, "sources", []),
            "last_assemble": dbmod.get_meta(conn, "last_assemble"),
            "last_output": dbmod.get_meta(conn, "last_output"),
        }
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM relation_status GROUP BY status"
        ).fetchall()
        info["status_counts"] = {s: 0 for s in STATUSES}
        info["status_counts"].update({r["status"]: r["n"] for r in rows})
        if faults:
            info["faults"] = dbmod.fetch_faults(conn)
        if relation_id is not None:
            info["relation"] = _relation_detail(conn, relation_id)
        return info
    finally:
        conn.close()


def _history_overview(conn, revision):
    overview = hist.revision_overview(conn, revision)
    pinned = overview["revision"]
    info = {
        "staging_state": dbmod.get_meta(conn, "staging_state"),
        "history_enabled": True,
        "revision": pinned,
        "head_revision": overview["head_revision"],
        "revision_identity": hist.revision_identity(conn, pinned),
        "counts": overview["entity_counts"],
        "fault_count": dbmod.get_meta(conn, "fault_count"),
        "sources": dbmod.get_meta(conn, "sources", []),
        "status_counts": {
            s: overview["status_counts"].get(s, 0) for s in STATUSES
        },
        "absent_relations": overview["status_counts"].get(ABSENT, 0),
        "relations": overview["relations"],
        "batches": hist.list_batches(conn),
    }
    return info


def enable_history(db_path: str) -> dict:
    """Turn an existing loaded+assembled database into a revision 0 warehouse."""
    return hist.enable_history(db_path)


# ---------------------------------------------------------------------------
# spatial face queries (point coverage / rectangle intersection)
# ---------------------------------------------------------------------------

def query_faces_at_point(db_path: str, x, y, revision: int | None = None) -> dict:
    """Return the complete faces covering planar point (x, y).

    When ``revision`` is omitted, the head revision observed at query start
    is pinned for the whole query.  The spatial index only narrows the
    candidate set; coverage is decided exactly with the Shapely geometry
    stored at that revision (boundary counts as covered, hole interiors do
    not).  Each relation appears once with all covering face indices.
    """
    conn = hist.connect(db_path)
    try:
        return spatial.query_point(conn, x, y, revision=revision)
    finally:
        hist.close(conn)


def query_faces_in_range(db_path: str, minx, miny, maxx, maxy,
                         revision: int | None = None) -> dict:
    """Return the complete faces intersecting a planar rectangle.

    Coordinates use the same planar x/y system as the stored geometry, must
    all be finite and satisfy ``minx <= maxx`` / ``miny <= maxy`` (zero
    width or height allowed).  Boundary contact counts as an intersection.
    The head revision is pinned at query start when ``revision`` is omitted.
    """
    conn = hist.connect(db_path)
    try:
        return spatial.query_range(
            conn, minx, miny, maxx, maxy, revision=revision
        )
    finally:
        hist.close(conn)


def spatial_index_info(db_path: str) -> dict:
    """Per-revision spatial index build state (for inspection/audits)."""
    conn = hist.connect(db_path)
    try:
        hist._require_history(conn)
        return spatial.spatial_index_state(conn)
    finally:
        hist.close(conn)


def apply_changes(db_path: str, change_file: str,
                  sync: hist.SyncPoint | None = None) -> dict:
    """Atomically apply a UTF-8 JSONL change batch; return its receipt."""
    return hist.apply_changes(db_path, change_file, sync=sync)


def get_receipt(db_path: str, batch_id: str) -> dict:
    """Read the receipt of a previously committed batch."""
    conn = hist.connect(db_path)
    try:
        return hist.get_receipt(conn, batch_id)
    finally:
        hist.close(conn)


def export_revision_package(db_path: str, out_dir: str, revision: int | None = None,
                            force: bool = False) -> dict:
    """Export an immutable package bound to one revision."""
    conn = hist.connect(db_path)
    try:
        if revision is None:
            revision = hist.head_revision(conn)
        return export_revision(conn, out_dir, revision, force=force)
    finally:
        hist.close(conn)


# ---------------------------------------------------------------------------
# offline work copies and merge intake
# ---------------------------------------------------------------------------

def source_identity(db_path: str) -> dict:
    """The warehouse's stable source identity (minted once, then reused)."""
    conn = hist.connect(db_path)
    try:
        hist._require_history(conn)
        return {"source_id": hist.ensure_source_id(conn)}
    finally:
        hist.close(conn)


def checkout_workcopy(db_path: str, workcopy_path: str,
                      revision: int | None = None, force: bool = False) -> dict:
    """Create a self-contained offline work copy at a pinned revision."""
    return wcmod.checkout_workcopy(db_path, workcopy_path,
                                   revision=revision, force=force)


def workcopy_info(workcopy_path: str) -> dict:
    """Provenance and edit-head state of one work copy."""
    conn = wcmod.connect_workcopy(workcopy_path)
    try:
        return wcmod.workcopy_info(conn)
    finally:
        conn.close()


def workcopy_edit(workcopy_path: str, edit_file: str) -> dict:
    """Apply one offline edit batch to a work copy; return its receipt."""
    return wcmod.workcopy_edit(workcopy_path, edit_file)


def workcopy_preview(workcopy_path: str, relation_id=None) -> dict:
    """Preview the copy's current state with the ordinary assembly rules."""
    return wcmod.workcopy_preview(workcopy_path, relation_id=relation_id)


def workcopy_export(workcopy_path: str, package_dir: str,
                    force: bool = False) -> dict:
    """Export the fixed copy head as a portable transport package."""
    return wcmod.workcopy_export(workcopy_path, package_dir, force=force)


def create_merge_plan(db_path: str, package_dir: str) -> dict:
    """Validate a transport package and persist a merge plan."""
    return mergemod.create_merge_plan(db_path, package_dir)


def get_merge_plan(db_path: str, plan_id: str) -> dict:
    """The plan with current rulings, candidate states and diagnostics."""
    return mergemod.get_merge_plan(db_path, plan_id)


def list_merge_plans(db_path: str) -> list:
    return mergemod.list_merge_plans(db_path)


def save_merge_rulings(db_path: str, plan_id: str, rulings) -> dict:
    """Save conflict rulings / custom overrides onto an open plan."""
    return mergemod.save_merge_rulings(db_path, plan_id, rulings)


def merge_preview(db_path: str, plan_id: str) -> dict:
    """Candidate map preview from automatic results + current rulings."""
    return mergemod.merge_preview(db_path, plan_id)


def confirm_merge(db_path: str, plan_id: str, merge_id: str,
                  sync: hist.SyncPoint | None = None) -> dict:
    """Land the ruled plan as one warehouse revision; return the receipt."""
    return mergemod.confirm_merge(db_path, plan_id, merge_id, sync=sync)


def get_merge_receipt(db_path: str, merge_id: str) -> dict:
    """Read the receipt of a previously committed merge."""
    return mergemod.get_merge_receipt(db_path, merge_id)


def _relation_detail(conn, rid: int) -> dict | None:
    row = conn.execute(
        "SELECT status, reason FROM relation_status WHERE relation_id = ?", (rid,)
    ).fetchone()
    members = conn.execute(
        "SELECT members FROM relations WHERE id = ?", (rid,)
    ).fetchone()
    detail = {
        "relation": rid,
        "defined": members is not None,
        "members": json.loads(members["members"]) if members else None,
    }
    if row is None:
        detail["status"] = None
        return detail
    detail["status"] = row["status"]
    detail["reason"] = json.loads(row["reason"]) if row["reason"] else None
    faces = []
    for face in conn.execute(
        "SELECT id, face_index, geojson FROM faces WHERE relation_id = ?"
        " ORDER BY face_index",
        (rid,),
    ):
        rings = []
        for ring in conn.execute(
            "SELECT id, ring_index, role, is_hole, node_ids FROM rings"
            " WHERE face_id = ? ORDER BY ring_index",
            (face["id"],),
        ):
            segments = []
            for seg in conn.execute(
                "SELECT seq, way_id, path, reversed, node_ids FROM ring_segments"
                " WHERE ring_id = ? ORDER BY seq",
                (ring["id"],),
            ):
                way = conn.execute(
                    "SELECT nodes FROM ways WHERE id = ?", (seg["way_id"],)
                ).fetchone()
                segments.append(
                    {
                        "way": seg["way_id"],
                        "path": json.loads(seg["path"]),
                        "reversed": bool(seg["reversed"]),
                        "nodes": json.loads(seg["node_ids"]),
                        "way_definition_nodes": json.loads(way["nodes"]) if way else None,
                    }
                )
            rings.append(
                {
                    "role": ring["role"],
                    "hole": bool(ring["is_hole"]),
                    "nodes": json.loads(ring["node_ids"]),
                    "segments": segments,
                }
            )
        faces.append(
            {
                "face_index": face["face_index"],
                "geometry": json.loads(face["geojson"]),
                "rings": rings,
            }
        )
    detail["faces"] = faces
    return detail


def inspect_package(path: str) -> dict:
    """Read a package manifest and count relation statuses on disk."""
    manifest = read_manifest(path)
    info = {"manifest": manifest}
    rel_path = f"{path}/relations.jsonl"
    try:
        with open(rel_path, encoding="utf-8") as fh:
            info["relations_file_lines"] = sum(1 for line in fh if line.strip())
    except OSError:
        info["relations_file_lines"] = None
    return info

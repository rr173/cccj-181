#!/usr/bin/env python3
"""Independent acceptance for historical spatial face queries.

The reference implementation here must NOT call any production spatial
query or index-maintenance code: no ``osmface.spatial`` query functions,
no ``face_rtree`` access.  Instead it rebuilds expectations two ways:

1. pure coordinate arithmetic on the hand-written spatial world
   (point-in-rectangle / rectangle-overlap tests written from scratch);
2. Shapely predicates applied to the geometry persisted in
   ``face_history`` rows, loaded per relation through the ordinary
   ``inspect_database`` read path (which existed before this feature).

Both references are then compared against the public query API at every
revision, including a boundary move, a delete/incomplete/restore cycle and
an offline merge that adds a brand-new local face.

    python3 checks/check_spatial.py
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import osmface  # noqa: E402
from shapely.geometry import Point, box, shape  # noqa: E402
from history_util import (  # noqa: E402
    change_file,
    delete,
    node,
    put,
    rel,
    wm,
    write_jsonl,
)
from merge_util import edit_file, lm, lnode, lrel, lway  # noqa: E402
from fixtures_spatial_data import spatial_world_records  # noqa: E402

R, I, U, V, M = 400, 800, 600, 700, 900

# hand-written point matrix: (x, y) -> {relation: [face indices]}
POINT_MATRIX_REV0 = [
    (1, 1, {R: [0]}),
    (5, 5, {}),                       # hole interior, no relation covers it
    (8, 8, {I: [0]}),
    (45, 45, {U: [0]}),
    (30, 30, {}),
    (0, 5, {R: [0]}),
    (4, 5, {R: [0]}),
    (6, 8, {I: [0]}),
    (18, 18, {R: [0], V: [0]}),
    (0, 0, {R: [0]}),
    (20, 20, {R: [0], V: [0]}),
    (62, 62, {M: [0]}),
    (72, 72, {M: [1]}),
    (67, 67, {}),
]

RANGE_MATRIX_REV0 = [
    # minx, miny, maxx, maxy -> expected
    (4.5, 4.5, 5.5, 5.5, {}),       # wholly inside the hole, clear of island
    (2, 2, 5, 5, {R: [0]}),
    (7, 7, 9, 9, {I: [0]}),
    (5, 5, 6, 6, {I: [0]}),         # touches the island corner
    (20, -5, 20, 25, {R: [0], V: [0]}),
    (-5, 0, 25, 0, {R: [0]}),
    (1, 1, 1, 1, {R: [0]}),
    (42, 42, 48, 48, {U: [0]}),
    (-100, -100, 100, 100, {R: [0], I: [0], U: [0], V: [0], M: [0, 1]}),
]


def point_in_rect(px, py, minx, miny, maxx, maxy):
    """Plain-arithmetic reference: rectangle membership, boundary included."""
    return minx <= px <= maxx and miny <= py <= maxy


def rects_overlap(a, b):
    """Plain-arithmetic reference: closed rectangle intersection."""
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def stored_face_geometries(db, revision):
    """Reference geometry source: ordinary per-relation inspection only."""
    overview = osmface.inspect_database(db, revision=revision)
    out = {}
    for rid in overview["relations"]:
        detail = osmface.inspect_database(
            db, relation_id=rid, revision=revision)["relation"]
        if detail["status"] != "complete":
            continue
        out[rid] = [shape(f["geometry"]) for f in detail["faces"]]
    return out


def reference_point(db, revision, x, y):
    """Shapely covers() over stored faces, independent of the new index."""
    point = Point(x, y)
    hits = {}
    for rid, geoms in stored_face_geometries(db, revision).items():
        faces = [i for i, geom in enumerate(geoms) if geom.covers(point)]
        if faces:
            hits[rid] = faces
    return hits


def reference_range(db, revision, minx, miny, maxx, maxy):
    """Shapely intersects() over stored faces, independent of the new index."""
    window = box(minx, miny, maxx, maxy)
    hits = {}
    for rid, geoms in stored_face_geometries(db, revision).items():
        faces = [i for i, geom in enumerate(geoms) if geom.intersects(window)]
        if faces:
            hits[rid] = faces
    return hits


def api_hits(result):
    return {h["relation_id"]: h["faces"] for h in result["hits"]}


def check_rev0_hand_matrix(db):
    print("check 1: hand-written coordinate matrix at revision 0")
    # pure arithmetic sanity for one disjoint case (no production geometry)
    assert point_in_rect(45, 45, 40, 40, 50, 50)
    assert not point_in_rect(30, 30, 40, 40, 50, 50)
    assert rects_overlap((0, 0, 20, 20), (20, -5, 20, 25))
    assert not rects_overlap((40, 40, 50, 50), (0, 0, 10, 10))

    for x, y, expected in POINT_MATRIX_REV0:
        got = api_hits(osmface.query_faces_at_point(db, x, y, revision=0))
        ref = reference_point(db, 0, x, y)
        assert got == expected == ref, (x, y, got, expected, ref)
    for minx, miny, maxx, maxy, expected in RANGE_MATRIX_REV0:
        got = api_hits(osmface.query_faces_in_range(
            db, minx, miny, maxx, maxy, revision=0))
        ref = reference_range(db, 0, minx, miny, maxx, maxy)
        assert got == expected == ref, (minx, miny, maxx, maxy, got, expected, ref)
    print(f"  {len(POINT_MATRIX_REV0)} point cases, "
          f"{len(RANGE_MATRIX_REV0)} range cases match hand + Shapely references")


def check_boundary_move(db, tmp):
    print("check 2: boundary move freezes old revision and changes the new")
    osmface.apply_changes(db, change_file(
        tmp, "expand.jsonl", "expand", 0, [
            put(1, node(21, 30, 0)), put(1, node(22, 30, 20))]))
    probe = (25, 1)
    old = api_hits(osmface.query_faces_at_point(db, *probe, revision=0))
    new = api_hits(osmface.query_faces_at_point(db, *probe, revision=1))
    assert old == reference_point(db, 0, *probe) == {}
    assert new == reference_point(db, 1, *probe) == {R: [0]}
    # the old index rows survive untouched
    import sqlite3
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM face_rtree_rows WHERE revision=0").fetchone()[0] == 6
    assert conn.execute(
        "SELECT COUNT(*) FROM face_rtree_rows WHERE revision=1").fetchone()[0] == 6
    conn.close()
    print("  same point differs across revisions; rev0 index and hits frozen")


def check_delete_incomplete_restore(db, tmp):
    print("check 3: delete -> incomplete -> restore through apply")
    osmface.apply_changes(db, change_file(
        tmp, "del.jsonl", "del", 1,
        [delete(1, "relation", U)]))
    assert api_hits(osmface.query_faces_at_point(db, 45, 45, revision=2)) == {}
    osmface.apply_changes(db, change_file(
        tmp, "broken.jsonl", "broken", 2,
        [put(2, rel(U, [wm(999)]))]))
    assert api_hits(osmface.query_faces_at_point(db, 45, 45, revision=3)) == {}
    detail = osmface.inspect_database(db, relation_id=U, revision=3)["relation"]
    assert detail["status"] == "incomplete"
    osmface.apply_changes(db, change_file(
        tmp, "restore.jsonl", "restore", 3,
        [put(3, rel(U, [wm(23)]))]))
    got = api_hits(osmface.query_faces_at_point(db, 45, 45, revision=4))
    assert got == reference_point(db, 4, 45, 45) == {U: [0]}
    print("  deleted/incomplete relations never hit; restored face matches reference")


def check_offline_merge_new_face(db, tmp):
    print("check 4: offline merge adds a new local-object face")
    copy = os.path.join(tmp, "field.db")
    osmface.checkout_workcopy(db, copy, revision=4)
    osmface.workcopy_edit(copy, edit_file(
        tmp, "e.jsonl", "day1", 0, [
            {"op": "put", "expected_version": 0, "entity": lnode("na", 70, 70)},
            {"op": "put", "expected_version": 0, "entity": lnode("nb", 80, 70)},
            {"op": "put", "expected_version": 0, "entity": lnode("nc", 80, 80)},
            {"op": "put", "expected_version": 0, "entity": lnode("nd", 70, 80)},
            {"op": "put", "expected_version": 0, "entity": lway(
                "w1", [{"local": "na"}, {"local": "nb"}, {"local": "nc"},
                       {"local": "nd"}, {"local": "na"}])},
            {"op": "put", "expected_version": 0,
             "entity": lrel("r1", [lm("w1")])},
        ]))
    pkg = os.path.join(tmp, "pkg")
    osmface.workcopy_export(copy, pkg)
    plan = osmface.create_merge_plan(db, pkg)
    receipt = osmface.confirm_merge(db, plan["plan_id"], "check-merge-1")
    new_rev = receipt["result_revision"]
    new_rid = receipt["allocation"]["relation"]["r1"]
    got = api_hits(osmface.query_faces_at_point(db, 75, 75, revision=new_rev))
    assert got == reference_point(db, new_rev, 75, 75) == {new_rid: [0]}
    assert osmface.query_faces_at_point(db, 75, 75, revision=0)["hits"] == []
    print(f"  merge revision {new_rev}: new relation {new_rid} queryable; "
          "old revisions unchanged")


def check_index_bookkeeping(db):
    print("check 5: physical index rows and per-revision versions")
    import sqlite3
    conn = sqlite3.connect(db)
    head = conn.execute(
        "SELECT head_revision FROM rev_meta WHERE id=1").fetchone()[0]
    meta = conn.execute(
        "SELECT revision, rtree_rows, bypass_rows FROM spatial_index_meta"
        " ORDER BY revision").fetchall()
    assert [r[0] for r in meta] == list(range(head + 1))
    for revision, rtree_rows, bypass_rows in meta:
        physical = conn.execute(
            "SELECT COUNT(*) FROM face_rtree_rows WHERE revision=?",
            (revision,)).fetchone()[0]
        # complete faces *effective* at the revision: each relation's latest
        # result row at or before the revision, when it is complete, with all
        # the faces persisted alongside that result row
        effective_faces = 0
        relations = conn.execute(
            "SELECT DISTINCT relation_id FROM rel_result_history"
            " WHERE revision <= ?", (revision,)).fetchall()
        for (relation_id,) in relations:
            row = conn.execute(
                "SELECT revision, status FROM rel_result_history"
                " WHERE relation_id = ? AND revision <= ?"
                " ORDER BY revision DESC LIMIT 1",
                (relation_id, revision)).fetchone()
            if row[1] != "complete":
                continue
            effective_faces += conn.execute(
                "SELECT COUNT(*) FROM face_history"
                " WHERE revision = ? AND relation_id = ?",
                (row[0], relation_id)).fetchone()[0]
        assert physical == rtree_rows
        assert physical + bypass_rows == effective_faces, (
            revision, physical, bypass_rows, effective_faces)
    conn.close()
    print(f"  all {head + 1} revisions have complete, matching index rows")


def main():
    tmp = tempfile.mkdtemp(prefix="osmface-spatial-check-")
    src = write_jsonl(
        os.path.join(tmp, "spatial_world.jsonl"), spatial_world_records())
    db = os.path.join(tmp, "spatial.db")
    osmface.load_database(db, [src], force=True)
    osmface.assemble_database(db)
    osmface.enable_history(db)
    check_rev0_hand_matrix(db)
    check_boundary_move(db, tmp)
    check_delete_incomplete_restore(db, tmp)
    check_offline_merge_new_face(db, tmp)
    check_index_bookkeeping(db)
    print("ALL INDEPENDENT SPATIAL CHECKS PASSED")


if __name__ == "__main__":
    main()

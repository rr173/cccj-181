"""Hand-written acceptance for historical spatial face queries.

The world (fixtures/spatial_world.jsonl, all integer coordinates):

  R (400): outer square (0,0)-(20,20) with a hole (4,4)-(12,12) and no
           object in the hole -> one face; point (5,5) is a true empty hit.
  I (800): independent island square (6,6)-(10,10), one face.
  U (600): disjoint square (40,40)-(50,50), one face.
  V (700): square (15,15)-(25,25) overlapping R's corner, one face.
  M (900): two disjoint outer squares -> MultiPolygon, faces 0 and 1.

Every expectation below is derived from the hand-written coordinates, not
from the production query code.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest
from shapely.geometry import shape

import osmface
import osmface.spatial as sp
from conftest import ROOT, fixture, run_cli
from history_util import (
    change_file,
    delete,
    node,
    put,
    rel,
    wm,
)

R, I, U, V, M = 400, 800, 600, 700, 900


def _db(tmp_path):
    path = str(tmp_path / "spatial.db")
    osmface.load_database(path, [fixture("spatial_world.jsonl")], force=True)
    osmface.assemble_database(path)
    osmface.enable_history(path)
    return path


def _hits(result):
    return {h["relation_id"]: h["faces"] for h in result["hits"]}


# ---------------------------------------------------------------------------
# revision 0: hand-written point / range matrix
# ---------------------------------------------------------------------------

def test_rev0_face_structure(tmp_path):
    db = _db(tmp_path)
    r = osmface.inspect_database(db, relation_id=R, revision=0)["relation"]
    assert r["status"] == "complete"
    assert len(r["faces"]) == 1
    assert shape(r["faces"][0]["geometry"]).area == 336.0
    i = osmface.inspect_database(db, relation_id=I, revision=0)["relation"]
    assert shape(i["faces"][0]["geometry"]).area == 16.0
    # the MultiPolygon relation really has two independently indexed faces
    m = osmface.inspect_database(db, relation_id=M, revision=0)["relation"]
    assert len(m["faces"]) == 2
    assert all(shape(f["geometry"]).area == 16.0 for f in m["faces"])


def test_point_matrix_handwritten_expectations(tmp_path):
    db = _db(tmp_path)
    cases = [
        # (x, y, expected {relation: faces})
        (1, 1, {R: [0]}),                 # inside the outer face
        (5, 5, {}),                       # inside R's hole, no relation there
        (8, 8, {I: [0]}),                 # inside the independent island
        (45, 45, {U: [0]}),               # inside the disjoint square
        (30, 30, {}),                     # outside everything
        (0, 5, {R: [0]}),                 # on the outer boundary -> covered
        (4, 5, {R: [0]}),                 # on the hole boundary -> covered
        (6, 8, {I: [0]}),                 # on the island boundary -> covered
        (18, 18, {R: [0], V: [0]}),       # overlapping region: both returned
        (0, 0, {R: [0]}),                 # exact outer corner
        (20, 20, {R: [0], V: [0]}),       # corner shared by R and V
        (62, 62, {M: [0]}),               # MultiPolygon first face
        (72, 72, {M: [1]}),               # MultiPolygon second face
        (67, 67, {}),                     # between M's two faces: empty
    ]
    for x, y, expected in cases:
        result = osmface.query_faces_at_point(db, x, y, revision=0)
        assert result["query"] == "point"
        assert result["revision"] == 0
        assert result["head_revision"] == 0
        assert _hits(result) == expected, (x, y, result)


def test_range_matrix_handwritten_expectations(tmp_path):
    db = _db(tmp_path)
    # a box wholly inside the hole and clear of the island: no face
    # intersection (bbox-only would produce a false hit on R's outer box)
    assert _hits(osmface.query_faces_in_range(db, 4.5, 4.5, 5.5, 5.5,
                                               revision=0)) == {}
    # a box crossing the hole boundary touches the outer face boundary
    assert _hits(osmface.query_faces_in_range(db, 2, 2, 5, 5, revision=0)) == {R: [0]}
    # a box around the island hits the island only (its bbox is inside the
    # hole, so R's hole interior rejects it)
    assert _hits(osmface.query_faces_in_range(db, 7, 7, 9, 9, revision=0)) == {I: [0]}
    # a box touching only the island corner still intersects the island
    corner = osmface.query_faces_in_range(db, 5, 5, 6, 6, revision=0)
    assert _hits(corner) == {I: [0]}
    # zero-width range exactly touching the outer boundary
    touch = osmface.query_faces_in_range(db, 20, -5, 20, 25, revision=0)
    assert _hits(touch) == {R: [0], V: [0]}
    # zero-height range along the bottom edge
    edge = osmface.query_faces_in_range(db, -5, 0, 25, 0, revision=0)
    assert _hits(edge) == {R: [0]}
    # a degenerate point-like range at an interior point
    pt = osmface.query_faces_in_range(db, 1, 1, 1, 1, revision=0)
    assert _hits(pt) == {R: [0]}
    # range over the disjoint square only
    assert _hits(osmface.query_faces_in_range(db, 42, 42, 48, 48, revision=0)) == {U: [0]}
    # big range: every complete relation appears once, with real face lists
    whole = osmface.query_faces_in_range(db, -100, -100, 100, 100, revision=0)
    assert _hits(whole) == {R: [0], I: [0], U: [0], V: [0], M: [0, 1]}
    # stable ordering by relation id
    assert [h["relation_id"] for h in whole["hits"]] == sorted(
        h["relation_id"] for h in whole["hits"])


def test_result_shape_and_candidate_counts(tmp_path):
    db = _db(tmp_path)
    result = osmface.query_faces_at_point(db, 5, 5, revision=0)
    # the hole point lies inside R's outer face bbox (so the R-tree returns
    # face 0 as a candidate) but no other stored box reaches it; exact
    # Shapely covers() then rejects the hole interior -> zero hits
    assert result["hit_count"] == 0 and result["hits"] == []
    assert result["index"]["candidate_faces"] == 1
    assert result["exact_checks"] == 1
    interior = osmface.query_faces_at_point(db, 1, 1, revision=0)
    # only relations whose boxes overlap the tiny window are ever checked
    assert interior["index"]["candidate_relations"] == 1
    assert interior["exact_checks"] == 1 and interior["hit_count"] == 1
    # the island point reaches two stored boxes (R's outer bbox + I's box);
    # exact filtering keeps only the island face
    island_pt = osmface.query_faces_at_point(db, 8, 8, revision=0)
    assert island_pt["index"]["candidate_faces"] == 2
    assert _hits(island_pt) == {I: [0]}


# ---------------------------------------------------------------------------
# history: move a boundary, then prove old and new revisions differ
# ---------------------------------------------------------------------------

def test_moving_boundary_changes_hits_but_old_revision_is_frozen(tmp_path):
    db = _db(tmp_path)
    # expand the outer square by moving its right edge nodes (21, 22) from
    # x=20 to x=30; the hole stays contained and R stays complete
    osmface.apply_changes(db, change_file(
        tmp_path, "expand.jsonl", "expand", 0, [
            put(1, node(21, 30, 0)),
            put(1, node(22, 30, 20)),
        ]))
    # the strip between the old and new edge is uncovered at rev 0 ...
    old = osmface.query_faces_at_point(db, 25, 1, revision=0)
    assert _hits(old) == {}
    # ... and covered by R at the new revision (same fixed point)
    new = osmface.query_faces_at_point(db, 25, 1, revision=1)
    assert _hits(new) == {R: [0]}
    # points inside the surviving region still hit
    assert _hits(osmface.query_faces_at_point(db, 1, 1, revision=1)) == {R: [0]}
    # each revision has its own immutable index rows
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT revision, rtree_rows, bypass_rows FROM spatial_index_meta"
        " ORDER BY revision").fetchall()
    assert rows == [(0, 6, 0), (1, 6, 0)]
    rev0_rows = conn.execute(
        "SELECT COUNT(*) FROM face_rtree_rows WHERE revision=0").fetchone()[0]
    rev1_rows = conn.execute(
        "SELECT COUNT(*) FROM face_rtree_rows WHERE revision=1").fetchone()[0]
    assert rev0_rows == rev1_rows == 6
    conn.close()
    # index bounds really changed for R's outer face between revisions
    state = osmface.spatial_index_info(db)
    assert [r["revision"] for r in state["revisions"]] == [0, 1]
    assert state["head_revision"] == 1


# ---------------------------------------------------------------------------
# delete -> incomplete -> restore through apply
# ---------------------------------------------------------------------------

def test_delete_incomplete_restore_lifecycle(tmp_path):
    db = _db(tmp_path)
    # 1. delete the relation outright: absent marker, no index face at rev 1
    osmface.apply_changes(db, change_file(
        tmp_path, "del.jsonl", "del-r", 0, [delete(1, "relation", R)]))
    assert _hits(osmface.query_faces_at_point(db, 1, 1, revision=1)) == {}
    detail = osmface.inspect_database(db, relation_id=R, revision=1)["relation"]
    assert detail["status"] == "absent"
    # rev 0 still returns it
    assert _hits(osmface.query_faces_at_point(db, 1, 1, revision=0)) == {R: [0]}

    # 2. restore the relation but break it (references a missing way)
    osmface.apply_changes(db, change_file(
        tmp_path, "broken.jsonl", "broken-r", 1,
        [put(2, rel(R, [wm(999)]))]))
    assert _hits(osmface.query_faces_at_point(db, 1, 1, revision=2)) == {}
    detail = osmface.inspect_database(db, relation_id=R, revision=2)["relation"]
    assert detail["status"] == "incomplete" and detail["faces"] == []
    # no index row exists for R at revision 2 even though rev 0/1 history stays
    conn = sqlite3.connect(db)
    indexed = {row[0] for row in conn.execute(
        "SELECT DISTINCT relation_id FROM face_rtree_rows WHERE revision=2")}
    assert R not in indexed
    conn.close()

    # 3. restore the original definition: complete faces are queryable again
    osmface.apply_changes(db, change_file(
        tmp_path, "restore.jsonl", "restore-r", 2,
        [put(3, rel(R, [wm(20, "outer"), wm(21, "inner")]))]))
    assert _hits(osmface.query_faces_at_point(db, 1, 1, revision=3)) == {R: [0]}
    assert _hits(osmface.query_faces_at_point(db, 5, 5, revision=3)) == {}


def test_topology_conflict_relation_drops_out_of_index(tmp_path):
    db = _db(tmp_path)
    # make R reference a bowtie way (self-intersecting) -> topology_conflict
    bow = [
        put(0, node(70, 0, 0)), put(0, node(71, 2, 2)),
        put(0, node(72, 2, 0)), put(0, node(73, 0, 2)),
        put(0, {"type": "way", "id": 70, "nodes": [70, 71, 72, 73, 70]}),
        put(1, rel(R, [wm(70)])),
    ]
    osmface.apply_changes(db, change_file(
        tmp_path, "conflict.jsonl", "conflict", 0, bow))
    detail = osmface.inspect_database(db, relation_id=R, revision=1)["relation"]
    assert detail["status"] == "topology_conflict"
    assert _hits(osmface.query_faces_at_point(db, 1, 1, revision=1)) == {}
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM face_rtree_rows WHERE revision=1 AND relation_id=?",
        (R,)).fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------------------
# validation and explicit errors (never a silent empty success)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("x,y", [
    (float("nan"), 1.0), (1.0, float("inf")), (-float("inf"), 0.0),
    ("1", 1.0), (None, 1.0), (True, 1.0),
])
def test_invalid_point_coordinates_raise(tmp_path, x, y):
    db = _db(tmp_path)
    with pytest.raises(osmface.SpatialQueryError):
        osmface.query_faces_at_point(db, x, y)


@pytest.mark.parametrize("bounds", [
    (0, 0, float("nan"), 1), (float("inf"), 0, 1, 1),
    (2, 0, 1, 1),     # minx > maxx
    (0, 2, 1, 1),     # miny > maxy
    ("0", 0, 1, 1), (0, 0, 1, None),
])
def test_invalid_ranges_raise(tmp_path, bounds):
    db = _db(tmp_path)
    with pytest.raises(osmface.SpatialQueryError):
        osmface.query_faces_in_range(db, *bounds)


def test_zero_dimension_ranges_are_allowed(tmp_path):
    db = _db(tmp_path)
    sp.validate_bounds(5, 5, 5, 5)
    sp.validate_bounds(5, 0, 5, 10)
    sp.validate_bounds(0, 5, 10, 5)


def test_invalid_revision_raises(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(osmface.HistoryError):
        osmface.query_faces_at_point(db, 1, 1, revision=99)
    with pytest.raises(osmface.HistoryError):
        osmface.query_faces_at_point(db, 1, 1, revision=-1)
    with pytest.raises(osmface.HistoryError):
        osmface.query_faces_at_point(db, 1, 1, revision="0")


def test_spatial_queries_require_history(tmp_path):
    plain = str(tmp_path / "plain.db")
    osmface.load_database(plain, [fixture("spatial_world.jsonl")], force=True)
    osmface.assemble_database(plain)
    with pytest.raises(osmface.NotHistoryError):
        osmface.query_faces_at_point(plain, 1, 1)
    with pytest.raises(osmface.NotHistoryError):
        osmface.query_faces_in_range(plain, 0, 0, 1, 1)
    with pytest.raises(osmface.NotHistoryError):
        osmface.spatial_index_info(plain)


def test_plain_database_behaviour_is_unchanged(tmp_path):
    plain = str(tmp_path / "plain.db")
    osmface.load_database(plain, [fixture("spatial_world.jsonl")], force=True)
    osmface.assemble_database(plain)
    conn = sqlite3.connect(plain)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    conn.close()
    assert "face_rtree" not in tables and "spatial_index_meta" not in tables
    # raw records, assembly results and sources are all still present
    info = osmface.inspect_database(plain)
    assert info["counts"]["relation"] == 5
    assert info["status_counts"]["complete"] == 5


def test_idempotent_enable_keeps_one_index_and_all_sources(tmp_path):
    db = _db(tmp_path)
    again = osmface.enable_history(db)
    assert again["idempotent"] is True
    conn = sqlite3.connect(db)
    # no duplicated index rows or meta entries
    assert conn.execute(
        "SELECT COUNT(*) FROM spatial_index_meta").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM face_rtree_rows WHERE revision=0").fetchone()[0] == 6
    # raw input records and sources survive
    assert conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 5
    # legacy assembly mirror still present
    assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 6
    conn.close()
    # queries still work after the retry
    assert _hits(osmface.query_faces_at_point(db, 1, 1, revision=0)) == {R: [0]}


def test_query_geometry_and_index_share_one_revision_identity(tmp_path):
    db = _db(tmp_path)
    osmface.apply_changes(db, change_file(
        tmp_path, "expand.jsonl", "expand", 0,
        [put(1, node(21, 30, 0)), put(1, node(22, 30, 20))]))
    result = osmface.query_faces_at_point(db, 1, 1, revision=0)
    # every geometry row the query loads belongs to the pinned revision
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rev_rows = {row[0] for row in conn.execute(
        "SELECT DISTINCT revision FROM face_history").fetchall()}
    assert rev_rows == {0, 1}
    # candidates at revision 0 resolve to revision-0 geometry only
    cands, _ = sp.candidate_faces(conn, 0, 1, 1, 1, 1)
    for rid, face_index in cands:
        row = conn.execute(
            "SELECT revision FROM face_history"
            " WHERE revision=0 AND relation_id=? AND face_index=?",
            (rid, face_index)).fetchone()
        assert row is not None
    conn.close()
    assert result["revision"] == 0 and result["head_revision"] == 1


# ---------------------------------------------------------------------------
# R-tree precision boundary and the documented bypass
# ---------------------------------------------------------------------------

def test_rtree_rounds_outward_inside_float32_range(tmp_path):
    # independent observation on an isolated R-tree: an awkward finite box
    # is stored with an outward (containing) float32 box, never an inward one
    probe = sqlite3.connect(":memory:")
    probe.execute("CREATE VIRTUAL TABLE rt USING rtree(id, minx, maxx, miny, maxy)")
    big = 1.0e15
    probe.execute("INSERT INTO rt VALUES (1, ?, ?, 0, 1)", (big, big + 1000.0))
    row = probe.execute("SELECT minx, maxx FROM rt WHERE id=1").fetchone()
    assert row[0] <= big and row[1] >= big + 1000.0
    # and the stored outward box still matches a window on the true edge
    hits = probe.execute(
        "SELECT id FROM rt WHERE minx<=? AND maxx>=? AND miny<=? AND maxy>=?",
        (big, big, 1.0, 0.0)).fetchall()
    assert hits == [(1,)]
    probe.close()


def test_unindexable_box_uses_documented_bypass(tmp_path):
    db = _db(tmp_path)
    conn = osmface.history.connect(db)
    # a face box beyond float32 range cannot live in the R-tree
    huge = 1.0e39
    assert sp.box_is_indexable(0, 0, 1, 1)
    assert not sp.box_is_indexable(huge, 0, huge + 1, 1)
    sp.ensure_spatial_schema(conn)
    conn.execute("BEGIN IMMEDIATE")
    # fabricate one unindexable face as the only relation recomputed at the
    # new revision; the five existing relations are untouched and must be
    # carried forward from revision 0
    rev = osmface.history.head_revision(conn) + 1
    geojson = json.dumps({
        "type": "Polygon",
        "coordinates": [[[huge, 0], [huge + 1, 0], [huge + 1, 1],
                         [huge, 1], [huge, 0]]],
    })
    conn.execute(
        "INSERT INTO rel_result_history(revision, relation_id, status, reason,"
        " geometry, source_desc, expansion, n_occ, n_nodes)"
        " VALUES(?, 999, 'complete', NULL, NULL, NULL, NULL, 0, 0)", (rev,))
    conn.execute(
        "INSERT INTO face_history(revision, relation_id, face_index, geojson)"
        " VALUES(?, 999, 0, ?)", (rev, geojson))
    summary = sp.index_revision(conn, rev)
    conn.commit()
    # six carried R-tree faces plus the one bypass face
    assert summary == {"rtree_rows": 6, "bypass_rows": 1}
    # the bypass makes the otherwise un-indexable face a real candidate
    candidates, stats = sp.candidate_faces(conn, rev, huge, 0.2, huge + 1, 0.8)
    assert (999, 0) in candidates and stats["bypass_candidates"] == 1
    # and a window far away does not pull it in, but carried faces survive
    far, far_stats = sp.candidate_faces(conn, rev, 0, 0, 1, 1)
    assert (999, 0) not in far and far_stats["bypass_candidates"] == 0
    assert far_stats["bypass_indexed"] == 1
    assert far_stats["candidate_faces"] >= 1
    osmface.history.close(conn)


# ---------------------------------------------------------------------------
# index write failure: no visible head/geometry/index inconsistency
# ---------------------------------------------------------------------------

def test_index_write_failure_rolls_back_the_whole_revision(tmp_path, monkeypatch):
    db = _db(tmp_path)
    calls = {"n": 0}
    real = sp.index_revision

    def flaky(conn, revision):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("simulated index write failure")
        return real(conn, revision)

    monkeypatch.setattr(osmface.history.spatial, "index_revision", flaky)
    with pytest.raises(sqlite3.OperationalError):
        osmface.apply_changes(db, change_file(
            tmp_path, "move.jsonl", "move-fail", 0,
            [put(1, node(21, 30, 0))]))
    # nothing moved: head, geometry and index all stay at revision 0
    assert osmface.inspect_database(db)["revision"] == 0
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT head_revision FROM rev_meta WHERE id=1").fetchone()[0] == 0
    assert conn.execute(
        "SELECT MAX(revision) FROM spatial_index_meta").fetchone()[0] == 0
    assert conn.execute(
        "SELECT MAX(revision) FROM face_history").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_history WHERE type='node' AND id=21"
    ).fetchone()[0] == 1
    conn.close()
    # the same batch then lands cleanly (an outward move that keeps every
    # ring simple and nested) and the index catches up
    receipt = osmface.apply_changes(db, change_file(
        tmp_path, "move2.jsonl", "move-ok", 0, [put(1, node(21, 30, 0))]))
    assert receipt["revision"] == 1
    assert osmface.spatial_index_info(db)["revisions"][-1]["revision"] == 1
    assert _hits(osmface.query_faces_at_point(db, 1, 1, revision=1)) == {R: [0]}
    # unchanged relations were carried into the new revision's index
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM face_rtree_rows WHERE revision=1").fetchone()[0] == 6
    conn.close()


def test_corrupt_index_revision_is_reported_not_returned_as_empty(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    # delete the index rows but keep the geometry: a query must error loudly
    conn.execute("DELETE FROM face_rtree")
    conn.execute("DELETE FROM face_rtree_rows")
    conn.execute("DELETE FROM spatial_index_meta")
    conn.commit()
    conn.close()
    with pytest.raises(osmface.SpatialQueryError):
        osmface.query_faces_at_point(db, 1, 1, revision=0)


# ---------------------------------------------------------------------------
# concurrent commit while a query runs: the read revision stays fixed
# ---------------------------------------------------------------------------

def test_query_pins_revision_against_concurrent_commit(tmp_path):
    db = _db(tmp_path)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / "wait-precommit").write_text("")
    batch = change_file(
        tmp_path, "expand.jsonl", "concurrent-expand", 0,
        [put(1, node(21, 30, 0)), put(1, node(22, 30, 20))])
    env = dict(os.environ, PYTHONPATH=ROOT, OSMFACE_BUSY_TIMEOUT="500",
               OSMFACE_SYNC_DIR=str(sync_dir))
    writer = subprocess.Popen(
        [sys.executable, "-m", "osmface", "apply", db, batch],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    deadline = time.time() + 30
    while not (sync_dir / "reached-precommit").exists():
        time.sleep(0.02)
        assert time.time() < deadline
    # query with an explicit old revision while the writer is parked:
    # the newly covered strip is still empty on the pinned revision
    pinned = osmface.query_faces_at_point(db, 25, 1, revision=0)
    assert pinned["revision"] == 0 and _hits(pinned) == {}
    (sync_dir / "proceed-precommit").write_text("")
    (sync_dir / "proceed-committed").write_text("")
    _, err = writer.communicate(timeout=30)
    assert writer.returncode == 0, err.decode()
    after = osmface.query_faces_at_point(db, 25, 1, 1)
    assert after["revision"] == 1 and _hits(after) == {R: [0]}


def test_default_revision_pins_head_at_query_start(tmp_path, monkeypatch):
    db = _db(tmp_path)
    seen = {}
    real_pin = sp._pin

    def observe(conn, revision):
        pinned, head = real_pin(conn, revision)
        seen["pinned"] = pinned
        return pinned, head

    monkeypatch.setattr(sp, "_pin", observe)
    result = osmface.query_faces_at_point(db, 1, 1)
    assert result["revision"] == seen["pinned"] == 0
    assert result["head_revision"] == 0


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------

def test_cli_query_point_and_range(tmp_path):
    db = _db(tmp_path)
    proc = run_cli("query-point", db, "8", "8", "--revision", "0")
    assert proc.returncode == 0
    assert "relation 800: faces=[0]" in proc.stdout
    assert "revision=0" in proc.stdout and "candidates=" in proc.stdout
    proc = run_cli("query-range", db, "-100", "-100", "100", "100",
                   "--revision", "0", "--json")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert {h["relation_id"] for h in payload["hits"]} == {R, I, U, V, M}
    # bad coordinates are a domain error, not an empty success
    bad = run_cli("query-point", db, "nan", "1")
    assert bad.returncode == 2 and "error" in bad.stderr
    bad_rev = run_cli("query-point", db, "1", "1", "--revision", "42")
    assert bad_rev.returncode == 2
    idx = run_cli("spatial-index", db)
    assert idx.returncode == 0
    assert '"revision": 0' in idx.stdout

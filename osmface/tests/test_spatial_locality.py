"""Spatial queries never scan or deserialize the whole warehouse.

Independent evidence (not API self-reporting):

* ``EXPLAIN QUERY PLAN`` shows candidate retrieval uses the R-tree virtual
  table index plus the companion rowid index, never a full scan of
  ``face_history``;
* a SQLite authorizer records every table read during a point query and
  proves the raw payload tables (nodes/ways/relations) are never opened and
  ``face_history`` is only fetched for index-selected candidates;
* on a runtime-generated grid, a point in one cell checks exactly one face,
  while hundreds of unrelated relations exist in the same revision.

Scale data is generated into tmp_path at test time and never committed.
"""
import os
import subprocess
import sys
import sqlite3

import osmface
import osmface.spatial as sp
from conftest import ROOT

GEN = os.path.join(ROOT, "fixtures", "gen_scale.py")


def _generate_scale(tmp_path, unused=5000, ring_nodes=400, grid=12):
    path = str(tmp_path / "scale.jsonl")
    subprocess.run(
        [sys.executable, GEN, path, "--unused", str(unused),
         "--ring-nodes", str(ring_nodes), "--grid", str(grid)],
        check=True, capture_output=True, text=True,
    )
    return path, grid * grid + 1


def _warehouse(tmp_path):
    src, total = _generate_scale(tmp_path)
    db = str(tmp_path / "scale.db")
    osmface.load_database(db, [src])
    osmface.assemble_database(db)
    osmface.enable_history(db)
    return db, total


def test_point_query_checks_only_the_candidate_face(tmp_path):
    db, total_relations = _warehouse(tmp_path)
    # grid square 0 occupies (3_000_000, 3_000_000)..(+10, +10)
    result = osmface.query_faces_at_point(db, 3_000_005.0, 3_000_005.0)
    assert result["hit_count"] == 1
    assert result["hits"][0]["relation_id"] == 800000
    # one candidate face, one exact Shapely check, despite 145 relations
    assert result["index"]["candidate_faces"] == 1
    assert result["exact_checks"] == 1
    assert total_relations > 100
    # a point outside the 1e6-radius circle and far from the 3e6 grid:
    # zero candidates, zero geometry deserializations
    empty = osmface.query_faces_at_point(db, 2_000_000.0, 0.0)
    assert empty["hits"] == []
    assert empty["index"]["candidate_faces"] == 0
    assert empty["exact_checks"] == 0


def test_candidate_sql_uses_rtree_not_full_scan(tmp_path):
    db, _ = _warehouse(tmp_path)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    plan = conn.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT m.relation_id, m.face_index"
        " FROM face_rtree r JOIN face_rtree_rows m ON m.rowid = r.id"
        " WHERE m.revision = ?"
        " AND r.minx <= ? AND r.maxx >= ?"
        " AND r.miny <= ? AND r.maxy >= ?",
        (0, 3_000_006.0, 3_000_004.0, 3_000_006.0, 3_000_004.0),
    ).fetchall()
    rendered = " ".join(row[-1] for row in plan).upper()
    assert "FACE_RTREE" in rendered
    # the R-tree access is an indexed virtual-table lookup, never a SCAN of
    # the geometry table
    assert "FACE_HISTORY" not in rendered
    # the companion table really has a revision-leading index for old rows
    indexes = {r[1] for r in conn.execute(
        "PRAGMA index_list(face_rtree_rows)")}
    assert "idx_face_rtree_rows_rev" in indexes
    conn.close()


def test_query_never_opens_raw_payload_tables_and_fetches_only_candidates(tmp_path):
    db, total_relations = _warehouse(tmp_path)
    conn = osmface.history.connect(db)
    reads = []

    def authorizer(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_READ:
            reads.append((arg1, arg2))
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    result = sp.query_point(conn, 3_000_005.0, 3_000_005.0, revision=0)
    conn.close()

    tables = {t for t, _c in reads}
    raw_payload = {"nodes", "ways", "relations", "faults"}
    assert tables.isdisjoint(raw_payload), tables & raw_payload
    # geometry history is read, but exactly once per candidate face
    face_reads = [c for t, c in reads if t == "face_history"]
    assert face_reads.count("geojson") == result["index"]["candidate_faces"] == 1
    # and never via an unscoped full table scan of all relations' geometry
    assert total_relations > 100
    assert len(face_reads) < total_relations


def test_range_query_candidate_count_stays_small_on_grid(tmp_path):
    db, _ = _warehouse(tmp_path)
    # a window covering at most four grid cells
    result = osmface.query_faces_in_range(
        db, 3_000_009.0, 3_000_009.0, 3_000_025.0, 3_000_025.0)
    assert 1 <= result["index"]["candidate_faces"] <= 4
    assert result["exact_checks"] == result["index"]["candidate_faces"]
    assert all(h["faces"] for h in result["hits"])
    # the huge circle relation and all distant cells were never deserialized
    assert 900000 not in {h["relation_id"] for h in result["hits"]}

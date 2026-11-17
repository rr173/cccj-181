"""Locality: a small change on scale data must not scan or recompute the map.

Proven with independent evidence, not self-reported counts:
* receipt recomputed set equals the exact affected relation ids;
* fetch counters stay bounded by one relation's boundary;
* a SQLite authorizer shows dependency analysis never opens the raw
  node/way/relation payload tables;
* EXPLAIN QUERY PLAN shows the reverse-index lookup uses an index, not a
  full table scan.
"""
import os
import shutil
import sqlite3
import subprocess
import sys

import osmface
import osmface.history as hist
from conftest import ROOT
from history_util import change_file, node, put

GEN = os.path.join(ROOT, "fixtures", "gen_scale.py")


def _generate_scale(tmp_path, unused=10000, ring_nodes=1000, grid=15):
    path = str(tmp_path / "scale.jsonl")
    subprocess.run(
        [sys.executable, GEN, path, "--unused", str(unused),
         "--ring-nodes", str(ring_nodes), "--grid", str(grid)],
        check=True, capture_output=True, text=True,
    )
    return path


def test_unused_node_change_recomputes_nothing(tmp_path):
    src = _generate_scale(tmp_path)
    db = str(tmp_path / "scale.db")
    osmface.load_database(db, [src])
    osmface.assemble_database(db)
    osmface.enable_history(db)

    receipt = osmface.apply_changes(db, change_file(
        tmp_path, "tiny.jsonl", "tiny", 0,
        [put(1, node(5_000_000, 1.0, -10_000_000.0))]))
    assert receipt["recomputed_relations"] == []
    assert receipt["fetched"] == {"node_rows": 0, "way_rows": 0}


def test_one_grid_node_recomputes_exactly_one_relation(tmp_path):
    src = _generate_scale(tmp_path)
    db = str(tmp_path / "scale.db")
    osmface.load_database(db, [src])
    osmface.assemble_database(db)
    osmface.enable_history(db)
    total_relations = osmface.inspect_database(db)["counts"]["relation"]
    assert total_relations > 200

    receipt = osmface.apply_changes(db, change_file(
        tmp_path, "one.jsonl", "one", 0,
        [put(1, node(8_000_001, 11.0, 3_000_000.0))]))
    assert receipt["recomputed_relations"] == [800000]
    # only the one boundary way and its four nodes were fetched
    assert receipt["fetched"] == {"node_rows": 4, "way_rows": 1}


def test_dependency_analysis_never_reads_raw_payload_tables(tmp_path):
    src = _generate_scale(tmp_path)
    db = str(tmp_path / "scale.db")
    osmface.load_database(db, [src])
    osmface.assemble_database(db)
    osmface.enable_history(db)

    probe = str(tmp_path / "probe.db")
    shutil.copy(db, probe)
    for ext in ("-wal", "-shm"):
        if os.path.exists(db + ext):
            shutil.copy(db + ext, probe + ext)

    changes_file = change_file(
        tmp_path, "c.jsonl", "probe", 0,
        [put(1, node(8_000_002, 12.0, 3_000_010.0))])
    _header, _raw, _digest, changes = hist.parse_change_file(changes_file)

    accessed_tables = set()

    def authorizer(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_READ:
            accessed_tables.add(arg1)
        return sqlite3.SQLITE_OK

    conn = hist.connect(probe)
    conn.set_authorizer(authorizer)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM apply_lock WHERE id=1")
    conn.execute(
        "INSERT INTO apply_lock(id, holder, acquired_at) VALUES(1,?,?)",
        ("probe", "now"))
    hist._affected_relations(conn, changes, 0)
    conn.rollback()
    conn.close()

    raw_payload = {"nodes", "ways", "relations", "faults"}
    assert accessed_tables.isdisjoint(raw_payload), (
        f"dependency scan touched raw payload tables: "
        f"{accessed_tables & raw_payload}"
    )


def test_reverse_lookup_uses_index_not_full_scan(tmp_path):
    src = _generate_scale(tmp_path)
    db = str(tmp_path / "scale.db")
    osmface.load_database(db, [src])
    osmface.assemble_database(db)
    osmface.enable_history(db)
    conn = hist.connect(db)
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT src_type, src_id FROM ref_edge_history"
        " WHERE ref_type=? AND ref_id=? AND revision<=?",
        ("node", 8_000_001, 0),
    ).fetchall()
    conn.close()
    rendered = " ".join(row[-1] for row in plan)
    assert "SCAN" not in rendered.upper() or "USING INDEX" in rendered.upper()
    assert "idx_ref_edge_reverse" in rendered

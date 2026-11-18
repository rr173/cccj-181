"""Locality: a merge touching one object must not rescan or recompute
the whole map.  Proven with the receipt's real recomputed set and fetch
counters, plus a SQLite authorizer showing the merge never opens the raw
node/way/relation payload tables of the legacy staging schema.
"""
import os
import sqlite3
import subprocess
import sys

import osmface
from conftest import ROOT
from history_util import change_file, node, put
from merge_util import edit_file, lnode

GEN = os.path.join(ROOT, "fixtures", "gen_scale.py")


def _generate_scale(tmp_path, unused=10000, ring_nodes=1000, grid=15):
    path = str(tmp_path / "scale.jsonl")
    subprocess.run(
        [sys.executable, GEN, path, "--unused", str(unused),
         "--ring-nodes", str(ring_nodes), "--grid", str(grid)],
        check=True, capture_output=True, text=True,
    )
    return path


def _scale_warehouse(tmp_path):
    src = _generate_scale(tmp_path)
    db = str(tmp_path / "scale.db")
    osmface.load_database(db, [src])
    osmface.assemble_database(db)
    osmface.enable_history(db)
    return db


def test_merge_of_unused_node_recomputes_nothing(tmp_path):
    db = _scale_warehouse(tmp_path)
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy)
    # field moves one unreferenced node and adds one local node
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1,
         "entity": node(5_000_000, 1.0, -10_000_000.0)},
        {"op": "put", "expected_version": 0, "entity": lnode("n1", 3, 3)},
    ]))
    pkg = str(tmp_path / "pkg")
    osmface.workcopy_export(copy, pkg)
    plan = osmface.create_merge_plan(db, pkg)
    receipt = osmface.confirm_merge(db, plan["plan_id"], "m-scale")
    assert receipt["recomputed_relations"] == []
    assert receipt["fetched"] == {"node_rows": 0, "way_rows": 0}
    assert receipt["result_revision"] == 1


def test_merge_one_grid_node_recomputes_exactly_one_relation(tmp_path):
    db = _scale_warehouse(tmp_path)
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy)
    # node 8000000 is a corner of grid relation 800000 only
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1,
         "entity": node(8_000_000, 3_000_001.0, 3_000_001.0)},
    ]))
    pkg = str(tmp_path / "pkg")
    osmface.workcopy_export(copy, pkg)
    plan = osmface.create_merge_plan(db, pkg)
    receipt = osmface.confirm_merge(db, plan["plan_id"], "m-grid")
    assert receipt["recomputed_relations"] == [800000]
    # fetches are bounded by that one relation's boundary, not the map
    assert receipt["fetched"]["node_rows"] <= 8
    assert receipt["fetched"]["way_rows"] <= 2


def test_merge_never_reads_legacy_payload_tables(tmp_path):
    db = _scale_warehouse(tmp_path)
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy)
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1,
         "entity": node(8_000_000, 3_000_001.0, 3_000_001.0)},
    ]))
    pkg = str(tmp_path / "pkg")
    osmface.workcopy_export(copy, pkg)
    plan = osmface.create_merge_plan(db, pkg)

    opened = []
    def authorizer(action, arg1, arg2, dbname, source):
        if action == sqlite3.SQLITE_READ and arg2 is not None:
            opened.append(arg1)
        return sqlite3.SQLITE_OK

    conn = sqlite3.connect(db)
    conn.set_authorizer(authorizer)
    conn.close()
    # run the confirm on a connection wrapped by the authorizer: use the
    # public API but spy through a dedicated connection is not possible,
    # so instead verify via the module-level connect hook
    import osmface.history as hist
    real_connect = hist.connect
    def spying_connect(path):
        c = real_connect(path)
        c.set_authorizer(authorizer)
        return c
    hist.connect = spying_connect
    try:
        osmface.confirm_merge(db, plan["plan_id"], "m-auth")
    finally:
        hist.connect = real_connect
    legacy_payload_tables = {"nodes", "ways", "relations"}
    assert not (set(opened) & legacy_payload_tables), (
        f"merge read legacy payload tables: {set(opened) & legacy_payload_tables}"
    )

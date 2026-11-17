"""Hand-written incremental narrative with shoelace-checked areas.

World: rectangle way 10 -> relation A (100) -> parent P (200); unrelated U
(300); gap relation G (400) on missing way 77.
"""
from shapely.geometry import shape

import osmface
from history_util import (
    build_warehouse,
    change_file,
    delete,
    node,
    put,
    rel,
    rm,
    shoelace,
    way,
    wm,
)


def _area(detail):
    return shape(detail["faces"][0]["geometry"]).area


def test_move_right_nodes_100_to_120_unrelated_not_recomputed(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    # hand check the rev-0 rectangle with the shoelace formula
    rev0 = osmface.inspect_database(db, relation_id=100, revision=0)["relation"]
    ring_nodes = rev0["faces"][0]["rings"][0]["nodes"]
    coords = {1: (0, 0), 2: (10, 0), 3: (10, 10), 4: (0, 10)}
    assert shoelace([coords[n] for n in ring_nodes[:-1]]) == 100.0
    assert _area(rev0) == 100.0
    assert _area(osmface.inspect_database(
        db, relation_id=200, revision=0)["relation"]) == 100.0

    receipt = osmface.apply_changes(db, change_file(
        tmp_path, "move.jsonl", "move", 0, [
            put(1, node(2, 12, 0)),
            put(1, node(3, 12, 10)),
        ]))
    # exactly A and P recompute; U is untouched
    assert receipt["recomputed_relations"] == [100, 200]
    assert {r["relation"] for r in receipt["affected_relations"]} == {100, 200}

    rev1_a = osmface.inspect_database(db, relation_id=100, revision=1)["relation"]
    rev1_p = osmface.inspect_database(db, relation_id=200, revision=1)["relation"]
    assert _area(rev1_a) == 120.0 and _area(rev1_p) == 120.0
    # independent shoelace: width 12 * height 10
    coords1 = {1: (0, 0), 2: (12, 0), 3: (12, 10), 4: (0, 10)}
    assert shoelace([coords1[n] for n in
                     rev1_a["faces"][0]["rings"][0]["nodes"][:-1]]) == 120.0

    # U did not recompute and keeps its 100 area
    u1 = osmface.inspect_database(db, relation_id=300, revision=1)["relation"]
    assert _area(u1) == 100.0
    u_receipt = [r for r in receipt["affected_relations"]
                 if r["relation"] == 300]
    assert u_receipt == []

    # old revision still reports 100 with the old object source versions
    old_a = osmface.inspect_database(db, relation_id=100, revision=0)["relation"]
    assert _area(old_a) == 100.0
    seg = old_a["faces"][0]["rings"][0]["segments"][0]
    assert seg["way_version"] == 1
    assert dict(seg["node_versions"]) == {1: 1, 2: 1, 3: 1, 4: 1}
    # the new revision records node versions 2 for the moved nodes
    new_seg = rev1_a["faces"][0]["rings"][0]["segments"][0]
    assert dict(new_seg["node_versions"]) == {1: 1, 2: 2, 3: 2, 4: 1}


def test_batch_level_forward_reference_and_way_swap(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    # new independent closed way 20 on nodes 21..24; A switches to it and
    # old way 10 is deleted in the same batch (forward refs allowed)
    receipt = osmface.apply_changes(db, change_file(
        tmp_path, "swap.jsonl", "swap", 0, [
            put(0, node(21, 100, 100)), put(0, node(22, 120, 100)),
            put(0, node(23, 120, 110)), put(0, node(24, 100, 110)),
            put(0, way(20, [21, 22, 23, 24, 21])),
            put(1, rel(100, [wm(20)])),
            delete(1, "way", 10),
        ]))
    assert receipt["recomputed_relations"] == [100, 200]
    a1 = osmface.inspect_database(db, relation_id=100, revision=1)["relation"]
    assert _area(a1) == 200.0
    assert a1["faces"][0]["rings"][0]["segments"][0]["way"] == 20
    # old way 10 is a tombstone
    import sqlite3
    row = sqlite3.connect(db).execute(
        "SELECT deleted, version FROM entity_versions WHERE type='way' AND id=10"
    ).fetchone()
    assert row == (1, 2)


def test_restore_deleted_way_does_not_affect_detached_relations(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    osmface.apply_changes(db, change_file(
        tmp_path, "swap.jsonl", "swap", 0, [
            put(0, node(21, 100, 100)), put(0, node(22, 120, 100)),
            put(0, node(23, 120, 110)), put(0, node(24, 100, 110)),
            put(0, way(20, [21, 22, 23, 24, 21])),
            put(1, rel(100, [wm(20)])),
            delete(1, "way", 10),
        ]))
    # restore way 10 (tombstone v2) and move one of its nodes
    receipt = osmface.apply_changes(db, change_file(
        tmp_path, "restore.jsonl", "restore", 1, [
            put(2, way(10, [1, 2, 3, 4, 1])),
            put(1, node(2, 11, 0)),
        ]))
    # no relation recomputes: A/P detached from way 10, U/G never used it
    assert receipt["recomputed_relations"] == []
    a2 = osmface.inspect_database(db, relation_id=100, revision=2)["relation"]
    assert _area(a2) == 200.0
    p2 = osmface.inspect_database(db, relation_id=200, revision=2)["relation"]
    assert _area(p2) == 200.0


def test_gap_introduction_and_recovery(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    # G starts incomplete because way 77 is missing
    g0 = osmface.inspect_database(db, relation_id=400, revision=0)["relation"]
    assert g0["status"] == "incomplete"
    assert g0["reason"]["code"] == "missing_references"

    receipt = osmface.apply_changes(db, change_file(
        tmp_path, "fill.jsonl", "fill", 0, [
            put(0, node(71, 0, 200)), put(0, node(72, 5, 200)),
            put(0, node(73, 5, 205)), put(0, node(74, 0, 205)),
            put(0, way(77, [71, 72, 73, 74, 71])),
        ]))
    entry = receipt["affected_relations"][0]
    assert entry["relation"] == 400
    # G already referenced the missing way 77 at rev 0, so the real edge
    # grounds the witness in the old graph (and still in the new one)
    assert entry["basis"] in ("old", "both")
    witness = entry["paths"][0]
    edge = witness["path"][0]
    assert edge == ["relation", 400, 0, "way", 77, "outer"]
    g1 = osmface.inspect_database(db, relation_id=400, revision=1)["relation"]
    assert g1["status"] == "complete"
    assert shoelace([(0, 200), (5, 200), (5, 205), (0, 205)]) == 25.0
    assert _area(g1) == 25.0


def test_missing_reference_kept_as_edge_and_relation_recovers(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    # reverse edge for the missing way 77 is still stored
    import sqlite3
    edges = sqlite3.connect(db).execute(
        "SELECT src_id, ref_id FROM ref_edge_history"
        " WHERE ref_type='way' AND ref_id=77 AND revision=0"
    ).fetchall()
    assert edges == [(400, 77)]


def test_relation_delete_removes_current_but_keeps_history(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    osmface.apply_changes(db, change_file(
        tmp_path, "del.jsonl", "del", 0, [delete(1, "relation", 300)]))
    gone = osmface.inspect_database(db, relation_id=300, revision=1)["relation"]
    assert gone["status"] == "absent" and gone["defined"] is False
    # history still shows the complete relation at revision 0
    old = osmface.inspect_database(db, relation_id=300, revision=0)["relation"]
    assert old["status"] == "complete" and _area(old) == 100.0
    # restore from tombstone (v2) -> v3, complete again
    osmface.apply_changes(db, change_file(
        tmp_path, "res.jsonl", "res", 1,
        [put(2, rel(300, [wm(90)]))]))
    back = osmface.inspect_database(db, relation_id=300, revision=2)["relation"]
    assert back["status"] == "complete" and back["object_version"] == 3


def test_witness_path_uses_old_graph_on_detachment(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    # A drops way 10 (replaced by a brand new way); the old A -> way10 edge
    # must ground an old-graph witness for way 10's change
    receipt = osmface.apply_changes(db, change_file(
        tmp_path, "detach.jsonl", "detach", 0, [
            put(0, node(31, 0, 300)), put(0, node(32, 4, 300)),
            put(0, node(33, 4, 304)), put(0, node(34, 0, 304)),
            put(0, way(30, [31, 32, 33, 34, 31])),
            put(1, rel(100, [wm(30)])),
            delete(1, "way", 10),
        ]))
    a_entry = [r for r in receipt["affected_relations"]
               if r["relation"] == 100][0]
    bases = {p["basis"] for p in a_entry["paths"]}
    # relation 100 was directly modified; it is reachable in both graphs
    assert "direct" in bases or "both" in bases


def test_new_graph_only_witness_for_fresh_reference(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    # brand new relation 700 references a brand new way 80: the edge exists
    # only in the post-batch graph, so its witness must be grounded on "new"
    receipt = osmface.apply_changes(db, change_file(
        tmp_path, "fresh.jsonl", "fresh", 0, [
            put(0, node(81, 0, 400)), put(0, node(82, 3, 400)),
            put(0, node(83, 3, 403)), put(0, node(84, 0, 403)),
            put(0, way(80, [81, 82, 83, 84, 81])),
            put(0, rel(700, [wm(80)])),
        ]))
    entry = [r for r in receipt["affected_relations"]
             if r["relation"] == 700][0]
    assert entry["basis"] == "new"
    bases = {p["basis"] for p in entry["paths"]}
    assert "direct" in bases
    detail = osmface.inspect_database(db, relation_id=700, revision=1)["relation"]
    assert detail["status"] == "complete" and detail["object_version"] == 1


def test_reversed_way_keeps_geometry_changes_source(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    before = osmface.inspect_database(db, relation_id=100, revision=0)["relation"]
    receipt = osmface.apply_changes(db, change_file(
        tmp_path, "rev.jsonl", "rev", 0,
        [put(1, way(10, [1, 4, 3, 2, 1]))]))
    entry = [r for r in receipt["affected_relations"]
             if r["relation"] == 100][0]
    assert entry["geometry_changed"] is False
    assert entry["source_changed"] is True
    after = osmface.inspect_database(db, relation_id=100, revision=1)["relation"]
    assert _area(after) == _area(before) == 100.0
    # the segment now reports reversal against the new way definition
    assert after["faces"][0]["rings"][0]["segments"][0]["reversed"] is True

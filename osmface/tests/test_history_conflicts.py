"""Legal raw changes that break geometry are still accepted: the affected
relations get incomplete/conflict status and lose their stale faces, then
recover when the break is repaired.  Also covers enabling on a database that
already contains conflict relations."""
from shapely.geometry import shape

import osmface
from history_util import (
    build_warehouse,
    change_file,
    delete,
    node,
    put,
    way,
    wm,
)


def test_conflicting_change_accepted_then_face_removed_and_recovered(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    # delete a node the boundary needs: a legal object change, but way 10 no
    # longer has all its nodes -> A/P become incomplete, faces removed
    receipt = osmface.apply_changes(db, change_file(
        tmp_path, "break.jsonl", "break", 0, [delete(1, "node", 3)]))
    assert set(receipt["recomputed_relations"]) == {100, 200}
    broken = osmface.inspect_database(db, relation_id=100, revision=1)["relation"]
    assert broken["status"] == "incomplete"
    assert broken["faces"] == []  # yesterday's face must not survive
    parent = osmface.inspect_database(db, relation_id=200, revision=1)["relation"]
    assert parent["status"] == "incomplete"
    # unrelated U keeps its complete face
    u = osmface.inspect_database(db, relation_id=300, revision=1)["relation"]
    assert u["status"] == "complete" and u["faces"]

    # repair: re-create node 3 via a tombstone restore -> faces come back
    osmface.apply_changes(db, change_file(
        tmp_path, "fix.jsonl", "fix", 1, [put(2, node(3, 10, 10))]))
    fixed = osmface.inspect_database(db, relation_id=100, revision=2)["relation"]
    assert fixed["status"] == "complete"
    assert shape(fixed["faces"][0]["geometry"]).area == 100.0


def test_geometry_conflict_via_dangling_way_is_accepted(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    # give relation U a second open fragment whose endpoints cannot close:
    # a legal way definition, but the relation boundary becomes a conflict
    osmface.apply_changes(db, change_file(
        tmp_path, "add.jsonl", "add", 0, [
            put(0, node(61, 70, 50)), put(0, node(62, 80, 50)),
            put(0, way(91, [9, 61, 62])),
            put(1, {"type": "relation", "id": 300, "members": [
                wm(90), wm(91)]}),
        ]))
    detail = osmface.inspect_database(db, relation_id=300, revision=1)["relation"]
    # the change was accepted; the relation is isolated as a conflict/gap,
    # and the warehouse simply moves on to the next revision
    assert detail["status"] in ("topology_conflict", "incomplete")
    assert osmface.inspect_database(db)["revision"] == 1
    # the previous complete face is not exposed as current
    assert all(f == [] for f in [detail["faces"]]) or detail["faces"] == []


def test_enable_on_database_with_conflict_relation(tmp_path):
    # build a raw input that assembles a topology_conflict, then enable
    src = tmp_path / "conf.jsonl"
    records = [
        node(1, 0, 0), node(2, 2, 2), node(3, 2, 0), node(4, 0, 2),
        way(10, [1, 2, 3, 4, 1]),  # bowtie: self-intersecting
        {"type": "relation", "id": 900, "members": [wm(10)]},
    ]
    with open(src, "w", encoding="utf-8") as fh:
        import json
        for r in records:
            fh.write(json.dumps(r) + "\n")
    db = str(tmp_path / "c.db")
    osmface.load_database(db, [str(src)])
    osmface.assemble_database(db)
    summary = osmface.enable_history(db)  # conflict does not block enabling
    assert summary["revision"] == 0
    detail = osmface.inspect_database(db, relation_id=900, revision=0)["relation"]
    assert detail["status"] == "topology_conflict"
    assert detail["reason"]["code"] == "ring_self_intersection"

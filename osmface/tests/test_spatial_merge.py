"""The spatial index is maintained on the merge-confirm revision path too.

A field copy adds a brand-new local relation (with local way/nodes); after
merge-confirm lands it as one warehouse revision, the new local-object face
must be queryable through the spatial index while every older revision's
index stays untouched.
"""
import json
import sqlite3

import osmface
from conftest import fixture
from merge_util import edit_file, lm, lnode, lrel, lway


def _warehouse(tmp_path):
    db = str(tmp_path / "spatial.db")
    osmface.load_database(db, [fixture("spatial_world.jsonl")], force=True)
    osmface.assemble_database(db)
    osmface.enable_history(db)
    return db


def _hits(result):
    return {h["relation_id"]: h["faces"] for h in result["hits"]}


def test_merge_confirm_indexes_new_local_face(tmp_path):
    db = _warehouse(tmp_path)
    copy = str(tmp_path / "field.db")
    osmface.checkout_workcopy(db, copy, revision=0)
    # field work: a new closed way on four local nodes plus a local relation
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "day1", 0, [
        {"op": "put", "expected_version": 0, "entity": lnode("na", 70, 70)},
        {"op": "put", "expected_version": 0, "entity": lnode("nb", 80, 70)},
        {"op": "put", "expected_version": 0, "entity": lnode("nc", 80, 80)},
        {"op": "put", "expected_version": 0, "entity": lnode("nd", 70, 80)},
        {"op": "put", "expected_version": 0,
         "entity": lway("w1", [{"local": "na"}, {"local": "nb"},
                               {"local": "nc"}, {"local": "nd"},
                               {"local": "na"}])},
        {"op": "put", "expected_version": 0,
         "entity": lrel("r1", [lm("w1")])},
    ]))
    preview = osmface.workcopy_preview(copy)
    assert preview["status_counts"]["complete"] >= 1
    pkg = str(tmp_path / "pkg")
    osmface.workcopy_export(copy, pkg)

    plan = osmface.create_merge_plan(db, pkg)
    assert plan["unresolved_conflicts"] == []
    preview2 = osmface.merge_preview(db, plan["plan_id"])
    assert preview2["complete"] is True
    receipt = osmface.confirm_merge(db, plan["plan_id"], "merge-spatial-1")
    new_rev = receipt["result_revision"]
    assert new_rev == 1
    new_rid = None
    for (kind, name), allocated in _allocation_items(receipt):
        if kind == "relation" and name == "r1":
            new_rid = allocated
    assert new_rid is not None

    # the new face is queryable at the merge revision through the index
    hit = osmface.query_faces_at_point(db, 75, 75, revision=new_rev)
    assert _hits(hit) == {new_rid: [0]}
    # and the same point returns nothing at the old revision
    old = osmface.query_faces_at_point(db, 75, 75, revision=0)
    assert old["hits"] == []
    # real index rows exist for the new revision and rev 0 rows are intact
    conn = sqlite3.connect(db)
    meta = conn.execute(
        "SELECT revision, rtree_rows, bypass_rows FROM spatial_index_meta"
        " ORDER BY revision").fetchall()
    assert meta[0] == (0, 6, 0)
    assert meta[-1][0] == new_rev and meta[-1][1] == 7
    new_rows = conn.execute(
        "SELECT relation_id, face_index FROM face_rtree_rows"
        " WHERE revision=? ORDER BY relation_id, face_index",
        (new_rev,)).fetchall()
    assert (new_rid, 0) in new_rows
    rev0 = conn.execute(
        "SELECT COUNT(*) FROM face_rtree_rows WHERE revision=0").fetchone()[0]
    assert rev0 == 6
    conn.close()

    # idempotent retry of the same merge returns the original receipt and
    # must not duplicate index rows
    again = osmface.confirm_merge(db, plan["plan_id"], "merge-spatial-1")
    assert again["result_revision"] == new_rev
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM face_rtree_rows WHERE revision=?",
        (new_rev,)).fetchone()[0] == 7
    assert conn.execute(
        "SELECT COUNT(*) FROM spatial_index_meta WHERE revision=?",
        (new_rev,)).fetchone()[0] == 1
    conn.close()


def test_merge_with_no_entity_changes_creates_no_revision_or_index(tmp_path):
    db = _warehouse(tmp_path)
    copy = str(tmp_path / "field.db")
    osmface.checkout_workcopy(db, copy, revision=0)
    # a same-value put leaves the effective definitions identical to the
    # baseline, so the three-way merge lands no entity and no new revision
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "noop", 0, [
        {"op": "put", "expected_version": 1,
         "entity": {"type": "node", "id": 20, "x": 0, "y": 0}},
    ]))
    pkg = str(tmp_path / "pkg")
    osmface.workcopy_export(copy, pkg)
    plan = osmface.create_merge_plan(db, pkg)
    receipt = osmface.confirm_merge(db, plan["plan_id"], "merge-noop")
    assert receipt["result_revision"] == 0
    state = osmface.spatial_index_info(db)
    assert [r["revision"] for r in state["revisions"]] == [0]


def _allocation_items(receipt):
    for kind, mapping in receipt["allocation"].items():
        for name, allocated in mapping.items():
            yield (kind, name), allocated

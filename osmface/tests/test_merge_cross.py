"""Cross-side reference breaks: the warehouse deletes a node the field
keeps using; the plan must surface the gap, accept a custom repair, and
land exactly what the preview showed."""
import json
import sqlite3

import pytest
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
from merge_util import edit_file, lnode, local, lway


def _setup(tmp_path):
    """B: rectangle way 10 (nodes 1..4) -> relation 100.
    Main (H): deletes node 3 (relation 100 becomes incomplete).
    Field (L): rewires way 10 to [1,3,4,1] — still using deleted node 3 —
    and creates a local node n1 at the old corner.  Neither side has a
    content conflict on any single object."""
    db, _ = build_warehouse(tmp_path, osmface)
    osmface.apply_changes(db, change_file(
        tmp_path, "del.jsonl", "del", 0, [delete(1, "node", 3)]))
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy, revision=0)
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 0, "entity": lnode("n1", 10, 10)},
        {"op": "put", "expected_version": 1, "entity": way(10, [1, 3, 4, 1])},
    ]))
    pkg = str(tmp_path / "pkg")
    osmface.workcopy_export(copy, pkg)
    return db, pkg


def test_cross_gap_is_surfaced_in_plan_and_preview(tmp_path):
    db, pkg = _setup(tmp_path)
    plan = osmface.create_merge_plan(db, pkg)
    by_key = {(d["type"], d["id"]): d for d in plan["decisions"]}
    # node 3: main deleted it, field kept baseline -> keep_main (the delete)
    assert by_key[("node", 3)]["decision"] == "keep_main"
    assert by_key[("node", 3)]["h"]["state"] == "deleted"
    # way 10: field rewired it, main untouched -> take_field
    assert by_key[("way", 10)]["decision"] == "take_field"
    assert by_key[("way", 10)]["l"]["payload"] == [1, 3, 4, 1]
    # no content conflict anywhere, yet the combination is broken
    assert plan["unresolved_conflicts"] == []
    gaps = plan["reference_gaps"]
    assert any(
        g["from"]["type"] == "way" and g["from"]["id"] == 10
        and g["missing"]["type"] == "node" and g["missing"]["id"] == 3
        for g in gaps
    )
    # the preview assembles the candidate and shows the affected relation
    preview = osmface.merge_preview(db, plan["plan_id"])
    assert preview["complete"] is True
    assert preview["relations"]["100"]["status"] == "incomplete"
    reason = preview["relations"]["100"]["reason"]
    assert reason["code"] == "missing_references"
    assert any(g["kind"] == "node" and g["id"] == 3
               for g in reason["gaps"])


def test_custom_repair_lands_exactly_what_preview_showed(tmp_path):
    db, pkg = _setup(tmp_path)
    plan = osmface.create_merge_plan(db, pkg)
    # repair: way 10 uses the field's new local node instead of deleted 3
    osmface.save_merge_rulings(db, plan["plan_id"], [
        {"type": "way", "id": 10, "choice": "custom",
         "entity": {"type": "way", "id": 10,
                    "nodes": [1, 2, local("n1"), 4, 1]},
         "reason": "node 3 was deleted on main; use the field's "
                   "replacement corner"},
    ])
    plan2 = osmface.get_merge_plan(db, plan["plan_id"])
    w10 = [d for d in plan2["decisions"]
           if (d["type"], d["id"]) == ("way", 10)][0]
    # the plan shows the automatic result AND the override with its reason
    assert w10["decision"] == "take_field"
    assert w10["ruling"]["choice"] == "custom"
    assert w10["ruling"]["reason"]
    preview = osmface.merge_preview(db, plan["plan_id"])
    assert preview["complete"] is True
    assert preview["relations"]["100"]["status"] == "complete"
    preview_area = shape(
        preview["relations"]["100"]["faces"][0]["geojson"]).area
    assert preview_area == 100.0
    receipt = osmface.confirm_merge(db, plan["plan_id"], "m-repair")
    assert receipt["result_revision"] == 2
    new_node_id = receipt["allocation"]["node"]["n1"]
    # the landed warehouse matches the preview exactly
    landed = osmface.inspect_database(db, relation_id=100, revision=2)
    assert landed["relation"]["status"] == "complete"
    assert shape(landed["relation"]["faces"][0]["geometry"]).area == 100.0
    seg = landed["relation"]["faces"][0]["rings"][0]["segments"][0]
    assert seg["way_definition_nodes"] == [1, 2, new_node_id, 4, 1]
    # the old revision is still viewable, with its old (broken) state
    old = osmface.inspect_database(db, relation_id=100, revision=1)
    assert old["relation"]["status"] == "incomplete"
    older = osmface.inspect_database(db, relation_id=100, revision=0)
    assert older["relation"]["status"] == "complete"
    # versions: way 10 went 1 -> 2 on the warehouse's own version line
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT version, payload FROM entity_versions WHERE type='way'"
        " AND id=10"
    ).fetchone()
    conn.close()
    assert row[0] == 2
    assert json.loads(row[1]) == [1, 2, new_node_id, 4, 1]


def test_gap_diagnostics_are_not_entity_conflicts(tmp_path):
    db, pkg = _setup(tmp_path)
    plan = osmface.create_merge_plan(db, pkg)
    # a reference gap never masquerades as a content conflict
    assert plan["unresolved_conflicts"] == []
    # confirming with the gap still open is allowed (legal raw objects
    # land as incomplete), and the stale face is not exposed anymore
    receipt = osmface.confirm_merge(db, plan["plan_id"], "m-gap")
    assert receipt["result_revision"] == 2
    landed = osmface.inspect_database(db, relation_id=100, revision=2)
    assert landed["relation"]["status"] == "incomplete"
    assert landed["relation"]["faces"] == []

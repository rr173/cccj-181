"""Merge plans: three-way decisions, rulings, candidate preview,
confirmation, idempotent retries and receipts."""
import json
import os
import shutil
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
    rel,
    way,
    wm,
    write_jsonl,
)
from merge_util import edit_file, local, lnode, lrel, lway, lm, rulings_file


def _warehouse_with_updates(tmp_path):
    """Warehouse at revision 1: node 2 moved, node 21 added (unused)."""
    db, _ = build_warehouse(tmp_path, osmface)
    osmface.apply_changes(db, change_file(
        tmp_path, "main1.jsonl", "main1", 0,
        [put(1, node(2, 12, 0)), put(0, node(21, 99, 99))]))
    return db


def _field_copy(tmp_path, db, name="copy.db"):
    copy = str(tmp_path / name)
    osmface.checkout_workcopy(db, copy, revision=0)
    return copy


def _export(tmp_path, copy, name="pkg"):
    pkg = str(tmp_path / name)
    osmface.workcopy_export(copy, pkg)
    return pkg


def test_plan_three_way_decisions(tmp_path):
    db = _warehouse_with_updates(tmp_path)
    copy = _field_copy(tmp_path, db)
    # field: moves node 3 (untouched by main), re-moves node 2 elsewhere,
    # deletes unused way 90, puts node 1 back to its baseline value
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1, "entity": node(3, 10, 14)},
        {"op": "put", "expected_version": 1, "entity": node(2, 55, 0)},
        {"op": "delete", "expected_version": 1, "type": "way", "id": 90},
        {"op": "put", "expected_version": 1, "entity": node(1, 0, 0)},
    ]))
    pkg = _export(tmp_path, copy)
    plan = osmface.create_merge_plan(db, pkg)
    by_key = {(d["type"], d["id"]): d for d in plan["decisions"]}
    # node 3: field changed, main kept baseline -> take_field
    assert by_key[("node", 3)]["decision"] == "take_field"
    # node 2: both moved it differently -> conflict, all three sides shown
    n2 = by_key[("node", 2)]
    assert n2["decision"] == "conflict"
    assert n2["b"]["payload"] == [10, 0]
    assert n2["l"]["payload"] == [55, 0]
    assert n2["h"]["payload"] == [12, 0]
    # way 90: field deleted, main untouched -> take_field (a delete)
    assert by_key[("way", 90)]["decision"] == "take_field"
    assert by_key[("way", 90)]["l"]["state"] == "deleted"
    # node 1: field put the baseline value back -> definition unchanged,
    # so it never becomes a decision at all
    assert ("node", 1) not in by_key
    # node 21: main-side only -> keep_main
    assert by_key[("node", 21)]["decision"] == "keep_main"
    # node 2 versions are evidence, not a conflict trigger
    assert n2["b"]["version"] == 1 and n2["h"]["version"] == 2


def test_same_definition_different_versions_is_not_a_conflict(tmp_path):
    db = _warehouse_with_updates(tmp_path)  # main moved node 2 to (12,0)
    copy = _field_copy(tmp_path, db)
    # field independently moves node 2 to the very same coordinates
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1, "entity": node(2, 12, 0)},
    ]))
    plan = osmface.create_merge_plan(db, _export(tmp_path, copy))
    by_key = {(d["type"], d["id"]): d for d in plan["decisions"]}
    assert by_key[("node", 2)]["decision"] == "same"
    assert plan["unresolved_conflicts"] == []
    # confirming needs no rulings and produces no entity change for it
    receipt = osmface.confirm_merge(db, plan["plan_id"], "m-same")
    changed = {(c["type"], c["id"]) for c in receipt["changes"]}
    assert ("node", 2) not in changed


def test_delete_restore_and_never_existed_are_distinct(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    # main deletes way 90 in rev 1, then rev 2 restores it unchanged
    osmface.apply_changes(db, change_file(
        tmp_path, "del.jsonl", "del", 0, [delete(1, "way", 90)]))
    osmface.apply_changes(db, change_file(
        tmp_path, "res.jsonl", "res", 1,
        [put(2, way(90, [9, 10, 11, 12, 9]))]))
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy, revision=1)  # way 90 is a tombstone
    # field restores way 90 with the same definition main restored it with
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 2,
         "entity": way(90, [9, 10, 11, 12, 9])},
    ]))
    plan = osmface.create_merge_plan(db, _export(tmp_path, copy))
    by_key = {(d["type"], d["id"]): d for d in plan["decisions"]}
    w90 = by_key[("way", 90)]
    # B is a tombstone with version evidence; L and H are the same restore
    assert w90["b"]["state"] == "deleted" and w90["b"]["version"] == 2
    assert w90["decision"] == "same"
    # a tombstone is not "never existed": baseline carries its version
    assert w90["b"]["state"] != "absent"


def test_ordered_members_with_duplicates_are_not_set_compared(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy)
    # field: relation 300 members become [way 90, way 90] (duplicate kept)
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1,
         "entity": rel(300, [wm(90), wm(90)])},
    ]))
    # main: relation 300 members reordered/duplicated differently
    osmface.apply_changes(db, change_file(
        tmp_path, "m.jsonl", "m", 0,
        [put(1, rel(300, [wm(90), wm(90), wm(90)]))]))
    plan = osmface.create_merge_plan(db, _export(tmp_path, copy))
    by_key = {(d["type"], d["id"]): d for d in plan["decisions"]}
    r300 = by_key[("relation", 300)]
    # two occurrences vs three: a set comparison would call them equal
    assert r300["decision"] == "conflict"
    assert [m["ref"] for m in r300["l"]["payload"]] == [90, 90]
    assert [m["ref"] for m in r300["h"]["payload"]] == [90, 90, 90]
    # rule it for the field side and confirm: exactly two occurrences land
    osmface.save_merge_rulings(db, plan["plan_id"], [
        {"type": "relation", "id": 300, "choice": "field"},
    ])
    receipt = osmface.confirm_merge(db, plan["plan_id"], "m-dup")
    detail = osmface.inspect_database(
        db, relation_id=300, revision=receipt["result_revision"])["relation"]
    assert [m["ref"] for m in detail["members"]] == [90, 90]


def test_member_reorder_is_a_definition_change(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy)
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1,
         "entity": rel(300, [wm(90), wm(10)])},
    ]))
    osmface.apply_changes(db, change_file(
        tmp_path, "m.jsonl", "m", 0,
        [put(1, rel(300, [wm(10), wm(90)]))]))
    plan = osmface.create_merge_plan(db, _export(tmp_path, copy))
    by_key = {(d["type"], d["id"]): d for d in plan["decisions"]}
    # same members, different order: a real conflict, never silently merged
    assert by_key[("relation", 300)]["decision"] == "conflict"


def test_wrong_source_and_identical_content_warehouse_rejected(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy)
    osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e.jsonl", "e", 0,
        [{"op": "put", "expected_version": 0, "entity": lnode("n1", 1, 1)}]))
    pkg = _export(tmp_path, copy)
    # a second warehouse with byte-identical content but no common origin
    other = str(tmp_path / "other")
    os.makedirs(other)
    db2, _ = build_warehouse(other, osmface)
    assert osmface.source_identity(db2)["source_id"] != \
        osmface.source_identity(db)["source_id"]
    with pytest.raises(osmface.MergeError):
        osmface.create_merge_plan(db2, pkg)


def test_baseline_mismatch_rejected(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy)
    pkg = _export(tmp_path, copy)
    # tamper with the packaged baseline: revision bump on one row
    base_path = os.path.join(pkg, "baseline.jsonl")
    lines = open(base_path, encoding="utf-8").read().splitlines()
    row = json.loads(lines[0])
    row["version"] += 1
    lines[0] = json.dumps(row, sort_keys=True)
    open(base_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    # the manifest digest no longer matches the file -> integrity failure
    with pytest.raises(osmface.PackageError):
        osmface.create_merge_plan(db, pkg)


def test_corrupt_and_incomplete_packages_rejected(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy)
    osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e.jsonl", "e", 0,
        [{"op": "put", "expected_version": 1, "entity": node(2, 12, 0)}]))
    pkg = _export(tmp_path, copy)

    # truncated edits file
    broken = str(tmp_path / "pkg-truncated")
    shutil.copytree(pkg, broken)
    edits_path = os.path.join(broken, "edits.jsonl")
    data = open(edits_path, "rb").read()
    open(edits_path, "wb").write(data[: max(1, len(data) - 3)])
    with pytest.raises(osmface.PackageError):
        osmface.create_merge_plan(db, broken)

    # missing file
    incomplete = str(tmp_path / "pkg-incomplete")
    shutil.copytree(pkg, incomplete)
    os.remove(os.path.join(incomplete, "baseline.jsonl"))
    with pytest.raises(osmface.PackageError):
        osmface.create_merge_plan(db, incomplete)

    # forged provenance string (digests left as they were)
    forged = str(tmp_path / "pkg-forged")
    shutil.copytree(pkg, forged)
    prov_path = os.path.join(forged, "provenance.json")
    prov = json.load(open(prov_path, encoding="utf-8"))
    prov["source_id"] = "0" * 32
    json.dump(prov, open(prov_path, "w", encoding="utf-8"),
              indent=2, sort_keys=True)
    with pytest.raises(osmface.PackageError):
        osmface.create_merge_plan(db, forged)

    # manifest pointing outside the package directory
    escaped = str(tmp_path / "pkg-escaped")
    shutil.copytree(pkg, escaped)
    man_path = os.path.join(escaped, "manifest.json")
    man = json.load(open(man_path, encoding="utf-8"))
    man["files"].append({"path": "../../etc/passwd", "bytes": 1,
                         "sha256": "0" * 64})
    json.dump(man, open(man_path, "w", encoding="utf-8"))
    with pytest.raises(osmface.PackageError):
        osmface.create_merge_plan(db, escaped)


def test_duplicate_identity_in_package_rejected(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy)
    pkg = _export(tmp_path, copy)
    # duplicate a baseline row and fix up the manifest digests honestly:
    # the package must still be rejected for the duplicate identity
    base_path = os.path.join(pkg, "baseline.jsonl")
    lines = open(base_path, encoding="utf-8").read().splitlines()
    lines.append(lines[0])
    data = ("\n".join(lines) + "\n").encode("utf-8")
    open(base_path, "wb").write(data)
    import hashlib
    man_path = os.path.join(pkg, "manifest.json")
    man = json.load(open(man_path, encoding="utf-8"))
    for entry in man["files"]:
        if entry["path"] == "baseline.jsonl":
            entry["bytes"] = len(data)
            entry["sha256"] = hashlib.sha256(data).hexdigest()
    json.dump(man, open(man_path, "w", encoding="utf-8"))
    with pytest.raises(osmface.PackageError):
        osmface.create_merge_plan(db, pkg)


def test_unruled_plan_cannot_confirm(tmp_path):
    db = _warehouse_with_updates(tmp_path)
    copy = _field_copy(tmp_path, db)
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1, "entity": node(2, 55, 0)},
    ]))
    plan = osmface.create_merge_plan(db, _export(tmp_path, copy))
    assert plan["unresolved_conflicts"] == [{"type": "node", "id": 2}]
    with pytest.raises(osmface.MergeError):
        osmface.confirm_merge(db, plan["plan_id"], "m1")
    # preview refuses to claim a committable candidate
    preview = osmface.merge_preview(db, plan["plan_id"])
    assert preview["complete"] is False
    # nothing landed
    assert osmface.inspect_database(db)["revision"] == 1


def test_head_moved_after_rulings_requires_new_plan(tmp_path):
    db = _warehouse_with_updates(tmp_path)
    copy = _field_copy(tmp_path, db)
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1, "entity": node(2, 55, 0)
         if False else node(2, 55, 0)},
    ]))
    plan = osmface.create_merge_plan(db, _export(tmp_path, copy))
    osmface.save_merge_rulings(db, plan["plan_id"], [
        {"type": "node", "id": 2, "choice": "field"},
    ])
    # main advances after the rulings were saved
    osmface.apply_changes(db, change_file(
        tmp_path, "main2.jsonl", "main2", 1, [put(1, node(4, 0, 44))]))
    with pytest.raises(osmface.MergeConflict):
        osmface.confirm_merge(db, plan["plan_id"], "m1")
    # the old rulings are not silently applied to the new head
    detail = osmface.inspect_database(db, relation_id=100, revision=2)
    assert detail["relation"]["object_version"] == 1
    conn = sqlite3.connect(db)
    payload = conn.execute(
        "SELECT payload FROM entity_versions WHERE type='node' AND id=2"
    ).fetchone()[0]
    conn.close()
    assert json.loads(payload) == [12, 0]  # main's value, not the field's


def test_confirm_allocates_and_rewrites_all_reference_levels(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    # main owns negative ids and a deleted positive id in the node domain
    osmface.apply_changes(db, change_file(
        tmp_path, "neg.jsonl", "neg", 0,
        [put(0, node(-7, 500, 500)), put(0, node(50, 3, 3))]))
    osmface.apply_changes(db, change_file(
        tmp_path, "del.jsonl", "del", 1, [delete(1, "node", 50)]))
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy, revision=0)
    # field builds a nested structure: r2 -> r1 -> w1 -> n1..n4
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 0, "entity": lnode("n1", 0, 20)},
        {"op": "put", "expected_version": 0, "entity": lnode("n2", 4, 20)},
        {"op": "put", "expected_version": 0, "entity": lnode("n3", 4, 24)},
        {"op": "put", "expected_version": 0, "entity": lnode("n4", 0, 24)},
        {"op": "put", "expected_version": 0,
         "entity": lway("w1", [local("n1"), local("n2"), local("n3"),
                               local("n4"), local("n1")])},
        {"op": "put", "expected_version": 0, "entity": lrel("r1", [lm("w1")])},
        {"op": "put", "expected_version": 0,
         "entity": lrel("r2", [{"type": "relation", "ref": local("r1"),
                                "role": "outer"}])},
    ]))
    plan = osmface.create_merge_plan(db, _export(tmp_path, copy))
    receipt = osmface.confirm_merge(db, plan["plan_id"], "m-alloc")
    alloc = receipt["allocation"]
    # every local object got one permanent id, above every id ever used
    # (node 50's tombstone and the negative id -7 stay occupied)
    assert set(alloc["node"].values()) == {51, 52, 53, 54}
    assert alloc["way"]["w1"] == 91
    assert alloc["relation"] == {"r1": 401, "r2": 402}
    # negative ids and the tombstoned id 50 stay untouched
    conn = sqlite3.connect(db)
    rows = dict(conn.execute(
        "SELECT id, payload FROM entity_versions WHERE type='node'"))
    conn.close()
    assert json.loads(rows[-7]) == [500, 500]
    assert 50 in rows  # tombstone id was not reused
    # references at every level point at the permanent ids
    way_payload = json.loads(conn.execute(
        "SELECT payload FROM entity_versions WHERE type='way' AND id=91"
    ).fetchone()[0]) if False else None
    detail = osmface.inspect_database(db, relation_id=402)["relation"]
    assert detail["status"] == "complete"
    assert detail["members"] == [
        {"type": "relation", "ref": 401, "role": "outer"}
    ]
    inner = osmface.inspect_database(db, relation_id=401)["relation"]
    assert inner["members"] == [{"type": "way", "ref": 91, "role": "outer"}]
    seg = inner["faces"][0]["rings"][0]["segments"][0]
    assert seg["way"] == 91
    assert seg["way_definition_nodes"] == [51, 52, 53, 54, 51]
    # final area comes from the assembled face
    assert shape(inner["faces"][0]["geometry"]).area == 16.0


def test_two_copies_same_local_names_get_different_ids(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    copies = []
    for name in ("a.db", "b.db"):
        copy = str(tmp_path / name)
        osmface.checkout_workcopy(db, copy)
        osmface.workcopy_edit(copy, edit_file(
            tmp_path, f"e-{name}.jsonl", "e", 0,
            [{"op": "put", "expected_version": 0,
              "entity": lnode("n1", 7, 7)}]))
        copies.append(copy)
    # same coordinates on purpose: identity must never merge by content
    pkg_a = _export(tmp_path, copies[0], "pkg-a")
    pkg_b = _export(tmp_path, copies[1], "pkg-b")
    plan_a = osmface.create_merge_plan(db, pkg_a)
    receipt_a = osmface.confirm_merge(db, plan_a["plan_id"], "m-a")
    plan_b = osmface.create_merge_plan(db, pkg_b)
    receipt_b = osmface.confirm_merge(db, plan_b["plan_id"], "m-b")
    id_a = receipt_a["allocation"]["node"]["n1"]
    id_b = receipt_b["allocation"]["node"]["n1"]
    assert id_a != id_b
    conn = sqlite3.connect(db)
    payloads = {
        r[0]: json.loads(r[1]) for r in conn.execute(
            "SELECT id, payload FROM entity_versions WHERE type='node'"
            " AND id IN (?, ?)", (id_a, id_b))
    }
    conn.close()
    assert payloads[id_a] == [7, 7] and payloads[id_b] == [7, 7]


def test_merge_receipt_idempotent_and_cross_checked(tmp_path):
    db = _warehouse_with_updates(tmp_path)
    copy = _field_copy(tmp_path, db)
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1, "entity": node(3, 10, 14)},
    ]))
    plan = osmface.create_merge_plan(db, _export(tmp_path, copy))
    first = osmface.confirm_merge(db, plan["plan_id"], "m1")
    # main advances afterwards; resending the same merge must find the
    # original receipt, not re-allocate or re-apply
    osmface.apply_changes(db, change_file(
        tmp_path, "later.jsonl", "later", 2, [put(1, node(4, 0, 44))]))
    again = osmface.confirm_merge(db, plan["plan_id"], "m1")
    assert again == first
    assert again["result_revision"] == first["result_revision"] == 2
    assert osmface.inspect_database(db)["revision"] == 3
    # same merge_id with a different package is rejected
    copy2 = _field_copy(tmp_path, db, "copy2.db")
    osmface.workcopy_edit(copy2, edit_file(tmp_path, "e2.jsonl", "e2", 0, [
        {"op": "put", "expected_version": 1, "entity": node(4, 1, 1)},
    ]))
    plan2 = osmface.create_merge_plan(db, _export(tmp_path, copy2, "pkg2"))
    with pytest.raises(osmface.MergeConflict):
        osmface.confirm_merge(db, plan2["plan_id"], "m1")
    # the receipt is queryable and traces back to locals and baseline
    receipt = osmface.get_merge_receipt(db, "m1")
    assert receipt["plan_id"] == plan["plan_id"]
    assert receipt["base_revision"] == 0
    assert receipt["head_revision"] == 1
    assert receipt["baseline_versions"]["node:3"] == 1


def test_merge_does_not_confuse_batch_namespace(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy)
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1, "entity": node(3, 10, 14)},
    ]))
    plan = osmface.create_merge_plan(db, _export(tmp_path, copy))
    receipt = osmface.confirm_merge(db, plan["plan_id"], "shared-id")
    assert receipt["result_revision"] == 1
    # ordinary apply keeps working on the merged head; a batch_id equal to
    # a merge_id is a different namespace and is not mistaken for a resend
    direct = osmface.apply_changes(db, change_file(
        tmp_path, "direct.jsonl", "shared-id", 1, [put(1, node(4, 0, 44))]))
    assert direct["revision"] == 2
    assert direct["batch_id"] == "shared-id"
    # the merge receipt is still the merge's own
    assert osmface.get_merge_receipt(db, "shared-id")["kind"] == "merge"
    # revision identities distinguish the two kinds
    info = osmface.inspect_database(db, revision=1)
    assert info["revision_identity"]["kind"] == "merge"
    info2 = osmface.inspect_database(db, revision=2)
    assert info2["revision_identity"]["kind"] == "batch"


def test_no_entity_change_means_no_empty_revision(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy)
    # field re-puts the exact baseline definition of node 2
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1, "entity": node(2, 10, 0)},
    ]))
    plan = osmface.create_merge_plan(db, _export(tmp_path, copy))
    receipt = osmface.confirm_merge(db, plan["plan_id"], "m-noop")
    assert receipt["result_revision"] == 0
    assert receipt["changes"] == []
    assert osmface.inspect_database(db)["revision"] == 0
    # the merge is still recorded and retryable
    assert osmface.get_merge_receipt(db, "m-noop")["result_revision"] == 0


def test_plan_is_immutable_for_later_packages(tmp_path):
    db = _warehouse_with_updates(tmp_path)
    copy = _field_copy(tmp_path, db)
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1, "entity": node(3, 10, 14)},
    ]))
    pkg1 = _export(tmp_path, copy, "pkg1")
    plan1 = osmface.create_merge_plan(db, pkg1)
    # the copy moves on and exports another package
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e2.jsonl", "e2", 1, [
        {"op": "put", "expected_version": 2, "entity": node(3, 10, 15)},
    ]))
    pkg2 = _export(tmp_path, copy, "pkg2")
    plan2 = osmface.create_merge_plan(db, pkg2)
    # the old plan still pins the old package content
    old = osmface.get_merge_plan(db, plan1["plan_id"])
    new = osmface.get_merge_plan(db, plan2["plan_id"])
    assert old["package_id"] != new["package_id"]
    old_n3 = [d for d in old["decisions"]
              if (d["type"], d["id"]) == ("node", 3)][0]
    assert old_n3["l"]["payload"] == [10, 14]
    new_n3 = [d for d in new["decisions"]
              if (d["type"], d["id"]) == ("node", 3)][0]
    assert new_n3["l"]["payload"] == [10, 15]


def test_unknown_or_foreign_rulings_rejected(tmp_path):
    db = _warehouse_with_updates(tmp_path)
    copy = _field_copy(tmp_path, db)
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1, "entity": node(2, 55, 0)},
    ]))
    plan = osmface.create_merge_plan(db, _export(tmp_path, copy))
    with pytest.raises(osmface.MergeError):
        osmface.save_merge_rulings(db, plan["plan_id"], [
            {"type": "node", "id": 4242, "choice": "field"},
        ])
    # a non-conflict object cannot be "ruled" to a side, only overridden
    with pytest.raises(osmface.MergeError):
        osmface.save_merge_rulings(db, plan["plan_id"], [
            {"type": "node", "id": 21, "choice": "main"},
        ])


def test_custom_ruling_validated(tmp_path):
    db = _warehouse_with_updates(tmp_path)
    copy = _field_copy(tmp_path, db)
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1, "entity": node(2, 55, 0)},
    ]))
    plan = osmface.create_merge_plan(db, _export(tmp_path, copy))
    # custom definition for a different object must not sneak in
    with pytest.raises(osmface.MergeError):
        osmface.save_merge_rulings(db, plan["plan_id"], [
            {"type": "node", "id": 2, "choice": "custom",
             "entity": node(3, 1, 1)},
        ])
    # malformed custom definitions are rejected
    with pytest.raises(osmface.MergeError):
        osmface.save_merge_rulings(db, plan["plan_id"], [
            {"type": "node", "id": 2, "choice": "custom",
             "entity": {"type": "node", "id": 2, "x": "a", "y": 1}},
        ])
    # a valid custom definition lands
    osmface.save_merge_rulings(db, plan["plan_id"], [
        {"type": "node", "id": 2, "choice": "custom",
         "entity": node(2, 33, 0)},
    ])
    receipt = osmface.confirm_merge(db, plan["plan_id"], "m-custom")
    conn = sqlite3.connect(db)
    payload = conn.execute(
        "SELECT payload FROM entity_versions WHERE type='node' AND id=2"
    ).fetchone()[0]
    conn.close()
    assert json.loads(payload) == [33, 0]


def test_package_baseline_beyond_head_rejected(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy)
    pkg = _export(tmp_path, copy)
    # forge a package whose baseline revision does not exist yet: repair the
    # provenance digests honestly so only the revision check can fire
    prov_path = os.path.join(pkg, "provenance.json")
    prov = json.load(open(prov_path, encoding="utf-8"))
    prov["base_revision"] = 99
    json.dump(prov, open(prov_path, "w", encoding="utf-8"),
              indent=2, sort_keys=True)
    # manifest binds provenance by digest: refresh it, and package_id
    import hashlib
    data = open(prov_path, "rb").read()
    man_path = os.path.join(pkg, "manifest.json")
    man = json.load(open(man_path, encoding="utf-8"))
    for entry in man["files"]:
        if entry["path"] == "provenance.json":
            entry["bytes"] = len(data)
            entry["sha256"] = hashlib.sha256(data).hexdigest()
    man["package_id"] = hashlib.sha256(json.dumps({
        "format": man["format"], "provenance": prov,
        "files": man["files"],
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    json.dump(man, open(man_path, "w", encoding="utf-8"))
    with pytest.raises(osmface.MergeError):
        osmface.create_merge_plan(db, pkg)


def test_deleted_local_objects_leave_no_trace(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy)
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e1.jsonl", "e1", 0, [
        {"op": "put", "expected_version": 0, "entity": lnode("n1", 1, 1)},
        {"op": "put", "expected_version": 0, "entity": lnode("n2", 2, 2)},
    ]))
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e2.jsonl", "e2", 1, [
        {"op": "delete", "expected_version": 1, "type": "node",
         "local_id": "n1"},
    ]))
    pkg = _export(tmp_path, copy)
    from osmface.merge import read_package
    reopened = read_package(pkg)
    # only the surviving local is in the intake set
    assert [l[1] for l in reopened.locals_alive] == ["n2"]
    plan = osmface.create_merge_plan(db, pkg)
    receipt = osmface.confirm_merge(db, plan["plan_id"], "m-nodel")
    assert receipt["allocation"] == {"node": {"n2": 13}}
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT id FROM entity_versions WHERE type='node' AND id >= 13"
    ).fetchall()
    conn.close()
    assert [r[0] for r in rows] == [13]


def test_empty_package_confirms_as_noop(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy)
    pkg = _export(tmp_path, copy)  # no edits at all
    plan = osmface.create_merge_plan(db, pkg)
    assert plan["decisions"] == []
    assert plan["locals"] == []
    receipt = osmface.confirm_merge(db, plan["plan_id"], "m-empty")
    assert receipt["result_revision"] == 0
    assert receipt["changes"] == []
    assert osmface.inspect_database(db)["revision"] == 0


def test_plan_summary_groups(tmp_path):
    db = _warehouse_with_updates(tmp_path)
    copy = _field_copy(tmp_path, db)
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1, "entity": node(3, 10, 14)},
        {"op": "put", "expected_version": 1, "entity": node(2, 55, 0)},
    ]))
    plan = osmface.create_merge_plan(db, _export(tmp_path, copy))
    summary = plan["summary"]
    assert summary["take_field"] == [{"type": "node", "id": 3}]
    assert summary["conflicts"] == [{"type": "node", "id": 2}]
    assert {"type": "node", "id": 21} in summary["keep_main"]


def test_duplicate_ruling_in_one_call_rejected(tmp_path):
    db = _warehouse_with_updates(tmp_path)
    copy = _field_copy(tmp_path, db)
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1, "entity": node(2, 55, 0)},
    ]))
    plan = osmface.create_merge_plan(db, _export(tmp_path, copy))
    with pytest.raises(osmface.MergeError):
        osmface.save_merge_rulings(db, plan["plan_id"], [
            {"type": "node", "id": 2, "choice": "field"},
            {"type": "node", "id": 2, "choice": "main"},
        ])
    # the rejected call saved nothing
    assert osmface.get_merge_plan(db, plan["plan_id"])[
        "unresolved_conflicts"] == [{"type": "node", "id": 2}]


def test_ruling_file_with_clear_choice(tmp_path):
    db = _warehouse_with_updates(tmp_path)
    copy = _field_copy(tmp_path, db)
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1, "entity": node(2, 55, 0)},
    ]))
    plan = osmface.create_merge_plan(db, _export(tmp_path, copy))
    osmface.save_merge_rulings(db, plan["plan_id"], [
        {"type": "node", "id": 2, "choice": "field"},
    ])
    assert osmface.get_merge_plan(db, plan["plan_id"])[
        "unresolved_conflicts"] == []
    # clearing a ruling re-opens the conflict
    osmface.save_merge_rulings(db, plan["plan_id"], [
        {"type": "node", "id": 2, "choice": "clear"},
    ])
    assert osmface.get_merge_plan(db, plan["plan_id"])[
        "unresolved_conflicts"] == [{"type": "node", "id": 2}]


def test_same_merge_id_with_different_rulings_rejected(tmp_path):
    db = _warehouse_with_updates(tmp_path)
    copy = _field_copy(tmp_path, db)
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1, "entity": node(2, 55, 0)},
    ]))
    pkg = _export(tmp_path, copy)
    plan = osmface.create_merge_plan(db, pkg)
    osmface.save_merge_rulings(db, plan["plan_id"], [
        {"type": "node", "id": 2, "choice": "field"},
    ])
    receipt = osmface.confirm_merge(db, plan["plan_id"], "m-rulings")
    assert receipt["result_revision"] == 2
    # a fresh plan on the new head with a different ruling set, presented
    # under the already-used merge_id, is rejected
    plan2 = osmface.create_merge_plan(db, pkg)
    assert plan2["plan_id"] != plan["plan_id"]  # head moved -> new plan
    osmface.save_merge_rulings(db, plan2["plan_id"], [
        {"type": "node", "id": 2, "choice": "custom",
         "entity": node(2, 77, 0), "reason": "trying to reland differently"},
    ])
    with pytest.raises(osmface.MergeConflict):
        osmface.confirm_merge(db, plan2["plan_id"], "m-rulings")
    # and the original ruling is what actually landed
    conn = sqlite3.connect(db)
    payload = conn.execute(
        "SELECT payload FROM entity_versions WHERE type='node' AND id=2"
    ).fetchone()[0]
    conn.close()
    assert json.loads(payload) == [55, 0]

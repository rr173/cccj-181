"""Offline work copies: checkout provenance, independent edit/preview,
edit-batch semantics, local identities, package export integrity."""
import json
import os
import shutil
import sqlite3

import pytest

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
from merge_util import edit_file, local, lnode, lrel, lway, lm


def _checkout(tmp_path, osmface, name="copy.db", revision=None):
    db, _ = build_warehouse(tmp_path, osmface)
    copy = str(tmp_path / name)
    osmface.checkout_workcopy(db, copy, revision=revision)
    return db, copy


def test_checkout_records_provenance_and_snapshot(tmp_path):
    db, copy = _checkout(tmp_path, osmface)
    info = osmface.workcopy_info(copy)
    main = osmface.source_identity(db)
    assert info["source_id"] == main["source_id"]
    assert info["base_revision"] == 0
    assert info["edit_head"] == 0
    assert info["counts"] == {"node": 8, "way": 2, "relation": 4}
    # tombstones and versions come along: delete way 90 on main, checkout
    # at the new revision must carry the tombstone with its version
    osmface.apply_changes(db, change_file(
        tmp_path, "del.jsonl", "del", 0, [delete(1, "way", 90)]))
    copy2 = str(tmp_path / "copy2.db")
    osmface.checkout_workcopy(db, copy2)
    conn = sqlite3.connect(copy2)
    row = conn.execute(
        "SELECT version, deleted, payload FROM baseline_entities"
        " WHERE type='way' AND id=90"
    ).fetchone()
    conn.close()
    assert row == (2, 1, None)


def test_checkout_pins_given_revision(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    osmface.apply_changes(db, change_file(
        tmp_path, "mv.jsonl", "mv", 0, [put(1, node(2, 12, 0))]))
    copy = str(tmp_path / "copy.db")
    summary = osmface.checkout_workcopy(db, copy, revision=0)
    assert summary["base_revision"] == 0
    # the copy holds the revision-0 coordinates, not the moved ones
    conn = sqlite3.connect(copy)
    payload = conn.execute(
        "SELECT payload FROM wc_entities WHERE type='node' AND key='2'"
    ).fetchone()[0]
    conn.close()
    assert json.loads(payload) == [10, 0]


def test_checkout_copies_snapshot_not_history(tmp_path):
    db, copy = _checkout(tmp_path, osmface)
    conn = sqlite3.connect(copy)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    # no warehouse history tables, no assemble results, no exports
    assert "entity_history" not in tables
    assert "rel_result_history" not in tables
    assert "faces" not in tables
    assert {"meta", "baseline_entities", "wc_entities",
            "edit_registry"} <= tables


def test_source_identity_is_stable_across_processes(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    first = osmface.source_identity(db)["source_id"]
    # repeated checkouts and fresh connections must not change it
    osmface.checkout_workcopy(db, str(tmp_path / "a.db"))
    osmface.checkout_workcopy(db, str(tmp_path / "b.db"))
    again = osmface.source_identity(db)["source_id"]
    assert first == again
    # an older warehouse (enabled before source ids existed) obtains one
    # lazily and then keeps it
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM meta WHERE key = 'source_id'")
    conn.commit()
    conn.close()
    minted = osmface.source_identity(db)["source_id"]
    assert minted and minted != first
    assert osmface.source_identity(db)["source_id"] == minted


def test_copy_works_after_main_disappears(tmp_path):
    db, copy = _checkout(tmp_path, osmface)
    # disconnect the warehouse: the copy must not sneak back to the path
    moved = str(tmp_path / "main-moved.db")
    shutil.move(db, moved)
    receipt = osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e1.jsonl", "e1", 0,
        [{"op": "put", "expected_version": 0, "entity": lnode("n1", 5, 5)}]))
    assert receipt["edit_head"] == 1
    preview = osmface.workcopy_preview(copy)
    assert preview["edit_head"] == 1
    # main head untouched by offline edits (still reachable at moved path)
    assert osmface.inspect_database(moved)["revision"] == 0


def test_edit_local_objects_and_forward_references(tmp_path):
    db, copy = _checkout(tmp_path, osmface)
    # one batch: a way referencing a node created later in the same batch
    receipt = osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e1.jsonl", "e1", 0, [
            {"op": "put", "expected_version": 0,
             "entity": lway("w1", [local("n1"), local("n2"),
                                   local("n3"), local("n1")])},
            {"op": "put", "expected_version": 0, "entity": lnode("n1", 0, 0)},
            {"op": "put", "expected_version": 0, "entity": lnode("n2", 4, 0)},
            {"op": "put", "expected_version": 0, "entity": lnode("n3", 0, 4)},
            {"op": "put", "expected_version": 0,
             "entity": lrel("r1", [lm("w1")])},
        ]))
    assert receipt["edit_head"] == 1
    preview = osmface.workcopy_preview(copy, relation_id="r1")
    assert preview["relation"]["status"] == "complete"
    # local identities are visible as local names, not integers
    ring_nodes = preview["relation"]["faces"][0]["rings"][0]["nodes"]
    assert "n1" in ring_nodes and 1 not in ring_nodes


def test_edit_existing_and_repeated_local_edits(tmp_path):
    db, copy = _checkout(tmp_path, osmface)
    osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e1.jsonl", "e1", 0,
        [{"op": "put", "expected_version": 0, "entity": lnode("n1", 1, 1)}]))
    # modify the local object again, then delete it in a later batch
    osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e2.jsonl", "e2", 1,
        [{"op": "put", "expected_version": 1, "entity": lnode("n1", 2, 2)}]))
    receipt = osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e3.jsonl", "e3", 2,
        [{"op": "delete", "expected_version": 2, "type": "node",
          "local_id": "n1"}]))
    entry = receipt["changes"][0]
    assert entry["old_version"] == 2 and entry["new_version"] == 3
    assert entry["tombstone"] is True
    # modify an existing warehouse object by integer id
    receipt = osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e4.jsonl", "e4", 3,
        [{"op": "put", "expected_version": 1, "entity": node(2, 15, 0)}]))
    assert receipt["changes"][0]["old_version"] == 1
    assert receipt["changes"][0]["new_version"] == 2


def test_stale_base_edit_rejected_and_head_not_overwritten(tmp_path):
    db, copy = _checkout(tmp_path, osmface)
    osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e1.jsonl", "e1", 0,
        [{"op": "put", "expected_version": 0, "entity": lnode("n1", 1, 1)}]))
    # a stale head must not overwrite the edits already written
    with pytest.raises(osmface.WorkcopyConflict):
        osmface.workcopy_edit(copy, edit_file(
            tmp_path, "e2.jsonl", "e2", 0,
            [{"op": "put", "expected_version": 0,
              "entity": lnode("n2", 2, 2)}]))
    info = osmface.workcopy_info(copy)
    assert info["edit_head"] == 1
    assert info["local_counts"]["node"] == 1


def test_edit_id_resend_and_conflict(tmp_path):
    db, copy = _checkout(tmp_path, osmface)
    path = edit_file(tmp_path, "e1.jsonl", "e1", 0,
                     [{"op": "put", "expected_version": 0,
                       "entity": lnode("n1", 1, 1)}])
    first = osmface.workcopy_edit(copy, path)
    resent = osmface.workcopy_edit(copy, path)
    assert resent == first
    assert osmface.workcopy_info(copy)["edit_head"] == 1
    # same edit_id with different bytes is a conflict
    other = edit_file(tmp_path, "e2.jsonl", "e1", 0,
                      [{"op": "put", "expected_version": 0,
                        "entity": lnode("n9", 9, 9)}])
    with pytest.raises(osmface.WorkcopyConflict):
        osmface.workcopy_edit(copy, other)


def test_failed_batch_leaves_no_partial_state(tmp_path):
    db, copy = _checkout(tmp_path, osmface)
    # second row breaks the expected_version of the same object
    bad = edit_file(tmp_path, "bad.jsonl", "bad", 0, [
        {"op": "put", "expected_version": 0, "entity": lnode("n1", 1, 1)},
        {"op": "put", "expected_version": 5, "entity": node(2, 3, 3)},
    ])
    with pytest.raises(osmface.WorkcopyConflict):
        osmface.workcopy_edit(copy, bad)
    info = osmface.workcopy_info(copy)
    assert info["edit_head"] == 0
    assert info["local_counts"]["node"] == 0
    # the failed edit_id was not consumed: fixed bytes may retry
    good = edit_file(tmp_path, "good.jsonl", "bad", 0, [
        {"op": "put", "expected_version": 0, "entity": lnode("n1", 1, 1)},
        {"op": "put", "expected_version": 1, "entity": node(2, 3, 3)},
    ])
    assert osmface.workcopy_edit(copy, good)["edit_head"] == 1


def test_local_references_must_resolve_at_batch_end(tmp_path):
    db, copy = _checkout(tmp_path, osmface)
    # way references a local node that never gets defined
    bad = edit_file(tmp_path, "dangling.jsonl", "dangling", 0, [
        {"op": "put", "expected_version": 0,
         "entity": lway("w1", [local("n1"), 1, 2])},
    ])
    with pytest.raises(osmface.WorkcopyError):
        osmface.workcopy_edit(copy, bad)
    # deleting a local node still referenced by a live local way fails too
    osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e1.jsonl", "e1", 0, [
            {"op": "put", "expected_version": 0, "entity": lnode("n1", 0, 0)},
            {"op": "put", "expected_version": 0, "entity": lnode("n2", 1, 0)},
            {"op": "put", "expected_version": 0,
             "entity": lway("w1", [local("n1"), local("n2")])},
        ]))
    with pytest.raises(osmface.WorkcopyError):
        osmface.workcopy_edit(copy, edit_file(
            tmp_path, "e2.jsonl", "e2", 1,
            [{"op": "delete", "expected_version": 1, "type": "node",
              "local_id": "n1"}]))


def test_cannot_create_integer_ids_offline(tmp_path):
    db, copy = _checkout(tmp_path, osmface)
    with pytest.raises(osmface.WorkcopyError):
        osmface.workcopy_edit(copy, edit_file(
            tmp_path, "e1.jsonl", "e1", 0,
            [{"op": "put", "expected_version": 0,
              "entity": node(999, 1, 1)}]))


def test_local_names_never_confused_with_integer_ids(tmp_path):
    db, copy = _checkout(tmp_path, osmface)
    # "12" is not a valid local name (would collide with integer id 12)
    with pytest.raises(osmface.WorkcopyError):
        osmface.workcopy_edit(copy, edit_file(
            tmp_path, "e1.jsonl", "e1", 0,
            [{"op": "put", "expected_version": 0,
              "entity": lnode("12", 1, 1)}]))
    # negative-integer-looking names are rejected too
    with pytest.raises(osmface.WorkcopyError):
        osmface.workcopy_edit(copy, edit_file(
            tmp_path, "e2.jsonl", "e2", 0,
            [{"op": "put", "expected_version": 0,
              "entity": lnode("-5", 1, 1)}]))


def test_preview_shows_incomplete_for_dangling_main_refs(tmp_path):
    db, copy = _checkout(tmp_path, osmface)
    # delete node 1 offline: relation 100's ring loses a corner
    osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e1.jsonl", "e1", 0,
        [{"op": "delete", "expected_version": 1, "type": "node", "id": 1}]))
    preview = osmface.workcopy_preview(copy)
    assert preview["statuses"]["100"]["status"] == "incomplete"
    gaps = preview["statuses"]["100"]["reason"]["gaps"]
    assert any(g["kind"] == "node" and g["id"] == 1 for g in gaps)
    # the warehouse head did not move and still shows the old state
    assert osmface.inspect_database(db)["revision"] == 0
    rel = osmface.inspect_database(db, relation_id=100)["relation"]
    assert rel["status"] == "complete"


def test_export_package_is_self_contained_and_verified(tmp_path):
    db, copy = _checkout(tmp_path, osmface)
    osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e1.jsonl", "e1", 0, [
            {"op": "put", "expected_version": 0, "entity": lnode("n1", 5, 5)},
            {"op": "put", "expected_version": 1, "entity": node(2, 11, 0)},
        ]))
    pkg = str(tmp_path / "pkg")
    manifest = osmface.workcopy_export(copy, pkg)
    assert manifest["state"] == "complete"
    names = {f["path"] for f in manifest["files"]}
    assert names == {"provenance.json", "baseline.jsonl", "edits.jsonl"}
    # every file is bound by length and a real digest
    for entry in manifest["files"]:
        data = open(os.path.join(pkg, entry["path"]), "rb").read()
        import hashlib
        assert len(data) == entry["bytes"]
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    # reopening the package works with the copy directory deleted
    edits = [json.loads(ln) for ln in
             open(os.path.join(pkg, "edits.jsonl"), encoding="utf-8")
             if ln.strip()]
    kinds = {(op["op"], op["type"]) for op in edits}
    assert ("put", "node") in kinds
    shutil.rmtree(os.path.dirname(copy)) if False else os.remove(copy)
    from osmface.merge import read_package
    reopened = read_package(pkg)
    assert reopened.source_id == osmface.source_identity(db)["source_id"]
    assert reopened.base_revision == 0
    assert reopened.workcopy_head == 1


def test_export_reflects_fixed_head_not_later_edits(tmp_path):
    db, copy = _checkout(tmp_path, osmface)
    osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e1.jsonl", "e1", 0,
        [{"op": "put", "expected_version": 0, "entity": lnode("n1", 5, 5)}]))
    pkg = str(tmp_path / "pkg")
    osmface.workcopy_export(copy, pkg)
    # later edits must not leak into the already-exported package
    osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e2.jsonl", "e2", 1,
        [{"op": "put", "expected_version": 0, "entity": lnode("n2", 6, 6)}]))
    from osmface.merge import read_package
    reopened = read_package(pkg)
    assert reopened.workcopy_head == 1
    assert [l for l in reopened.locals_alive] == [
        ("node", "n1", [5, 5])
    ]


def test_local_names_are_per_type_domains(tmp_path):
    db, copy = _checkout(tmp_path, osmface)
    # the same local name in two type domains is two different objects
    receipt = osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e1.jsonl", "e1", 0, [
            {"op": "put", "expected_version": 0, "entity": lnode("x1", 0, 0)},
            {"op": "put", "expected_version": 0, "entity": lnode("x2", 4, 0)},
            {"op": "put", "expected_version": 0, "entity": lnode("x3", 0, 4)},
            {"op": "put", "expected_version": 0,
             "entity": lway("x1", [local("x1"), local("x2"), local("x3"),
                                   local("x1")])},
        ]))
    assert receipt["edit_head"] == 1
    info = osmface.workcopy_info(copy)
    assert info["local_counts"] == {"node": 3, "way": 1, "relation": 0}


def test_copy_tombstone_restore_keeps_version_line(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    osmface.apply_changes(db, change_file(
        tmp_path, "del.jsonl", "del", 0, [delete(1, "way", 90)]))
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy)  # way 90 is a tombstone at rev 1
    # restore the tombstoned way inside the copy: version continues
    receipt = osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e1.jsonl", "e1", 0,
        [{"op": "put", "expected_version": 2,
          "entity": way(90, [9, 10, 11, 12, 9])}]))
    entry = receipt["changes"][0]
    assert entry["old_version"] == 2 and entry["new_version"] == 3
    # deleting it again in the copy is a normal delete
    receipt = osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e2.jsonl", "e2", 1,
        [{"op": "delete", "expected_version": 3, "type": "way", "id": 90}]))
    assert receipt["changes"][0]["tombstone"] is True


def test_preview_reports_topology_conflict(tmp_path):
    db, copy = _checkout(tmp_path, osmface)
    # a self-intersecting local bowtie ring assembles to topology_conflict
    osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e1.jsonl", "e1", 0, [
            {"op": "put", "expected_version": 0, "entity": lnode("n1", 0, 0)},
            {"op": "put", "expected_version": 0, "entity": lnode("n2", 2, 2)},
            {"op": "put", "expected_version": 0, "entity": lnode("n3", 2, 0)},
            {"op": "put", "expected_version": 0, "entity": lnode("n4", 0, 2)},
            {"op": "put", "expected_version": 0,
             "entity": lway("w1", [local("n1"), local("n2"), local("n3"),
                                   local("n4"), local("n1")])},
            {"op": "put", "expected_version": 0,
             "entity": lrel("r1", [lm("w1")])},
        ]))
    preview = osmface.workcopy_preview(copy, relation_id="r1")
    assert preview["relation"]["status"] == "topology_conflict"
    assert preview["relation"]["reason"]["code"] == "ring_self_intersection"


def test_checkout_requires_history(tmp_path):
    db = str(tmp_path / "plain.db")
    osmface.load_database(db, [str(tmp_path / "x.jsonl")] if False else [])
    # load_database with no files is not a valid path; build a real one
    src = tmp_path / "world.jsonl"
    write_jsonl(str(src), [node(1, 0, 0)])
    osmface.load_database(db, [str(src)], force=True)
    with pytest.raises(osmface.NotHistoryError):
        osmface.checkout_workcopy(db, str(tmp_path / "copy.db"))


def test_preview_single_relation_by_int_id_and_by_cli_string(tmp_path):
    db, copy = _checkout(tmp_path, osmface)
    # integer id lookup
    preview = osmface.workcopy_preview(copy, relation_id=100)
    assert preview["relation"]["status"] == "complete"
    assert preview["relation"]["defined"] is True
    # string "100" (what the CLI passes) resolves to the same relation
    preview2 = osmface.workcopy_preview(copy, relation_id="100")
    assert preview2["relation"]["status"] == "complete"
    # unknown relation
    preview3 = osmface.workcopy_preview(copy, relation_id=4242)
    assert preview3["relation"]["defined"] is False
    assert preview3["relation"]["status"] is None

"""Change-batch semantics: validation, object versions, tombstones,
same-value puts, batch idempotency, whole-batch rollback."""
import json
import os
import sqlite3

import pytest

import osmface
from history_util import (
    build_warehouse,
    change_file,
    delete,
    node,
    put,
    way,
)


def test_new_id_requires_expected_version_zero(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    bad = change_file(tmp_path, "b.jsonl", "b", 0,
                      [put(1, node(99, 1, 1))])
    with pytest.raises(osmface.BatchError):
        osmface.apply_changes(db, bad)
    assert osmface.inspect_database(db)["revision"] == 0


def test_put_and_delete_increment_version(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    f1 = change_file(tmp_path, "c1.jsonl", "c1", 0,
                     [put(1, node(1, 2, 2)), delete(1, "way", 90)])
    receipt = osmface.apply_changes(db, f1)
    versions = {c["id"]: c["new_version"] for c in receipt["changes"]}
    assert versions == {1: 2, 90: 2}  # ids sorted across type domains
    conn = sqlite3.connect(db)
    node_row = conn.execute(
        "SELECT version, deleted FROM entity_versions WHERE type='node' AND id=1"
    ).fetchone()
    way_row = conn.execute(
        "SELECT version, deleted, payload FROM entity_versions"
        " WHERE type='way' AND id=90"
    ).fetchone()
    conn.close()
    assert node_row == (2, 0)
    assert way_row[0] == 2 and way_row[1] == 1 and way_row[2] is None


def test_delete_absent_or_tombstoned_rejected(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    f1 = change_file(tmp_path, "d1.jsonl", "d1", 0,
                     [delete(1, "way", 90)])
    osmface.apply_changes(db, f1)
    # deleting the tombstone again is rejected
    f2 = change_file(tmp_path, "d2.jsonl", "d2", 1,
                     [delete(2, "way", 90)])
    with pytest.raises(osmface.BatchError):
        osmface.apply_changes(db, f2)
    # deleting a never-existing object is rejected
    f3 = change_file(tmp_path, "d3.jsonl", "d3", 1,
                     [delete(0, "node", 777)])
    with pytest.raises(osmface.BatchError):
        osmface.apply_changes(db, f3)
    assert osmface.inspect_database(db)["revision"] == 1


def test_tombstone_restore_keeps_version_line(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    osmface.apply_changes(db, change_file(
        tmp_path, "del.jsonl", "del", 0, [delete(1, "way", 90)]))
    # restore against the tombstone version (2); must NOT reset to version 1
    receipt = osmface.apply_changes(db, change_file(
        tmp_path, "res.jsonl", "res", 1,
        [put(2, way(90, [9, 10, 11, 12, 9]))]))
    entry = [c for c in receipt["changes"] if c["id"] == 90][0]
    assert entry["old_version"] == 2 and entry["new_version"] == 3
    assert entry["tombstone"] is False
    detail = osmface.inspect_database(db, relation_id=300, revision=2)["relation"]
    assert detail["status"] == "complete"


def test_same_value_put_is_a_new_version(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    receipt = osmface.apply_changes(db, change_file(
        tmp_path, "same.jsonl", "same", 0, [put(1, node(1, 0, 0))]))
    assert receipt["changes"][0]["new_version"] == 2
    rel = receipt["affected_relations"][0]
    assert rel["geometry_changed"] is False
    assert rel["source_changed"] is True


def test_boolean_is_not_an_integer(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    raw = json.dumps({"batch_id": "x", "base_revision": 0}) + "\n" + \
        json.dumps({"op": "put", "expected_version": True,
                    "entity": {"type": "node", "id": 1, "x": 1, "y": 1}}) + "\n"
    path = os.path.join(tmp_path, "bool.jsonl")
    open(path, "w", encoding="utf-8").write(raw)
    with pytest.raises(osmface.BatchError):
        osmface.apply_changes(db, path)
    # header boolean also rejected
    raw2 = json.dumps({"batch_id": "z", "base_revision": False}) + "\n" + \
        json.dumps({"op": "delete", "expected_version": 1,
                    "type": "node", "id": 1}) + "\n"
    path2 = os.path.join(tmp_path, "bool2.jsonl")
    open(path2, "w", encoding="utf-8").write(raw2)
    with pytest.raises(osmface.BatchError):
        osmface.apply_changes(db, path2)


def test_duplicate_object_in_batch_rejected(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    bad = change_file(tmp_path, "dup.jsonl", "dup", 0,
                      [put(1, node(1, 0, 0)), put(2, node(1, 1, 1))])
    with pytest.raises(osmface.BatchError):
        osmface.apply_changes(db, bad)
    assert osmface.inspect_database(db)["revision"] == 0


def test_malformed_lines_consume_no_revision(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    path = os.path.join(tmp_path, "broken.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"batch_id": "b", "base_revision": 0}) + "\n")
        fh.write("{not json\n")
    with pytest.raises(osmface.BatchError):
        osmface.apply_changes(db, path)
    assert osmface.inspect_database(db)["revision"] == 0


def test_version_conflict_rolls_back_whole_batch(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    bad = change_file(tmp_path, "conf.jsonl", "conf", 0, [
        put(1, node(1, 5, 5)),
        put(9, node(2, 6, 6)),  # wrong expected version
    ])
    with pytest.raises(osmface.RevisionConflict):
        osmface.apply_changes(db, bad)
    # first row must not have been applied: node 1 untouched, head unmoved
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT version FROM entity_versions WHERE type='node' AND id=1"
    ).fetchone()[0] == 1
    assert conn.execute("SELECT head_revision FROM rev_meta").fetchone()[0] == 0
    conn.close()


def test_resend_returns_original_receipt_without_new_revision(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    f1 = change_file(tmp_path, "c.jsonl", "survey", 0, [put(1, node(1, 3, 3))])
    first = osmface.apply_changes(db, f1)
    second = osmface.apply_changes(db, f1)  # same id, identical bytes, head moved
    assert second == first
    assert osmface.inspect_database(db)["revision"] == 1
    assert osmface.get_receipt(db, "survey")["input_sha256"] == first["input_sha256"]


def test_same_batch_id_different_bytes_rejected(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    f1 = change_file(tmp_path, "c.jsonl", "survey", 0, [put(1, node(1, 3, 3))])
    osmface.apply_changes(db, f1)
    f2 = change_file(tmp_path, "c2.jsonl", "survey", 1, [put(2, node(1, 4, 4))])
    with pytest.raises(osmface.BatchIdConflict):
        osmface.apply_changes(db, f2)
    # the failed attempt does not consume the revision
    assert osmface.inspect_database(db)["revision"] == 1


def test_failed_batch_id_can_be_reused(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    bad = change_file(tmp_path, "f.jsonl", "flaky", 0, [put(9, node(1, 1, 1))])
    with pytest.raises(osmface.RevisionConflict):
        osmface.apply_changes(db, bad)
    good = change_file(tmp_path, "g.jsonl", "flaky", 0, [put(1, node(1, 1, 1))])
    receipt = osmface.apply_changes(db, good)
    assert receipt["batch_id"] == "flaky" and receipt["revision"] == 1


def test_stale_base_revision_rejected(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    f1 = change_file(tmp_path, "c1.jsonl", "c1", 0, [put(1, node(1, 1, 1))])
    osmface.apply_changes(db, f1)
    stale = change_file(tmp_path, "stale.jsonl", "stale", 0,
                        [put(1, node(1, 2, 2))])
    with pytest.raises(osmface.RevisionConflict):
        osmface.apply_changes(db, stale)

"""CLI surface and a few edge cases of the revisioned warehouse."""
import json
import os
import sqlite3

import pytest

import osmface
from conftest import run_cli
from history_util import build_warehouse, change_file, node, put, way


def test_cli_apply_receipt_export_inspect(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    batch = change_file(tmp_path, "b.jsonl", "cli-batch", 0,
                        [put(1, node(2, 12, 0)), put(1, node(3, 12, 10))])
    proc = run_cli("apply", db, batch)
    assert proc.returncode == 0, proc.stderr
    assert "0->1" in proc.stdout and "cli-batch" in proc.stdout
    # receipt command
    proc = run_cli("receipt", db, "cli-batch")
    assert proc.returncode == 0
    receipt = json.loads(proc.stdout)
    assert receipt["revision"] == 1
    # inspect pinned to old revision
    proc = run_cli("inspect", db, "--relation", "100",
                   "--revision", "0", "--json")
    detail = json.loads(proc.stdout)["relation"]
    from shapely.geometry import shape
    assert shape(detail["faces"][0]["geometry"]).area == 100.0
    # export-revision
    out = str(tmp_path / "pkg")
    proc = run_cli("export-revision", db, out, "--revision", "1")
    assert proc.returncode == 0, proc.stderr
    manifest = json.load(open(os.path.join(out, "manifest.json")))
    assert manifest["revision"] == 1 and manifest["state"] == "complete"


def test_apply_json_receipt_is_complete(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    batch = change_file(tmp_path, "b.jsonl", "j", 0, [put(1, node(1, 1, 1))])
    proc = run_cli("apply", db, batch, "--json")
    receipt = json.loads(proc.stdout)
    for key in ("batch_id", "input_sha256", "base_revision", "revision",
                "changes", "affected_relations", "recomputed_relations"):
        assert key in receipt


def test_missing_reference_edges_survive_batch(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    # G (400) -> missing way 77. Modify an unrelated node; the dangling edge
    # and G's incomplete state must be preserved at the new revision.
    osmface.apply_changes(db, change_file(
        tmp_path, "b.jsonl", "b", 0, [put(1, node(9, 51, 50))]))
    conn = sqlite3.connect(db)
    edges = conn.execute(
        "SELECT COUNT(*) FROM ref_edge_history"
        " WHERE ref_type='way' AND ref_id=77"
    ).fetchone()[0]
    conn.close()
    assert edges >= 1
    detail = osmface.inspect_database(db, relation_id=400, revision=1)["relation"]
    assert detail["status"] == "incomplete"


def test_empty_change_set_rejected(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    path = os.path.join(tmp_path, "empty.jsonl")
    open(path, "w", encoding="utf-8").write(
        json.dumps({"batch_id": "e", "base_revision": 0}) + "\n")
    with pytest.raises(osmface.BatchError):
        osmface.apply_changes(db, path)


def test_unreadable_file_is_io_error(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    with pytest.raises(OSError):
        osmface.apply_changes(db, str(tmp_path / "does-not-exist.jsonl"))
    assert osmface.inspect_database(db)["revision"] == 0


def test_node_used_by_multiple_ways_recomputes_both_branches(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    # build a second relation sharing node 1 via a new closed way 50
    osmface.apply_changes(db, change_file(
        tmp_path, "add.jsonl", "add", 0, [
            put(0, node(41, 0, -20)), put(0, node(42, 4, -20)),
            put(0, node(43, 4, -16)), put(0, node(44, 0, -16)),
            put(0, way(50, [1, 41, 42, 43, 44, 1])),
            put(0, {"type": "relation", "id": 600,
                    "members": [{"type": "way", "ref": 50, "role": "outer"}]}),
        ]))
    # moving shared node 1 must reach both relation 100 (via way10) and 600
    receipt = osmface.apply_changes(db, change_file(
        tmp_path, "shared.jsonl", "shared", 1, [put(1, node(1, 1, 0))]))
    recomputed = set(receipt["recomputed_relations"])
    assert {100, 200, 600}.issubset(recomputed)
    assert 300 not in recomputed  # U still untouched

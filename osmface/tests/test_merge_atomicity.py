"""Deterministic crash and concurrency acceptance for merge confirmation.

Same marker-file protocol as the apply pipeline: the writer parks at
named sync points so tests can simulate termination exactly before or
after the commit, and can race a second writer.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

import osmface
from conftest import ROOT
from history_util import build_warehouse, change_file, node, put
from merge_util import edit_file, lnode


def _env(sync_dir=None):
    env = dict(os.environ, PYTHONPATH=ROOT, OSMFACE_BUSY_TIMEOUT="500")
    if sync_dir is not None:
        env["OSMFACE_SYNC_DIR"] = sync_dir
    return env


def _wait_marker(path, timeout=30):
    deadline = time.time() + timeout
    while not os.path.exists(path):
        time.sleep(0.02)
        assert time.time() < deadline, f"marker never appeared: {path}"


def _planned_merge(tmp_path):
    """Warehouse at rev 0 plus a package that adds local node n1."""
    db, _ = build_warehouse(tmp_path, osmface)
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy)
    osmface.workcopy_edit(copy, edit_file(
        tmp_path, "e.jsonl", "e", 0,
        [{"op": "put", "expected_version": 0, "entity": lnode("n1", 7, 7)}]))
    pkg = str(tmp_path / "pkg")
    osmface.workcopy_export(copy, pkg)
    plan = osmface.create_merge_plan(db, pkg)
    return db, plan["plan_id"]


def test_termination_before_commit_leaves_no_merge_trace(tmp_path):
    db, plan_id = _planned_merge(tmp_path)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / "crash-precommit").write_text("")
    proc = subprocess.run(
        [sys.executable, "-m", "osmface", "merge-confirm", db, plan_id,
         "--merge-id", "m-crash"],
        capture_output=True, text=True, env=_env(str(sync_dir)),
    )
    assert proc.returncode == 17
    # only the old revision is visible; no allocation, no entities
    assert osmface.inspect_database(db)["revision"] == 0
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_versions WHERE type='node' AND id=13"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM merge_registry WHERE merge_id='m-crash'"
    ).fetchone()[0] == 0
    conn.close()
    # the merge_id was not consumed: the same confirm now lands cleanly
    os.remove(sync_dir / "crash-precommit")
    receipt = osmface.confirm_merge(db, plan_id, "m-crash")
    assert receipt["result_revision"] == 1
    assert receipt["allocation"]["node"]["n1"] == 13


def test_termination_after_commit_resend_returns_receipt(tmp_path):
    db, plan_id = _planned_merge(tmp_path)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / "crash-committed").write_text("")
    proc = subprocess.run(
        [sys.executable, "-m", "osmface", "merge-confirm", db, plan_id,
         "--merge-id", "m-crash2"],
        capture_output=True, text=True, env=_env(str(sync_dir)),
    )
    assert proc.returncode == 17
    # the merge revision is fully present even though the receipt was lost
    assert osmface.inspect_database(db)["revision"] == 1
    os.remove(sync_dir / "crash-committed")
    resent = osmface.confirm_merge(db, plan_id, "m-crash2")
    assert resent["result_revision"] == 1
    assert resent["merge_id"] == "m-crash2"
    # no duplicate object versions or allocations
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_history WHERE type='node' AND id=13"
    ).fetchone()[0] == 1
    conn.close()


def test_confirm_racing_apply_exactly_one_wins(tmp_path):
    db, plan_id = _planned_merge(tmp_path)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / "wait-precommit").write_text("")
    merger = subprocess.Popen(
        [sys.executable, "-m", "osmface", "merge-confirm", db, plan_id,
         "--merge-id", "m-race"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=_env(str(sync_dir)),
    )
    _wait_marker(str(sync_dir / "reached-precommit"))
    # a direct apply races the parked merge on the same head
    batch = change_file(tmp_path, "b.jsonl", "racer", 0,
                        [put(1, node(2, 12, 0))])
    loser = subprocess.run(
        [sys.executable, "-m", "osmface", "apply", db, batch],
        capture_output=True, text=True, env=_env(None),
    )
    assert loser.returncode == 2
    assert "locked" in loser.stderr.lower() or "stale" in loser.stderr.lower()
    (sync_dir / "proceed-precommit").write_text("")
    (sync_dir / "proceed-committed").write_text("")
    _, err = merger.communicate(timeout=30)
    assert merger.returncode == 0, err.decode()
    assert osmface.inspect_database(db)["revision"] == 1
    # the loser rebases onto the merge revision and lands as revision 2
    batch2 = change_file(tmp_path, "b2.jsonl", "racer", 1,
                         [put(1, node(2, 12, 0))])
    ok = subprocess.run(
        [sys.executable, "-m", "osmface", "apply", db, batch2],
        capture_output=True, text=True, env=_env(None),
    )
    assert ok.returncode == 0, ok.stderr
    assert osmface.inspect_database(db)["revision"] == 2


def test_stepwise_rulings_and_re_ruling(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    osmface.apply_changes(db, change_file(
        tmp_path, "m.jsonl", "m", 0,
        [put(1, node(2, 12, 0)), put(1, node(3, 10, 12))]))
    copy = str(tmp_path / "copy.db")
    osmface.checkout_workcopy(db, copy, revision=0)
    osmface.workcopy_edit(copy, edit_file(tmp_path, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1, "entity": node(2, 55, 0)},
        {"op": "put", "expected_version": 1, "entity": node(3, 10, 55)},
    ]))
    pkg = str(tmp_path / "pkg")
    osmface.workcopy_export(copy, pkg)
    plan = osmface.create_merge_plan(db, pkg)
    assert len(plan["unresolved_conflicts"]) == 2
    # step one: rule node 2 only; the plan is still open for more rulings
    osmface.save_merge_rulings(db, plan["plan_id"], [
        {"type": "node", "id": 2, "choice": "field"},
    ])
    mid = osmface.get_merge_plan(db, plan["plan_id"])
    assert mid["unresolved_conflicts"] == [{"type": "node", "id": 3}]
    # step two: re-rule node 2 for the main side, then rule node 3
    osmface.save_merge_rulings(db, plan["plan_id"], [
        {"type": "node", "id": 2, "choice": "main"},
    ])
    osmface.save_merge_rulings(db, plan["plan_id"], [
        {"type": "node", "id": 3, "choice": "baseline"},
    ])
    done = osmface.get_merge_plan(db, plan["plan_id"])
    assert done["unresolved_conflicts"] == []
    receipt = osmface.confirm_merge(db, plan["plan_id"], "m-steps")
    # node 2 kept the main value (no change); node 3 was ruled back to the
    # baseline value, which differs from H -> exactly one entity change
    assert receipt["result_revision"] == 2
    assert [(c["type"], c["id"], c["op"]) for c in receipt["changes"]] == [
        ("node", 3, "put")
    ]
    conn = sqlite3.connect(db)
    rows = dict(conn.execute(
        "SELECT id, payload FROM entity_versions WHERE type='node'"
        " AND id IN (2, 3)"))
    conn.close()
    assert json.loads(rows[2]) == [12, 0]
    assert json.loads(rows[3]) == [10, 10]
    # a confirmed plan no longer accepts rulings
    with pytest.raises(osmface.MergeError):
        osmface.save_merge_rulings(db, plan["plan_id"], [
            {"type": "node", "id": 2, "choice": "field"},
        ])


def test_export_revision_of_a_merge_revision(tmp_path):
    db, plan_id = _planned_merge(tmp_path)
    receipt = osmface.confirm_merge(db, plan_id, "m-exp")
    out = str(tmp_path / "rev1")
    manifest = osmface.export_revision_package(db, out, revision=1)
    assert manifest["revision_identity"]["kind"] == "merge"
    assert manifest["revision_identity"]["merge_id"] == "m-exp"
    pkg = osmface.inspect_package(out)
    assert pkg["manifest"]["state"] == "complete"

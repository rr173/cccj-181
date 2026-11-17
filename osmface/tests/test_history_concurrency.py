"""Deterministic crash and concurrency acceptance.

No random sleeps: the writer blocks at named sync points (marker files),
letting tests park it before/after the commit and interleave a reader.
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


def _move_batch(directory, name, bid, base, expected, x):
    return change_file(directory, name, bid, base,
                       [put(expected, node(2, x, 0))])


def test_termination_before_commit_leaves_only_old_revision(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / "crash-precommit").write_text("")
    batch = _move_batch(tmp_path, "b.jsonl", "crash", 0, 1, 12)
    proc = subprocess.run(
        [sys.executable, "-m", "osmface", "apply", db, batch],
        capture_output=True, text=True, env=_env(str(sync_dir)),
    )
    assert proc.returncode == 17
    assert osmface.inspect_database(db)["revision"] == 0
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT version FROM entity_versions WHERE type='node' AND id=2"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_history WHERE type='node' AND id=2"
    ).fetchone()[0] == 1
    conn.close()
    # the batch id was not consumed; the same file now lands cleanly
    os.remove(sync_dir / "crash-precommit")
    receipt = osmface.apply_changes(db, batch)
    assert receipt["revision"] == 1 and receipt["batch_id"] == "crash"


def test_termination_after_commit_resend_returns_receipt(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    first = _move_batch(tmp_path, "b1.jsonl", "first", 0, 1, 12)
    osmface.apply_changes(db, first)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / "crash-committed").write_text("")
    second = _move_batch(tmp_path, "b2.jsonl", "second", 1, 2, 13)
    proc = subprocess.run(
        [sys.executable, "-m", "osmface", "apply", db, second],
        capture_output=True, text=True, env=_env(str(sync_dir)),
    )
    assert proc.returncode == 17
    # revision 2 is fully present even though the receipt never reached us
    assert osmface.inspect_database(db)["revision"] == 2
    os.remove(sync_dir / "crash-committed")
    resent = osmface.apply_changes(db, second)
    assert resent["revision"] == 2 and resent["batch_id"] == "second"
    # no duplicate object versions or result rows
    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_history WHERE type='node' AND id=2"
    ).fetchone()[0] == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM rel_result_history WHERE revision=2"
    ).fetchone()[0] >= 1
    conn.close()


def test_reader_pins_revision_while_writer_commits(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / "wait-precommit").write_text("")
    batch = _move_batch(tmp_path, "b.jsonl", "interleave", 0, 1, 12)
    writer = subprocess.Popen(
        [sys.executable, "-m", "osmface", "apply", db, batch],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=_env(str(sync_dir)),
    )
    _wait_marker(str(sync_dir / "reached-precommit"))
    # reader pins head revision 0 while the writer is parked mid-transaction
    pinned = osmface.inspect_database(db, revision=0)
    assert pinned["revision"] == 0
    detail = osmface.inspect_database(db, relation_id=100, revision=0)
    from shapely.geometry import shape
    assert shape(detail["relation"]["faces"][0]["geometry"]).area == 100.0
    (sync_dir / "proceed-precommit").write_text("")
    (sync_dir / "proceed-committed").write_text("")
    _, err = writer.communicate(timeout=30)
    assert writer.returncode == 0, err.decode()
    assert osmface.inspect_database(db)["revision"] == 1


def test_two_writers_only_one_lands(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / "wait-precommit").write_text("")
    batch_a = _move_batch(tmp_path, "a.jsonl", "writer-a", 0, 1, 12)
    batch_b = change_file(tmp_path, "b.jsonl", "writer-b", 0,
                          [put(1, node(3, 9, 9))])
    writer_a = subprocess.Popen(
        [sys.executable, "-m", "osmface", "apply", db, batch_a],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=_env(str(sync_dir)),
    )
    _wait_marker(str(sync_dir / "reached-precommit"))
    # B races A on the same base revision: it must lose explicitly
    loser = subprocess.run(
        [sys.executable, "-m", "osmface", "apply", db, batch_b],
        capture_output=True, text=True, env=_env(None),
    )
    assert loser.returncode == 2
    assert "locked" in loser.stderr.lower() or "stale" in loser.stderr.lower()
    assert osmface.inspect_database(db)["revision"] == 0
    (sync_dir / "proceed-precommit").write_text("")
    (sync_dir / "proceed-committed").write_text("")
    _, err = writer_a.communicate(timeout=30)
    assert writer_a.returncode == 0, err.decode()
    assert osmface.inspect_database(db)["revision"] == 1
    # B rebases onto the new head and then lands as revision 2
    batch_b2 = change_file(tmp_path, "b2.jsonl", "writer-b", 1,
                           [put(1, node(3, 9, 9))])
    ok = subprocess.run(
        [sys.executable, "-m", "osmface", "apply", db, batch_b2],
        capture_output=True, text=True, env=_env(None),
    )
    assert ok.returncode == 0, ok.stderr
    assert osmface.inspect_database(db)["revision"] == 2

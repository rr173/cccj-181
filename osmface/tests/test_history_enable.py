"""Enabling history: preconditions, revision 0 identity, retry safety,
and the refusal of legacy overwrite writes once history is on."""
import json
import os

import pytest

import osmface
from conftest import fixture, run_cli
from history_util import build_warehouse


def test_enable_creates_revision_zero(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    info = osmface.inspect_database(db)
    assert info["history_enabled"] is True
    assert info["revision"] == 0
    assert info["revision_identity"] == {
        "revision": 0, "head_revision": 0, "batch_id": None, "kind": "baseline",
    }
    # every existing object starts at version 1
    import sqlite3

    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT MIN(version), MAX(version), COUNT(*) FROM entity_versions"
    ).fetchone() == (1, 1, 14)  # 6 nodes + 3 ways + 5 relations
    conn.close()


def test_enable_is_idempotent_and_preserves_data(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    before = osmface.inspect_database(db, relation_id=100, revision=0)
    again = osmface.enable_history(db)
    assert again["idempotent"] is True and again["revision"] == 0
    after = osmface.inspect_database(db, relation_id=100, revision=0)
    assert after == before  # raw records, results and sources untouched


def test_enable_requires_loaded_and_assembled(tmp_path):
    # loaded but never assembled -> cannot enable
    db_loaded = os.path.join(tmp_path, "loaded.db")
    osmface.load_database(db_loaded, [fixture("base.jsonl")])
    with pytest.raises(osmface.HistoryError):
        osmface.enable_history(db_loaded)

    # failed (corrupted input) database -> cannot enable
    db_failed = os.path.join(tmp_path, "failed.db")
    osmface.load_database(db_failed, [fixture("dup_conflict.jsonl")])
    with pytest.raises(osmface.HistoryError):
        osmface.enable_history(db_failed)


def test_incomplete_and_conflict_relations_do_not_block_enable(tmp_path):
    # cycle.jsonl has incomplete relations; enabling must still succeed
    db = os.path.join(tmp_path, "cyc.db")
    osmface.load_database(db, [fixture("cycle.jsonl")])
    osmface.assemble_database(db)
    summary = osmface.enable_history(db)
    assert summary["revision"] == 0
    detail = osmface.inspect_database(db, relation_id=400, revision=0)["relation"]
    assert detail["status"] == "incomplete"
    assert detail["reason"]["code"] == "relation_cycle"


def test_legacy_assemble_is_refused_after_enable(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    with pytest.raises(osmface.HistoryError):
        osmface.assemble_database(db)
    proc = run_cli("assemble", db)
    assert proc.returncode == 2
    # the legacy results were not wiped by the refused call
    assert osmface.inspect_database(db, relation_id=100)["relation"]["status"] \
        == "complete"


def test_legacy_commands_still_work_on_plain_database(tmp_path):
    db = os.path.join(tmp_path, "plain.db")
    osmface.load_database(db, [fixture("base.jsonl")])
    osmface.assemble_database(db)
    # no history: revision argument is rejected but normal inspection works
    with pytest.raises(osmface.NotHistoryError):
        osmface.inspect_database(db, revision=0)
    info = osmface.inspect_database(db)
    assert info["status_counts"]["complete"] == 2
    assert "history_enabled" not in info


def test_enable_cli(tmp_path):
    db = os.path.join(tmp_path, "w.db")
    osmface.load_database(db, [fixture("base.jsonl")])
    osmface.assemble_database(db)
    proc = run_cli("enable-history", db)
    assert proc.returncode == 0 and "revision=0" in proc.stdout
    proc = run_cli("enable-history", db)  # safe retry
    assert proc.returncode == 0 and "idempotent" in proc.stdout

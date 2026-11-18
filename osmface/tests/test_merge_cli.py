"""CLI coverage for the offline-merge command chain."""
import json
import os

import osmface
from conftest import run_cli
from history_util import build_warehouse, change_file, node, put
from merge_util import edit_file, lnode, local, lway, rulings_file


def test_merge_command_chain(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    copy = str(tmp_path / "copy.db")
    pkg = str(tmp_path / "pkg")

    r = run_cli("checkout", db, copy)
    assert r.returncode == 0, r.stderr
    assert "base_revision=0" in r.stdout

    edits = edit_file(tmp_path, "e.jsonl", "e1", 0, [
        {"op": "put", "expected_version": 0, "entity": lnode("n1", 3, 3)},
        {"op": "put", "expected_version": 1, "entity": node(2, 11, 0)},
    ])
    r = run_cli("wc-edit", copy, edits)
    assert r.returncode == 0, r.stderr
    assert "0->1" in r.stdout

    r = run_cli("wc-info", copy)
    assert r.returncode == 0
    info = json.loads(r.stdout)
    assert info["edit_head"] == 1

    r = run_cli("wc-preview", copy)
    assert r.returncode == 0, r.stderr
    assert "edit_head=1" in r.stdout

    r = run_cli("wc-export", copy, pkg)
    assert r.returncode == 0, r.stderr
    assert "state=complete" in r.stdout

    # main moves the same node the field moved -> conflict
    osmface.apply_changes(db, change_file(
        tmp_path, "m.jsonl", "m", 0, [put(1, node(2, 12, 0))]))

    r = run_cli("merge-plan", db, pkg)
    assert r.returncode == 0, r.stderr
    assert "conflicts=1" in r.stdout
    plan_id = json.loads(run_cli("merge-plan", db, pkg, "--json").stdout)[
        "plan_id"]

    r = run_cli("merge-show", db, plan_id)
    assert r.returncode == 0
    assert "conflict: node 2" in r.stdout

    # preview refuses while the conflict is open
    r = run_cli("merge-preview", db, plan_id)
    assert r.returncode == 2
    assert "INCOMPLETE" in r.stdout

    rulings = rulings_file(tmp_path, "rulings.json", [
        {"type": "node", "id": 2, "choice": "field"},
    ])
    r = run_cli("merge-rule", db, plan_id, rulings)
    assert r.returncode == 0, r.stderr
    assert "unresolved=0" in r.stdout

    r = run_cli("merge-preview", db, plan_id)
    assert r.returncode == 0, r.stderr
    assert "complete" in r.stdout

    r = run_cli("merge-confirm", db, plan_id, "--merge-id", "m-cli")
    assert r.returncode == 0, r.stderr
    assert "1->2" in r.stdout

    r = run_cli("merge-receipt", db, "m-cli")
    assert r.returncode == 0
    receipt = json.loads(r.stdout)
    assert receipt["result_revision"] == 2
    assert receipt["allocation"]["node"]["n1"] == 13

    r = run_cli("merge-list", db)
    assert r.returncode == 0
    plans = json.loads(r.stdout)
    assert plans[0]["status"] == "confirmed"
    assert plans[0]["merge_id"] == "m-cli"

    # the warehouse state is real: node 2 took the field value
    detail = osmface.inspect_database(db, revision=2)
    assert detail["revision"] == 2
    import sqlite3
    conn = sqlite3.connect(db)
    payload = conn.execute(
        "SELECT payload FROM entity_versions WHERE type='node' AND id=2"
    ).fetchone()[0]
    conn.close()
    assert json.loads(payload) == [11, 0]


def test_source_id_command(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    r = run_cli("source-id", db)
    assert r.returncode == 0
    first = json.loads(r.stdout)["source_id"]
    again = json.loads(run_cli("source-id", db).stdout)["source_id"]
    assert first == again

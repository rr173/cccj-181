#!/usr/bin/env python3
"""Runnable walkthrough of the offline-merge story.

Builds one history-enabled warehouse, checks out two offline work copies,
edits them independently (offline, in fresh processes), exports transport
packages, moves the warehouse forward, then plans, rules, previews and
confirms both merges — printing hand-checkable state at every step.

    python3 checks/demo_merge.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from history_util import write_jsonl  # noqa: E402


def run(*args, expect=0):
    env = dict(os.environ, PYTHONPATH=ROOT)
    proc = subprocess.run(
        [sys.executable, "-m", "osmface", *args],
        capture_output=True, text=True, env=env,
    )
    out = (proc.stdout + proc.stderr).strip()
    print(f"$ osmface {' '.join(args)}")
    for line in out.splitlines():
        print(f"    {line}")
    if expect is not None and proc.returncode != expect:
        raise SystemExit(
            f"command failed with {proc.returncode} (expected {expect})"
        )
    print()
    return proc


def main():
    tmp = tempfile.mkdtemp(prefix="osmface-merge-demo-")
    print(f"working directory: {tmp}\n")
    main_db = os.path.join(tmp, "main.db")
    world = os.path.join(tmp, "world.jsonl")
    write_jsonl(world, [
        {"type": "node", "id": 1, "x": 0, "y": 0},
        {"type": "node", "id": 2, "x": 10, "y": 0},
        {"type": "node", "id": 3, "x": 10, "y": 10},
        {"type": "node", "id": 4, "x": 0, "y": 10},
        {"type": "way", "id": 10, "nodes": [1, 2, 3, 4, 1]},
        {"type": "relation", "id": 100,
         "members": [{"type": "way", "ref": 10, "role": "outer"}]},
    ])

    print("== build the warehouse and enable history ==\n")
    run("load", main_db, world)
    run("assemble", main_db)
    run("enable-history", main_db)
    run("source-id", main_db)

    print("== two field teams check out offline copies at head ==\n")
    copy_a = os.path.join(tmp, "teamA.db")
    copy_b = os.path.join(tmp, "teamB.db")
    run("checkout", main_db, copy_a)
    run("checkout", main_db, copy_b)

    print("== team A edits offline: new square via local names ==\n")
    edits_a = os.path.join(tmp, "editsA.jsonl")
    write_jsonl(edits_a, [
        {"edit_id": "teamA-day1", "base_edit": 0},
        {"op": "put", "expected_version": 0,
         "entity": {"type": "node", "local_id": "n1", "x": 20, "y": 0}},
        {"op": "put", "expected_version": 0,
         "entity": {"type": "node", "local_id": "n2", "x": 24, "y": 0}},
        {"op": "put", "expected_version": 0,
         "entity": {"type": "node", "local_id": "n3", "x": 24, "y": 4}},
        {"op": "put", "expected_version": 0,
         "entity": {"type": "node", "local_id": "n4", "x": 20, "y": 4}},
        {"op": "put", "expected_version": 0,
         "entity": {"type": "way", "local_id": "w1",
                    "nodes": [{"local": "n1"}, {"local": "n2"},
                              {"local": "n3"}, {"local": "n4"},
                              {"local": "n1"}]}},
        {"op": "put", "expected_version": 0,
         "entity": {"type": "relation", "local_id": "r1",
                    "members": [{"type": "way", "ref": {"local": "w1"},
                                 "role": "outer"}]}},
    ])
    run("wc-edit", copy_a, edits_a)
    run("wc-preview", copy_a, "--relation", "r1")

    print("== team B edits offline: move node 3, delete node 4 ==\n")
    edits_b = os.path.join(tmp, "editsB.jsonl")
    write_jsonl(edits_b, [
        {"edit_id": "teamB-day1", "base_edit": 0},
        {"op": "put", "expected_version": 1,
         "entity": {"type": "node", "id": 3, "x": 10, "y": 12}},
        {"op": "delete", "expected_version": 1, "type": "node", "id": 4},
    ])
    run("wc-edit", copy_b, edits_b)
    run("wc-preview", copy_b)

    print("== both teams export transport packages ==\n")
    pkg_a = os.path.join(tmp, "pkgA")
    pkg_b = os.path.join(tmp, "pkgB")
    run("wc-export", copy_a, pkg_a)
    run("wc-export", copy_b, pkg_b)

    print("== meanwhile the warehouse moves: node 3 moved too ==\n")
    main_batch = os.path.join(tmp, "main.jsonl")
    write_jsonl(main_batch, [
        {"batch_id": "main-1", "base_revision": 0},
        {"op": "put", "expected_version": 1,
         "entity": {"type": "node", "id": 3, "x": 10, "y": 11}},
    ])
    run("apply", main_db, main_batch)

    print("== team A's package: plan, preview, confirm (no conflicts) ==\n")
    run("merge-plan", main_db, pkg_a)
    plan_a = json.loads(run("merge-plan", main_db, pkg_a, "--json",
                            ).stdout)["plan_id"]
    run("merge-preview", main_db, plan_a)
    run("merge-confirm", main_db, plan_a, "--merge-id", "merge-teamA")

    print("== team B's package: conflict on node 3, ruled for the field ==\n")
    plan_b = json.loads(run("merge-plan", main_db, pkg_b, "--json",
                            ).stdout)["plan_id"]
    run("merge-show", main_db, plan_b)
    rulings = os.path.join(tmp, "rulingsB.json")
    with open(rulings, "w", encoding="utf-8") as fh:
        json.dump({"rulings": [
            {"type": "node", "id": 3, "choice": "field"},
        ]}, fh)
    run("merge-rule", main_db, plan_b, rulings)
    run("merge-preview", main_db, plan_b)
    run("merge-confirm", main_db, plan_b, "--merge-id", "merge-teamB")

    print("== receipts and final state ==\n")
    run("merge-receipt", main_db, "merge-teamA")
    run("merge-receipt", main_db, "merge-teamB")
    run("inspect", main_db)
    print("demo finished OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

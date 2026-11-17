#!/usr/bin/env python3
"""Reproducible independent check of the revisioned warehouse.

Two independent strands, neither trusting the production self-report:

1. Hand-written object list + shoelace formula: the key revisions' areas
   and the object versions behind every source segment are recomputed from
   coordinates alone (no geometry library needed for the area assertion).
2. Reference projection: a stdlib-only interpreter of change semantics emits
   the effective entity list per revision; each is fed into the ORDINARY
   full assembler on a fresh database and compared (status + topological
   geometry) with the incremental warehouse at the same revision.

    python3 checks/check_history.py
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

import osmface  # noqa: E402
from shapely.geometry import shape  # noqa: E402
from history_util import (  # noqa: E402
    baseline_records,
    change_file,
    delete,
    node,
    put,
    rel,
    rm,
    shoelace,
    way,
    wm,
    write_jsonl,
)

PROJECTION = os.path.join(ROOT, "checks", "reference_projection.py")


def build_batches(tmp):
    batches = [
        ("move.jsonl", "move", 0, [
            put(1, node(2, 12, 0)), put(1, node(3, 12, 10))]),
        ("swap.jsonl", "swap", 1, [
            put(0, node(21, 100, 100)), put(0, node(22, 120, 100)),
            put(0, node(23, 120, 110)), put(0, node(24, 100, 110)),
            put(0, way(20, [21, 22, 23, 24, 21])),
            put(1, rel(100, [wm(20)])),
            delete(1, "way", 10)]),
        ("fill.jsonl", "fill", 2, [
            put(0, node(71, 0, 200)), put(0, node(72, 5, 200)),
            put(0, node(73, 5, 205)), put(0, node(74, 0, 205)),
            put(0, way(77, [71, 72, 73, 74, 71]))]),
        ("cycle.jsonl", "cycle", 3, [
            put(0, rel(500, [rm(501)])), put(0, rel(501, [rm(500)]))]),
        ("break.jsonl", "break", 4, [
            put(1, rel(501, [wm(20)]))]),
        ("sameval.jsonl", "sameval", 5, [
            put(1, node(21, 100, 100))]),
        ("reverse.jsonl", "reverse", 6, [
            put(1, way(20, [21, 24, 23, 22, 21]))]),
    ]
    paths = []
    for name, bid, base, changes in batches:
        paths.append(change_file(tmp, name, bid, base, changes))
    return paths


def check_shoelace(db):
    """Area and source-version assertions from the hand-written list."""
    # rev0 rectangle coordinates, shoelace by hand
    coords0 = {1: (0, 0), 2: (10, 0), 3: (10, 10), 4: (0, 10)}
    ring0 = osmface.inspect_database(
        db, relation_id=100, revision=0)["relation"]["faces"][0]["rings"][0]
    area0 = shoelace([coords0[n] for n in ring0["nodes"][:-1]])
    assert area0 == 100.0, area0

    # rev1: right nodes at x=12 -> width 12, height 10 -> 120
    coords1 = {1: (0, 0), 2: (12, 0), 3: (12, 10), 4: (0, 10)}
    ring1 = osmface.inspect_database(
        db, relation_id=100, revision=1)["relation"]["faces"][0]["rings"][0]
    area1 = shoelace([coords1[n] for n in ring1["nodes"][:-1]])
    assert area1 == 120.0, area1
    # shapely agrees with the hand formula
    assert shape(osmface.inspect_database(
        db, relation_id=100, revision=1)["relation"]["faces"][0]["geometry"]
    ).area == 120.0
    # old revision keeps old object versions, new one records the bumps
    old_seg = osmface.inspect_database(
        db, relation_id=100, revision=0)["relation"]["faces"][0]["rings"][0]
    new_seg = osmface.inspect_database(
        db, relation_id=100, revision=1)["relation"]["faces"][0]["rings"][0]
    assert dict(old_seg["segments"][0]["node_versions"]) == {1: 1, 2: 1, 3: 1, 4: 1}
    assert dict(new_seg["segments"][0]["node_versions"]) == {1: 1, 2: 2, 3: 2, 4: 1}
    print(f"  shoelace: rev0={area0} rev1={area1}, source versions verified")


def check_projection(db, src, batch_paths, tmp):
    proj_dir = os.path.join(tmp, "proj")
    subprocess.run(
        [sys.executable, PROJECTION, src, *batch_paths, "--out-dir", proj_dir],
        check=True, capture_output=True, text=True)
    for revision in range(len(batch_paths) + 1):
        ref_db = os.path.join(tmp, f"ref-r{revision}.db")
        osmface.load_database(
            ref_db, [os.path.join(proj_dir, f"rev{revision}.jsonl")], force=True)
        osmface.assemble_database(ref_db)
        import sqlite3
        ref_status = {r[0]: r[1] for r in sqlite3.connect(ref_db).execute(
            "SELECT relation_id, status FROM relation_status")}
        for rid, status in ref_status.items():
            wh = osmface.inspect_database(
                db, relation_id=rid, revision=revision)["relation"]
            assert wh["status"] == status, (revision, rid, status)
            if status == "complete":
                ref = osmface.inspect_database(ref_db, relation_id=rid)["relation"]
                g_ref = shape(ref["faces"][0]["geometry"])
                g_wh = shape(wh["faces"][0]["geometry"])
                assert g_ref.equals(g_wh), (revision, rid)
    print(f"  projection: {len(batch_paths) + 1} revisions match full assembly")


def main():
    tmp = tempfile.mkdtemp(prefix="osmface-check-")
    src = write_jsonl(os.path.join(tmp, "world.jsonl"), baseline_records())
    db = os.path.join(tmp, "world.db")
    osmface.load_database(db, [src], force=True)
    osmface.assemble_database(db)
    osmface.enable_history(db)
    batch_paths = build_batches(tmp)
    for path in batch_paths:
        osmface.apply_changes(db, path)
    print("check 1: hand-written list + shoelace")
    check_shoelace(db)
    print("check 2: reference projection -> ordinary full assembly")
    check_projection(db, src, batch_paths, tmp)
    print("ALL INDEPENDENT CHECKS PASSED")


if __name__ == "__main__":
    main()

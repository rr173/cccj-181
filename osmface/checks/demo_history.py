#!/usr/bin/env python3
"""Runnable walkthrough of the revisioned map warehouse.

Builds the hand-written world (rectangle way -> relation A -> parent P,
unrelated U, gap relation G), enables history as revision 0, then walks
through the whole change/history/export story with printed, hand-checkable
results.  Uses only the public Python API and writes into a temp directory.

    python3 checks/demo_history.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import osmface  # noqa: E402
from shapely.geometry import shape  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests"))
from history_util import (  # noqa: E402
    baseline_records,
    change_file,
    delete,
    node,
    put,
    rel,
    way,
    wm,
    write_jsonl,
)


def area(detail):
    return shape(detail["faces"][0]["geometry"]).area


def main():
    work = tempfile.mkdtemp(prefix="osmface-demo-")
    print(f"workdir: {work}")
    db = os.path.join(work, "world.db")
    src = write_jsonl(os.path.join(work, "world.jsonl"), baseline_records())

    # 1. the ordinary one-shot pipeline, then enable history in place
    osmface.load_database(db, [src], force=True)
    osmface.assemble_database(db)
    print("enable:", osmface.enable_history(db))
    print("retry (idempotent):", osmface.enable_history(db)["idempotent"])

    # 2. revision 0: rectangle 10x10 -> area 100 for A and P
    a0 = osmface.inspect_database(db, relation_id=100, revision=0)["relation"]
    print(f"rev0  A area={area(a0)} way_version="
          f"{a0['faces'][0]['rings'][0]['segments'][0]['way_version']}")

    # 3. move the two right-side nodes to x=12 -> area 120; U not recomputed
    r1 = osmface.apply_changes(db, change_file(
        work, "move.jsonl", "survey-1", 0, [
            put(1, node(2, 12, 0)), put(1, node(3, 12, 10))]))
    print(f"rev1  recomputed={r1['recomputed_relations']} "
          f"(U=300 absent means it was not recomputed)")
    a1 = osmface.inspect_database(db, relation_id=100, revision=1)["relation"]
    print(f"rev1  A area={area(a1)}; rev0 still "
          f"{area(osmface.inspect_database(db, relation_id=100, revision=0)['relation'])}")
    print("      node source versions now:",
          a1["faces"][0]["rings"][0]["segments"][0]["node_versions"])

    # 4. same batch bytes resent -> original receipt, no new revision
    again = osmface.apply_changes(db, os.path.join(work, "move.jsonl"))
    print(f"resend survey-1 -> revision {again['revision']} (unchanged)")

    # 5. swap A onto a brand-new way and delete the old way in one batch
    osmface.apply_changes(db, change_file(
        work, "swap.jsonl", "swap", 1, [
            put(0, node(21, 100, 100)), put(0, node(22, 120, 100)),
            put(0, node(23, 120, 110)), put(0, node(24, 100, 110)),
            put(0, way(20, [21, 22, 23, 24, 21])),
            put(1, rel(100, [wm(20)])),
            delete(1, "way", 10)]))
    a2 = osmface.inspect_database(db, relation_id=100, revision=2)["relation"]
    print(f"rev2  A uses way20, area={area(a2)}; old way10 is a tombstone")

    # 6. restore the deleted way and move a node: detached A/P stay untouched
    r3 = osmface.apply_changes(db, change_file(
        work, "restore.jsonl", "restore", 2, [
            put(2, way(10, [1, 2, 3, 4, 1])),
            put(2, node(2, 10, 0))]))
    print(f"rev3  recomputed after way10 restore: {r3['recomputed_relations']}"
          " (A/P detached -> empty)")

    # 7. fill the missing way 77: G recovers from incomplete to complete
    g_before = osmface.inspect_database(db, relation_id=400, revision=3)["relation"]
    r4 = osmface.apply_changes(db, change_file(
        work, "fill.jsonl", "fill", 3, [
            put(0, node(71, 0, 200)), put(0, node(72, 5, 200)),
            put(0, node(73, 5, 205)), put(0, node(74, 0, 205)),
            put(0, way(77, [71, 72, 73, 74, 71]))]))
    g_after = osmface.inspect_database(db, relation_id=400, revision=4)["relation"]
    print(f"rev4  G {g_before['status']} -> {g_after['status']} "
          f"area={area(g_after)}; witness="
          f"{r4['affected_relations'][0]['paths'][0]['path']}")

    # 8. export an immutable revision package and show it is bound to revision
    out = os.path.join(work, "package-rev1")
    manifest = osmface.export_revision_package(db, out, revision=1)
    print(f"export {out}: state={manifest['state']} "
          f"revision={manifest['revision']}")
    print("files:", sorted(os.listdir(out)))

    print("\nCLI equivalents:")
    print(f"  python3 -m osmface inspect {db} --relation 100 --revision 0")
    print(f"  python3 -m osmface receipt {db} survey-1")
    print(f"  python3 -m osmface export-revision {db} {out}-head")
    shutil.rmtree(work, ignore_errors=True)
    print("demo OK")


if __name__ == "__main__":
    main()

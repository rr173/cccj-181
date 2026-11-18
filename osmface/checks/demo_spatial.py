#!/usr/bin/env python3
"""Runnable walkthrough of historical spatial face queries.

Builds the hand-written spatial world (outer square with a hole and an
island, a disjoint square and an overlapping one), enables history as
revision 0, then prints hand-checkable point/range answers, moves a
boundary to create revision 1, and shows the same point giving different
answers on the frozen old revision and the new one.

    python3 checks/demo_spatial.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "tests"))

import osmface  # noqa: E402
from history_util import change_file, node, put  # noqa: E402
from fixtures_spatial_data import spatial_world_records  # noqa: E402


def show(title, result):
    hits = {h["relation_id"]: h["faces"] for h in result["hits"]}
    print(f"  {title}: rev={result['revision']} head={result['head_revision']}"
          f" candidates={result['index']['candidate_faces']}"
          f" exact_checks={result['exact_checks']} hits={hits}")


def main():
    work = tempfile.mkdtemp(prefix="osmface-spatial-demo-")
    print(f"workdir: {work}")
    from history_util import write_jsonl

    src = write_jsonl(os.path.join(work, "spatial_world.jsonl"),
                      spatial_world_records())
    db = os.path.join(work, "spatial.db")
    osmface.load_database(db, [src], force=True)
    osmface.assemble_database(db)
    enable = osmface.enable_history(db)
    print("enable:", {k: enable[k] for k in ("revision", "spatial_index")})
    print("spatial-index state:", osmface.spatial_index_info(db))

    print("revision 0 point answers:")
    show("inside outer face (1,1)", osmface.query_faces_at_point(db, 1, 1, 0))
    show("inside hole, no relation there (5,5)",
         osmface.query_faces_at_point(db, 5, 5, 0))
    show("inside independent island (8,8)",
         osmface.query_faces_at_point(db, 8, 8, 0))
    show("on outer boundary (0,5)",
         osmface.query_faces_at_point(db, 0, 5, 0))
    show("overlap corner (18,18)",
         osmface.query_faces_at_point(db, 18, 18, 0))
    show("MultiPolygon face 1 (72,72)",
         osmface.query_faces_at_point(db, 72, 72, 0))
    show("outside everything (30,30)",
         osmface.query_faces_at_point(db, 30, 30, 0))

    print("revision 0 range answers:")
    show("box wholly inside the hole, clear of the island",
         osmface.query_faces_in_range(db, 4.5, 4.5, 5.5, 5.5, 0))
    show("box touching the island corner (5,5)-(6,6)",
         osmface.query_faces_in_range(db, 5, 5, 6, 6, 0))
    show("zero-width range touching x=20",
         osmface.query_faces_in_range(db, 20, -5, 20, 25, 0))
    show("whole window",
         osmface.query_faces_in_range(db, -100, -100, 100, 100, 0))

    print("apply: move the outer right edge from x=20 to x=30 ...")
    receipt = osmface.apply_changes(db, change_file(
        work, "expand.jsonl", "expand", 0, [
            put(1, node(21, 30, 0)), put(1, node(22, 30, 20))]))
    print("  revision:", receipt["revision"],
          "spatial_index:", receipt["spatial_index"])
    probe = (25, 1)
    print(f"same point {probe} across revisions:")
    show("old revision 0 (frozen)", osmface.query_faces_at_point(db, *probe, 0))
    show("new revision 1", osmface.query_faces_at_point(db, *probe, 1))
    print("done.")


if __name__ == "__main__":
    main()

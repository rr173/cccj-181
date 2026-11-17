"""Deterministic builders for the hand-written history world.

The world (all areas exact):

  rectangle way 10 on nodes 1..4  (0,0)-(10,0)-(10,10)-(0,10), closed
  relation A (100): outer way 10
  relation P (200): outer relation A
  relation U (300): an entirely separate closed way 90 on nodes 9..12
  relation G (400): references missing way 77 (gap)

Nothing here imports the production change/dependency/history machinery;
tests use these plain dict builders to write JSONL inputs.
"""
from __future__ import annotations

import json
import os


def node(i, x, y):
    return {"type": "node", "id": i, "x": x, "y": y}


def way(i, nodes):
    return {"type": "way", "id": i, "nodes": nodes}


def rel(i, members):
    return {"type": "relation", "id": i, "members": members}


def wm(ref, role="outer"):
    return {"type": "way", "ref": ref, "role": role}


def rm(ref, role="outer"):
    return {"type": "relation", "ref": ref, "role": role}


def put(expected_version, entity):
    return {"op": "put", "expected_version": expected_version, "entity": entity}


def delete(expected_version, kind, entity_id):
    return {"op": "delete", "expected_version": expected_version,
            "type": kind, "id": entity_id}


def header(batch_id, base_revision):
    return {"batch_id": batch_id, "base_revision": base_revision}


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    return path


def change_file(directory, name, batch_id, base_revision, changes):
    return write_jsonl(
        os.path.join(directory, name),
        [header(batch_id, base_revision), *changes],
    )


def baseline_records():
    return [
        node(1, 0, 0), node(2, 10, 0), node(3, 10, 10), node(4, 0, 10),
        way(10, [1, 2, 3, 4, 1]),
        rel(100, [wm(10)]),
        rel(200, [rm(100)]),
        node(9, 50, 50), node(10, 60, 50), node(11, 60, 60), node(12, 50, 60),
        way(90, [9, 10, 11, 12, 9]),
        rel(300, [wm(90)]),
        rel(400, [wm(77)]),
    ]


def build_warehouse(tmp_path, osmface):
    """load + assemble + enable history; return (db_path, src_path)."""
    src = write_jsonl(os.path.join(tmp_path, "world.jsonl"), baseline_records())
    db = os.path.join(tmp_path, "world.db")
    osmface.load_database(db, [src], force=True)
    osmface.assemble_database(db)
    osmface.enable_history(db)
    return db, src


def shoelace(coords):
    """Signed area of an open ring of (x, y) points (shoelace formula)."""
    area = 0.0
    for (x1, y1), (x2, y2) in zip(coords, coords[1:] + coords[:1]):
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0

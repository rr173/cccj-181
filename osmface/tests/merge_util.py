"""Deterministic builders for offline work-copy / merge tests.

Nothing here imports the production workcopy/merge machinery; tests use
these plain dict builders to write JSONL/JSON inputs.
"""
from __future__ import annotations

import json
import os


def local(name):
    return {"local": name}


def lnode(local_id, x, y):
    return {"type": "node", "local_id": local_id, "x": x, "y": y}


def lway(local_id, nodes):
    return {"type": "way", "local_id": local_id, "nodes": nodes}


def lrel(local_id, members):
    return {"type": "relation", "local_id": local_id, "members": members}


def lm(ref, role="outer"):
    """Member referencing a field-local way/relation."""
    return {"type": "way", "ref": local(ref), "role": role}


def lrm(ref, role="outer"):
    return {"type": "relation", "ref": local(ref), "role": role}


def edit_header(edit_id, base_edit):
    return {"edit_id": edit_id, "base_edit": base_edit}


def edit_file(directory, name, edit_id, base_edit, rows):
    path = os.path.join(str(directory), name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(edit_header(edit_id, base_edit),
                            separators=(",", ":")) + "\n")
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    return path


def rulings_file(directory, name, rulings):
    path = os.path.join(str(directory), name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"rulings": rulings}, fh, indent=2, sort_keys=True)
    return path

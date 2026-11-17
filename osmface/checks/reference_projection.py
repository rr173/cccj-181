#!/usr/bin/env python3
"""Independent reference projection of change-batch semantics.

This is a deliberately tiny, stdlib-only re-implementation of what a batch
*means* for the set of live entities.  It does NOT import any osmface
change/dependency/history-reading code: no ``osmface.history``, no
``osmface.assemble``, no versioned storage.  It only folds put/delete
operations onto an in-memory entity map, checking the same expected_version
and tombstone rules, and emits the effective entity records at every
revision.

A cross-check test feeds those projections into the *ordinary* full
assembler on a fresh staging database, so the incremental warehouse and a
from-scratch assembly of the same entities can be compared revision by
revision.

Usage:
    python3 checks/reference_projection.py world.jsonl \\
        batch1.jsonl batch2.jsonl --out-dir /tmp/proj
writes /tmp/proj/rev0.jsonl, rev1.jsonl, ...
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _valid_entity(entity):
    """Mirror the input contract just enough to reject malformed entities."""
    if not isinstance(entity, dict):
        return False
    kind = entity.get("type")
    if kind not in ("node", "way", "relation") or not _is_int(entity.get("id")):
        return False
    if kind == "node":
        return all(isinstance(entity.get(c), (int, float))
                   and not isinstance(entity.get(c), bool)
                   for c in ("x", "y"))
    if kind == "way":
        nodes = entity.get("nodes")
        return (isinstance(nodes, list) and len(nodes) >= 2
                and all(_is_int(n) for n in nodes))
    members = entity.get("members")
    if not isinstance(members, list):
        return False
    return all(
        isinstance(m, dict)
        and m.get("type") in ("way", "relation")
        and _is_int(m.get("ref"))
        and m.get("role") in ("outer", "inner")
        for m in members
    )


class ReferenceProjection:
    """Folds batches onto {(type, id): (version, deleted, record)}."""

    def __init__(self):
        # (type, id) -> [version, deleted, record_or_None]
        self.entities = {}
        self.revisions = []  # effective record snapshots, one per revision

    def load_baseline(self, baseline_path):
        records = []
        with open(baseline_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        for rec in records:
            key = (rec["type"], rec["id"])
            if key in self.entities and self.entities[key][2] == rec:
                continue
            assert key not in self.entities, f"baseline conflict {key}"
            self.entities[key] = [1, False, rec]
        self.revisions.append(self._snapshot())

    def apply_batch(self, path):
        with open(path, "rb") as fh:
            raw = fh.read()
        lines = [ln for ln in raw.decode("utf-8").splitlines() if ln.strip()]
        header = json.loads(lines[0])
        assert set(header) == {"batch_id", "base_revision"}
        seen = set()
        for line in lines[1:]:
            rec = json.loads(line)
            op = rec["op"]
            assert _is_int(rec["expected_version"]) and rec["expected_version"] >= 0
            if op == "put":
                entity = rec["entity"]
                assert _valid_entity(entity)
                key = (entity["type"], entity["id"])
                assert key not in seen, f"duplicate object {key} in batch"
                seen.add(key)
                cur = self.entities.get(key)
                if cur is None:
                    assert rec["expected_version"] == 0
                    self.entities[key] = [1, False, entity]
                else:
                    assert rec["expected_version"] == cur[0]
                    # a put on a tombstone is a restore keeping the version line
                    self.entities[key] = [cur[0] + 1, False, entity]
            else:
                assert rec["type"] in ("node", "way", "relation")
                key = (rec["type"], rec["id"])
                assert key not in seen, f"duplicate object {key} in batch"
                seen.add(key)
                cur = self.entities.get(key)
                assert cur is not None and not cur[1], \
                    f"delete of absent/dead object {key}"
                assert rec["expected_version"] == cur[0]
                self.entities[key] = [cur[0] + 1, True, None]
        self.revisions.append(self._snapshot())
        return header

    def _snapshot(self):
        return [
            {"key": key, "version": state[0], "record": state[2]}
            for key, state in sorted(self.entities.items())
            if not state[1]
        ]

    def effective_records(self, revision):
        return [item["record"] for item in self.revisions[revision]]

    def effective_versions(self, revision):
        return {item["key"]: item["version"] for item in self.revisions[revision]}

    def write_revision(self, out_path, revision):
        with open(out_path, "w", encoding="utf-8") as fh:
            for rec in self.effective_records(revision):
                fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


def project(baseline_path, batch_paths, out_dir):
    proj = ReferenceProjection()
    proj.load_baseline(baseline_path)
    os.makedirs(out_dir, exist_ok=True)
    proj.write_revision(os.path.join(out_dir, "rev0.jsonl"), 0)
    for i, path in enumerate(batch_paths, start=1):
        proj.apply_batch(path)
        proj.write_revision(os.path.join(out_dir, f"rev{i}.jsonl"), i)
    return proj


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("baseline")
    ap.add_argument("batches", nargs="*")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args(argv)
    proj = project(args.baseline, args.batches, args.out_dir)
    for rev, snap in enumerate(proj.revisions):
        print(f"rev{rev}: {len(snap)} live entities -> "
              f"{os.path.join(args.out_dir, f'rev{rev}.jsonl')}")


if __name__ == "__main__":
    sys.exit(main())

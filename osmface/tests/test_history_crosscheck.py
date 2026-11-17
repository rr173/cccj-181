"""Independent cross-check: a tiny reference projection of change semantics
feeds the *ordinary* full assembler; results must match the incremental
warehouse revision by revision.

The reference projection (checks/reference_projection.py) is stdlib-only and
must never import production change/dependency/history code.
"""
import json
import os
import subprocess
import sys

from shapely.geometry import shape

import osmface
from conftest import ROOT
from history_util import (
    baseline_records,
    build_warehouse,
    change_file,
    delete,
    node,
    put,
    rel,
    rm,
    way,
    wm,
    write_jsonl,
)

PROJECTION = os.path.join(ROOT, "checks", "reference_projection.py")


def _batches(tmp_path):
    # (filename, batch_id, base, changes) covering move, swap+delete,
    # restore, gap fill, cycle, relation delete/restore, same-value, reverse
    return [
        ("b1.jsonl", "move", 0, [
            put(1, node(2, 12, 0)), put(1, node(3, 12, 10))]),
        ("b2.jsonl", "swap", 1, [
            put(0, node(21, 100, 100)), put(0, node(22, 120, 100)),
            put(0, node(23, 120, 110)), put(0, node(24, 100, 110)),
            put(0, way(20, [21, 22, 23, 24, 21])),
            put(1, rel(100, [wm(20)])),
            delete(1, "way", 10)]),
        ("b3.jsonl", "restore", 2, [
            put(2, way(10, [1, 2, 3, 4, 1])),
            put(2, node(2, 10, 0))]),
        ("b4.jsonl", "fillgap", 3, [
            put(0, node(71, 0, 200)), put(0, node(72, 5, 200)),
            put(0, node(73, 5, 205)), put(0, node(74, 0, 205)),
            put(0, way(77, [71, 72, 73, 74, 71]))]),
        ("b5.jsonl", "cycle", 4, [
            put(0, rel(500, [rm(501)])), put(0, rel(501, [rm(500)]))]),
        ("b6.jsonl", "break-cycle", 5, [
            put(1, rel(501, [wm(20)]))]),
        ("b7.jsonl", "del-rel", 6, [
            delete(2, "relation", 501)]),
        ("b8.jsonl", "restore-rel", 7, [
            put(3, rel(501, [wm(20)]))]),
        ("b9.jsonl", "same-value", 8, [
            put(1, node(21, 100, 100))]),
        ("b10.jsonl", "reverse", 9, [
            put(1, way(20, [21, 24, 23, 22, 21]))]),
    ]


def test_projection_module_is_independent():
    text = open(PROJECTION, encoding="utf-8").read()
    # it may not import the package or touch any production storage/dep code
    for forbidden in ("import osmface", "from osmface", "ref_edge_history",
                      "entity_history", "rel_result_history",
                      "_affected_relations", "RevisionView", "apply_changes"):
        assert forbidden not in text, f"reference projection must not use {forbidden}"


def test_every_revision_matches_a_full_assembly_of_the_projection(tmp_path):
    db, src = build_warehouse(tmp_path, osmface)
    batch_paths = []
    for name, bid, base, changes in _batches(tmp_path):
        path = change_file(tmp_path, name, bid, base, changes)
        batch_paths.append(path)
        osmface.apply_changes(db, path)

    proj_dir = os.path.join(tmp_path, "proj")
    subprocess.run(
        [sys.executable, PROJECTION, src, *batch_paths, "--out-dir", proj_dir],
        check=True, capture_output=True, text=True,
    )

    n_revisions = len(batch_paths) + 1
    for revision in range(n_revisions):
        # full, ordinary assembly of the projected effective entities
        ref_db = os.path.join(tmp_path, f"ref-r{revision}.db")
        osmface.load_database(
            ref_db, [os.path.join(proj_dir, f"rev{revision}.jsonl")], force=True)
        osmface.assemble_database(ref_db)
        ref_info = osmface.inspect_database(ref_db)

        # the incremental warehouse at the same revision
        wh_info = osmface.inspect_database(db, revision=revision)
        assert ref_info["counts"] == wh_info["counts"], revision

        ref_rows = _status_map(ref_db)
        wh_overview = osmface.inspect_database(db, revision=revision)
        # the warehouse keeps absent tombstones; the fresh projection omits
        # them, so compare the live sets and check the absent set separately
        wh_live, wh_absent = set(), set()
        for rid in wh_overview["relations"]:
            detail = osmface.inspect_database(
                db, relation_id=rid, revision=revision)["relation"]
            (wh_absent if detail["status"] == "absent" else wh_live).add(rid)
        assert set(ref_rows) == wh_live, revision
        # every absent warehouse relation is genuinely absent from projection
        for rid in wh_absent:
            assert rid not in ref_rows, (revision, rid)
        for rid, ref_status in ref_rows.items():
            wh_detail = osmface.inspect_database(
                db, relation_id=rid, revision=revision)["relation"]
            assert wh_detail["status"] == ref_status, (revision, rid)
            if ref_status == "complete":
                ref_detail = osmface.inspect_database(
                    ref_db, relation_id=rid)["relation"]
                ref_geo = _multi(ref_detail)
                wh_geo = _multi(wh_detail)
                # topological equality, not byte equality
                assert ref_geo.equals(wh_geo), (revision, rid)


def test_projected_versions_match_warehouse_object_versions(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "checks"))
    from reference_projection import ReferenceProjection  # stdlib-only module

    db, src = build_warehouse(tmp_path, osmface)
    batch_paths = []
    for name, bid, base, changes in _batches(tmp_path):
        path = change_file(tmp_path, name, bid, base, changes)
        batch_paths.append(path)
        osmface.apply_changes(db, path)

    proj = ReferenceProjection()
    proj.load_baseline(src)
    for path in batch_paths:
        proj.apply_batch(path)

    import sqlite3
    conn = sqlite3.connect(db)
    for revision in range(len(batch_paths) + 1):
        for key, version in proj.effective_versions(revision).items():
            kind, entity_id = key
            row = conn.execute(
                "SELECT version FROM entity_history h WHERE type=? AND id=?"
                " AND rev<=? AND deleted=0 ORDER BY version DESC LIMIT 1",
                (kind, entity_id, revision),
            ).fetchone()
            assert row is not None and row[0] == version, (revision, key)
    conn.close()


def _status_map(db_path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    rows = {r[0]: r[1] for r in conn.execute(
        "SELECT relation_id, status FROM relation_status")}
    conn.close()
    return rows


def _multi(detail):
    from shapely.geometry import MultiPolygon, shape
    polys = [shape(f["geometry"]) for f in detail["faces"]]
    return MultiPolygon(polys) if len(polys) > 1 else polys[0]

"""Historical reads, revision pinning and revision-bound exports."""
import json
import os
import sqlite3

import pytest
from shapely.geometry import shape

import osmface
from history_util import build_warehouse, change_file, node, put, wm


def _area(detail):
    return shape(detail["faces"][0]["geometry"]).area


def _two_revisions(tmp_path):
    db, _ = build_warehouse(tmp_path, osmface)
    osmface.apply_changes(db, change_file(
        tmp_path, "move.jsonl", "move", 0, [
            put(1, node(2, 12, 0)), put(1, node(3, 12, 10))]))
    return db


def test_inspect_binds_everything_to_one_revision(tmp_path):
    db = _two_revisions(tmp_path)
    old = osmface.inspect_database(db, relation_id=100, revision=0)["relation"]
    new = osmface.inspect_database(db, relation_id=100, revision=1)["relation"]
    assert _area(old) == 100.0 and _area(new) == 120.0
    # member index path, way node order and reversal all match that revision
    seg_old = old["faces"][0]["rings"][0]["segments"][0]
    assert seg_old["way_definition_nodes"] == [1, 2, 3, 4, 1]
    assert seg_old["path"] == [[100, 0]]
    # complete-face sources name relation, way and actual node versions
    assert seg_old["way_version"] == 1
    assert [nv[1] for nv in seg_old["node_versions"]] == [1, 1, 1, 1, 1]


def test_unmoved_entities_keep_their_old_version(tmp_path):
    db = _two_revisions(tmp_path)
    new = osmface.inspect_database(db, relation_id=300, revision=1)["relation"]
    # U's way/nodes were never modified: still object version 1
    seg = new["faces"][0]["rings"][0]["segments"][0]
    assert seg["way_version"] == 1
    assert all(v == 1 for _, v in seg["node_versions"])


def test_head_pin_is_fixed_once_per_query(tmp_path):
    db = _two_revisions(tmp_path)
    info = osmface.inspect_database(db)  # head omitted -> pinned once
    assert info["revision"] == info["head_revision"] == 1
    with pytest.raises(osmface.HistoryError):
        osmface.inspect_database(db, revision=99)


def test_history_survives_process_reopen(tmp_path):
    db = _two_revisions(tmp_path)
    import subprocess
    import sys
    from conftest import ROOT

    env = dict(os.environ, PYTHONPATH=ROOT)
    proc = subprocess.run(
        [sys.executable, "-m", "osmface", "inspect", db,
         "--relation", "100", "--revision", "0", "--json"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    detail = json.loads(proc.stdout)["relation"]
    assert _area(detail) == 100.0  # old revision readable without input files


def test_export_is_bound_to_revision_and_batch(tmp_path):
    db = _two_revisions(tmp_path)
    out0 = os.path.join(tmp_path, "pkg0")
    manifest = osmface.export_revision_package(db, out0, revision=0)
    assert manifest["state"] == "complete"
    assert manifest["revision"] == 0
    assert manifest["revision_identity"]["kind"] == "baseline"
    lines = [json.loads(x) for x in
             open(os.path.join(out0, "relations.jsonl")) if x.strip()]
    a = [r for r in lines if r["relation"] == 100][0]
    assert _area({"faces": a["faces"]}) == 100.0
    # full provenance travels with the package, including object versions
    seg = a["faces"][0]["rings"][0]["segments"][0]
    assert seg["way_version"] == 1 and seg["node_versions"]
    rev_json = json.load(open(os.path.join(out0, "revision.json")))
    assert rev_json["revision"] == 0
    batches = [json.loads(x) for x in
               open(os.path.join(out0, "batches.jsonl")) if x.strip()]
    assert batches == []  # no batches at revision 0


def test_exported_directory_immutable_after_later_apply(tmp_path):
    db = _two_revisions(tmp_path)
    out0 = os.path.join(tmp_path, "pkg0")
    osmface.export_revision_package(db, out0, revision=0)
    before = open(os.path.join(out0, "relations.jsonl")).read()
    # another commit must not touch the already-exported directory
    osmface.apply_changes(db, change_file(
        tmp_path, "c2.jsonl", "c2", 1, [put(2, node(2, 13, 0))]))
    assert open(os.path.join(out0, "relations.jsonl")).read() == before
    # head export records the producing batch id
    out1 = os.path.join(tmp_path, "pkg1")
    man1 = osmface.export_revision_package(db, out1)
    assert man1["revision_identity"]["batch_id"] == "c2"
    batches = [json.loads(x) for x in
               open(os.path.join(out1, "batches.jsonl")) if x.strip()]
    assert [b["batch_id"] for b in batches] == ["move", "c2"]


def test_export_failure_leaves_no_usable_package_and_keeps_data(tmp_path):
    db = _two_revisions(tmp_path)
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    bad_out = str(blocker / "pkg")
    with pytest.raises(osmface.OsmFaceError):
        osmface.export_revision_package(db, bad_out)
    assert not os.path.exists(bad_out)
    # the committed revisions are untouched; export retries at a fixed path
    good = os.path.join(tmp_path, "good")
    manifest = osmface.export_revision_package(db, good, revision=1)
    assert manifest["state"] == "complete"


def test_export_does_not_backfill_from_current_tables(tmp_path):
    db = _two_revisions(tmp_path)
    out0 = os.path.join(tmp_path, "pkg0")
    osmface.export_revision_package(db, out0, revision=0)
    # corrupt nothing: instead assert rev0 package node versions are all 1
    lines = [json.loads(x) for x in
             open(os.path.join(out0, "relations.jsonl")) if x.strip()]
    for line in lines:
        if line["status"] == "complete":
            for face in line["faces"]:
                for ring in face["rings"]:
                    for seg in ring["segments"]:
                        assert all(v == 1 for _, v in seg["node_versions"])


def test_receipt_round_trip(tmp_path):
    db = _two_revisions(tmp_path)
    receipt = osmface.get_receipt(db, "move")
    assert receipt["base_revision"] == 0 and receipt["revision"] == 1
    assert {c["id"] for c in receipt["changes"]} == {2, 3}
    rels = {r["relation"]: r for r in receipt["affected_relations"]}
    assert rels[100]["status_before"] == "complete"
    assert rels[100]["status_after"] == "complete"
    assert rels[100]["is_new"] is False and rels[100]["is_deleted"] is False

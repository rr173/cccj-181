"""Boundary-size limits, package state machine, CLI end-to-end."""
import json
import os

import pytest

import osmface
from conftest import fixture, relation, run_cli


def test_boundary_node_limit(loaded):
    db = loaded("base.jsonl")
    summary = osmface.assemble_database(db, max_boundary_nodes=10)
    assert summary["status_counts"]["incomplete"] == 1
    rel = relation(db, 200)
    assert rel["status"] == "incomplete"
    assert rel["reason"]["code"] == "boundary_limit_exceeded"
    assert rel["faces"] == []  # never a truncated "success"
    # the small sub-relation is still within the limit and completes
    assert relation(db, 100)["status"] == "complete"


def test_boundary_way_limit(loaded):
    db = loaded("base.jsonl")
    osmface.assemble_database(db, max_boundary_ways=3)
    assert relation(db, 200)["reason"]["code"] == "boundary_limit_exceeded"
    assert relation(db, 100)["status"] == "complete"


def test_package_export_and_manifest(loaded, tmp_path):
    db = loaded("base.jsonl")
    out = str(tmp_path / "pkg")
    summary = osmface.assemble_database(db, out_dir=out)
    assert summary["package"]["state"] == "complete"

    manifest = json.load(open(os.path.join(out, "manifest.json")))
    assert manifest["state"] == "complete"
    assert manifest["counts"]["complete"] == 2

    lines = [json.loads(x) for x in
             open(os.path.join(out, "relations.jsonl")) if x.strip()]
    by_id = {r["relation"]: r for r in lines}
    assert by_id[200]["status"] == "complete"
    assert by_id[200]["geometry"]["type"] == "MultiPolygon"
    assert len(by_id[200]["geometry"]["coordinates"]) == 2
    assert by_id[100]["geometry"]["type"] == "Polygon"
    # provenance is part of the package
    seg = by_id[200]["faces"][0]["rings"][0]["segments"][0]
    assert seg["way"] == 1 and seg["path"] == [[200, 1]]

    info = osmface.inspect_package(out)
    assert info["manifest"]["state"] == "complete"
    assert info["relations_file_lines"] == 2


def test_package_replace_requires_force(loaded, tmp_path):
    db = loaded("base.jsonl")
    out = str(tmp_path / "pkg")
    osmface.assemble_database(db, out_dir=out)
    with pytest.raises(osmface.OsmFaceError):
        osmface.assemble_database(db, out_dir=out)
    osmface.assemble_database(db, out_dir=out, force=True)  # ok with force


def test_package_write_failure_not_complete(loaded, tmp_path):
    db = loaded("base.jsonl")
    blocker = tmp_path / "blocker"
    blocker.write_text("a regular file, not a directory")
    out = str(blocker / "pkg")  # parent is a file -> export must fail
    with pytest.raises(osmface.OsmFaceError):
        osmface.assemble_database(db, out_dir=out)
    assert not os.path.exists(out)
    # staging results remain queryable; nothing was exposed as a package
    assert relation(db, 200)["status"] == "complete"


def test_cli_end_to_end(tmp_path):
    db = str(tmp_path / "e2e.db")
    out = str(tmp_path / "out")
    assert run_cli("load", db, fixture("base.jsonl")).returncode == 0
    proc = run_cli("assemble", db, "--out", out)
    assert proc.returncode == 0, proc.stderr
    assert "complete=2" in proc.stdout
    proc = run_cli("inspect", db)
    assert proc.returncode == 0 and "status_counts" in proc.stdout
    proc = run_cli("inspect", db, "--relation", "200")
    assert proc.returncode == 0
    assert "way=5" in proc.stdout and "reversed=True" in proc.stdout
    proc = run_cli("inspect", db, "--relation", "200", "--json")
    detail = json.loads(proc.stdout)["relation"]
    assert detail["status"] == "complete"
    proc = run_cli("inspect", "--package", out)
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["manifest"]["state"] == "complete"

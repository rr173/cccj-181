"""Load-time behaviour: duplicate merging, conflicting definitions,
staging state machine, and the CLI contract."""
import pytest

import osmface
from conftest import fixture, run_cli


def test_identical_duplicates_merge(loaded):
    db = loaded("dup_identical.jsonl")
    info = osmface.inspect_database(db)
    assert info["staging_state"] == "loaded"
    assert info["merged_duplicates"] == 2
    assert info["fault_count"] == 0
    # content is exactly the base dataset
    assert info["counts"] == {"node": 12, "way": 6, "relation": 2}


def test_conflicting_definition_fails_load(tmp_path):
    db = str(tmp_path / "conflict.db")
    summary = osmface.load_database(db, [fixture("dup_conflict.jsonl")])
    assert summary["staging_state"] == "failed"
    assert summary["faults"] == 1
    info = osmface.inspect_database(db, faults=True)
    assert info["faults"][0]["kind"] == "conflicting_definition"
    # corrupted input must never be assembled into a package
    with pytest.raises(osmface.OsmFaceError):
        osmface.assemble_database(db, out_dir=str(tmp_path / "out"))
    assert not (tmp_path / "out").exists()


def test_malformed_records_are_faults(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        '{"type":"node","id":1,"x":0,"y":0}\n'
        '{"type":"node","id":2,"x":0}\n'                 # missing y
        '{"type":"way","id":1,"nodes":[1]}\n'            # too few nodes
        '{"type":"relation","id":1,"members":[{"type":"node","ref":1,"role":"outer"}]}\n'
        'not json at all\n'
        '{"type":"node","id":3,"x":0,"y":0}\n',
        encoding="utf-8",
    )
    db = str(tmp_path / "bad.db")
    summary = osmface.load_database(db, [str(bad)])
    assert summary["staging_state"] == "failed"
    assert summary["faults"] == 4
    assert summary["counts"]["node"] == 2  # the two valid nodes still staged


def test_cli_load_exit_codes(tmp_path):
    db = str(tmp_path / "ok.db")
    proc = run_cli("load", db, fixture("base.jsonl"))
    assert proc.returncode == 0, proc.stderr
    proc = run_cli("load", db, fixture("base.jsonl"))
    assert proc.returncode != 0  # exists, needs --force
    proc = run_cli("load", "--force", db, fixture("dup_conflict.jsonl"))
    assert proc.returncode == 2  # domain failure: input faults


def test_cli_assemble_refuses_failed_staging(tmp_path):
    db = str(tmp_path / "failed.db")
    assert run_cli("load", db, fixture("dup_conflict.jsonl")).returncode == 2
    proc = run_cli("assemble", db, "--out", str(tmp_path / "out"))
    assert proc.returncode == 2
    assert "staging" in proc.stderr

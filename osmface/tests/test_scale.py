"""Runtime-generated scale data: selective fetching and limits at size."""
import os
import subprocess
import sys

import osmface
from conftest import ROOT, relation

GEN = os.path.join(ROOT, "fixtures", "gen_scale.py")

UNUSED = 30_000
RING_NODES = 5_000
GRID = 20  # 400 square relations


def _generate(tmp_path):
    path = str(tmp_path / "scale.jsonl")
    subprocess.run(
        [sys.executable, GEN, path, "--unused", str(UNUSED),
         "--ring-nodes", str(RING_NODES), "--grid", str(GRID)],
        check=True, capture_output=True, text=True,
    )
    return path


def test_scale_assemble_selective_fetch(tmp_path):
    path = _generate(tmp_path)
    db = str(tmp_path / "scale.db")
    load_summary = osmface.load_database(db, [path])
    total_nodes = load_summary["counts"]["node"]
    assert total_nodes == UNUSED + RING_NODES + GRID * GRID * 4

    summary = osmface.assemble_database(db)
    assert summary["status_counts"] == {
        "complete": GRID * GRID + 1, "incomplete": 0, "topology_conflict": 0,
    }
    # assembly touched only the referenced boundary nodes, not the whole map
    fetched = summary["fetched"]["node_rows"]
    assert fetched == RING_NODES + GRID * GRID * 4
    assert fetched < total_nodes // 4

    big = relation(db, 900000)
    assert big["status"] == "complete"
    ring = big["faces"][0]["rings"][0]
    assert len(ring["nodes"]) == RING_NODES + 1
    assert ring["nodes"][0] == ring["nodes"][-1] == min(ring["nodes"])


def test_scale_boundary_limit(tmp_path):
    path = _generate(tmp_path)
    db = str(tmp_path / "scale_limit.db")
    osmface.load_database(db, [path])
    summary = osmface.assemble_database(db, max_boundary_nodes=1000)
    assert summary["status_counts"]["incomplete"] == 1
    big = relation(db, 900000)
    assert big["reason"]["code"] == "boundary_limit_exceeded"
    assert big["faces"] == []
    # small relations are unaffected by the big one's limit
    assert relation(db, 800000)["status"] == "complete"

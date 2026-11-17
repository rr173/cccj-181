"""Fault isolation and parent-propagation fixtures."""
import osmface
from conftest import relation


def _assemble(loaded, name):
    db = loaded(name)
    osmface.assemble_database(db)
    return db


def test_missing_node_gaps_and_propagation(loaded):
    db = _assemble(loaded, "missing_node.jsonl")
    rel = relation(db, 200)
    assert rel["status"] == "incomplete"
    assert rel["reason"]["code"] == "missing_references"
    gaps = {(g["kind"], g["id"]) for g in rel["reason"]["gaps"]}
    assert ("node", 2) in gaps
    # the gap is reachable through both ways that reference node 2
    paths = {tuple(map(tuple, g["path"])) for g in rel["reason"]["gaps"]
             if g["id"] == 2}
    assert ((200, 1),) in paths and ((200, 4),) in paths

    # parent relation 300 inherits the gap with the chain prefixed
    top = relation(db, 300)
    assert top["status"] == "incomplete"
    assert top["reason"]["code"] == "missing_references"
    top_paths = {tuple(map(tuple, g["path"])) for g in top["reason"]["gaps"]}
    assert ((300, 0), (200, 1)) in top_paths
    assert any(c["relation"] == 200 for c in top["reason"]["children"])

    # the unaffected sub-relation still assembles
    assert relation(db, 100)["status"] == "complete"


def test_relation_cycle_isolated_with_chain(loaded):
    db = _assemble(loaded, "cycle.jsonl")
    for rid in (400, 401):
        rel = relation(db, rid)
        assert rel["status"] == "incomplete"
        assert rel["reason"]["code"] == "relation_cycle"
        chain = rel["reason"]["chain"]
        assert chain[0] == chain[-1] and len(chain) == 3
        assert set(chain) == {400, 401}
    self_loop = relation(db, 403)
    assert self_loop["reason"]["code"] == "relation_cycle"
    assert self_loop["reason"]["chain"] == [403, 403]
    # parent closure of the cycle is isolated too
    parent = relation(db, 402)
    assert parent["status"] == "incomplete"
    assert parent["reason"]["code"] == "subrelation_unresolved"
    assert any(c["relation"] == 400 for c in parent["reason"]["children"])
    # empty relation is reported, not crashed on
    assert relation(db, 404)["reason"]["code"] == "empty_relation"
    # unrelated relations are unaffected
    assert relation(db, 200)["status"] == "complete"


def test_same_coords_different_node_ids_do_not_close(loaded):
    db = _assemble(loaded, "dup_endpoint_coords.jsonl")
    rel = relation(db, 200)
    assert rel["status"] == "topology_conflict"
    assert rel["reason"]["code"] == "endpoint_degree"
    endpoints = {e["node"]: e["degree"] for e in rel["reason"]["endpoints"]}
    assert endpoints[2] == 1 and endpoints[22] == 1
    assert relation(db, 100)["status"] == "complete"


def test_three_way_junction_rejected(loaded):
    db = _assemble(loaded, "junction.jsonl")
    rel = relation(db, 200)
    assert rel["status"] == "topology_conflict"
    assert rel["reason"]["code"] == "endpoint_degree"
    endpoints = {e["node"]: e["degree"] for e in rel["reason"]["endpoints"]}
    assert endpoints[2] == 3
    assert endpoints[13] == 1


def test_shared_way_not_deduplicated(loaded):
    db = _assemble(loaded, "shared_way.jsonl")
    for rid, member_index in ((500, 1), (501, 3)):
        rel = relation(db, rid)
        assert rel["status"] == "complete"
        (face,) = rel["faces"]
        shell = face["rings"][0]
        shared = [s for s in shell["segments"] if s["way"] == 12]
        assert len(shared) == 1
        assert shared[0]["path"] == [[rid, member_index]]
        assert shared[0]["nodes"] in ([14, 15], [15, 14])
        from shapely.geometry import shape
        assert shape(face["geometry"]).area == 50.0
    # same way twice in one relation: duplicate fragment -> conflict
    dup = relation(db, 502)
    assert dup["status"] == "topology_conflict"
    assert dup["reason"]["code"] == "degenerate_ring"


def test_touching_boundaries_rejected_and_propagated(loaded):
    db = _assemble(loaded, "touching.jsonl")
    rel = relation(db, 100)
    assert rel["status"] == "topology_conflict"
    assert rel["reason"]["code"] == "boundary_intersect"
    # parent cannot complete when a child is conflicted
    top = relation(db, 200)
    assert top["status"] == "incomplete"
    assert top["reason"]["code"] == "subrelation_unresolved"
    assert any(c["relation"] == 100 for c in top["reason"]["children"])

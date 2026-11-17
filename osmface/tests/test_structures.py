"""Additional structural fixtures: missing references, shared child
compilation, ring/nesting rule violations."""
import osmface
from conftest import relation


def _assemble(loaded, name):
    db = loaded(name)
    osmface.assemble_database(db)
    return db


def test_missing_way_and_relation_references(loaded):
    db = _assemble(loaded, "missing_node.jsonl")
    rel = relation(db, 600)
    assert rel["status"] == "incomplete"
    assert rel["reason"]["code"] == "missing_references"
    gaps = {(g["kind"], g["id"]) for g in rel["reason"]["gaps"]}
    assert ("way", 999) in gaps
    assert ("relation", 998) in gaps
    for g in rel["reason"]["gaps"]:
        assert g["path"][0][0] == 600


def test_shared_subrelation_keeps_parent_provenance(loaded):
    db = _assemble(loaded, "shared_child.jsonl")
    for rid, member_index in ((200, 2), (210, 1)):
        rel = relation(db, rid)
        assert rel["status"] == "complete"
        assert len(rel["faces"]) == 2
        (hole_face,) = [f for f in rel["faces"] if len(f["rings"]) == 2]
        (island_face,) = [f for f in rel["faces"] if len(f["rings"]) == 1]
        hole = hole_face["rings"][1]
        island = island_face["rings"][0]
        # same compiled child C, but each parent keeps its own chain
        assert hole["segments"][0]["path"] == [[rid, member_index], [100, 0]]
        assert island["segments"][0]["path"] == [[rid, member_index], [100, 1]]
    from shapely.geometry import shape
    areas = sorted(
        shape(f["geometry"]).area for f in relation(db, 210)["faces"]
    )
    assert areas == [4.0, 884.0]
    assert relation(db, 100)["status"] == "complete"


def test_self_intersecting_ring_rejected(loaded):
    db = _assemble(loaded, "bad_rings.jsonl")
    rel = relation(db, 610)
    assert rel["status"] == "topology_conflict"
    assert rel["reason"]["code"] == "ring_self_intersection"


def test_toplevel_inner_rejected(loaded):
    db = _assemble(loaded, "bad_rings.jsonl")
    rel = relation(db, 611)
    assert rel["status"] == "topology_conflict"
    assert rel["reason"]["code"] == "toplevel_not_outer"


def test_role_alternation_rejected(loaded):
    db = _assemble(loaded, "bad_rings.jsonl")
    rel = relation(db, 612)
    assert rel["status"] == "topology_conflict"
    assert rel["reason"]["code"] == "role_alternation"

"""Base fixture: hand-computed rings, roles, areas and per-segment provenance.

Manual expectation (see fixtures/make_fixtures.py for the layout):

  relation 200 (R): square 100 - hole 16 + island 4 = 88, two faces
    face0 shell outer [1,2,3,4,1]  (ways 1 fwd, 2 rev, 3 fwd, 4 rev)
          hole  inner [5,8,7,6,5]  (way 5 reversed, via C member 0)
    face1 shell outer [9,10,11,12,9] (way 6 forward, via C member 1)
  relation 100 (C): hole 16 - island 4 = 12, one face
    shell outer [5,6,7,8,5]  (way 5 forward)
    hole  inner [9,12,11,10,9] (way 6 reversed)
"""
from shapely.geometry import shape

import osmface
from conftest import relation


def _assemble(loaded):
    db = loaded("base.jsonl")
    summary = osmface.assemble_database(db)
    return db, summary


def test_load_counts(loaded):
    db = loaded("base.jsonl")
    info = osmface.inspect_database(db)
    assert info["staging_state"] == "loaded"
    assert info["counts"] == {"node": 12, "way": 6, "relation": 2}
    assert info["fault_count"] == 0


def test_status_counts(loaded):
    _, summary = _assemble(loaded)
    assert summary["status_counts"] == {
        "complete": 2, "incomplete": 0, "topology_conflict": 0,
    }


def test_face_areas_total_88(loaded):
    db, _ = _assemble(loaded)
    rel = relation(db, 200)
    assert rel["status"] == "complete"
    assert len(rel["faces"]) == 2
    areas = sorted(shape(f["geometry"]).area for f in rel["faces"])
    assert areas == [4.0, 84.0]
    assert sum(areas) == 88.0


def test_ring_node_orders_and_roles(loaded):
    db, _ = _assemble(loaded)
    faces = relation(db, 200)["faces"]
    # faces are deterministically ordered by the shell's smallest node id
    shell, hole = faces[0]["rings"]
    assert shell["role"] == "outer" and shell["hole"] is False
    assert shell["nodes"] == [1, 2, 3, 4, 1]
    assert hole["role"] == "inner" and hole["hole"] is True
    assert hole["nodes"] == [5, 8, 7, 6, 5]
    (island,) = faces[1]["rings"]
    assert island["role"] == "outer" and island["hole"] is False
    assert island["nodes"] == [9, 10, 11, 12, 9]


def test_square_segment_provenance(loaded):
    db, _ = _assemble(loaded)
    shell = relation(db, 200)["faces"][0]["rings"][0]
    segs = shell["segments"]
    assert [(s["way"], s["reversed"]) for s in segs] == [
        (1, False), (2, True), (3, False), (4, True),
    ]
    assert [s["path"] for s in segs] == [
        [[200, 1]], [[200, 4]], [[200, 0]], [[200, 3]],
    ]
    assert [s["nodes"] for s in segs] == [[1, 2], [2, 3], [3, 4], [4, 1]]
    # original way definitions stay untouched and traceable
    assert [s["way_definition_nodes"] for s in segs] == [
        [1, 2], [3, 2], [3, 4], [1, 4],
    ]


def test_nested_role_flip_provenance(loaded):
    db, _ = _assemble(loaded)
    faces = relation(db, 200)["faces"]
    hole_seg = faces[0]["rings"][1]["segments"][0]
    assert hole_seg["way"] == 5
    assert hole_seg["path"] == [[200, 2], [100, 0]]  # R.inner -> C member 0
    assert hole_seg["reversed"] is True              # inner rings are CW
    assert hole_seg["nodes"] == [5, 8, 7, 6, 5]
    island_seg = faces[1]["rings"][0]["segments"][0]
    assert island_seg["way"] == 6
    assert island_seg["path"] == [[200, 2], [100, 1]]  # R.inner -> C member 1
    assert island_seg["reversed"] is False
    assert island_seg["nodes"] == [9, 10, 11, 12, 9]


def test_subrelation_own_faces(loaded):
    db, _ = _assemble(loaded)
    rel = relation(db, 100)
    assert rel["status"] == "complete"
    (face,) = rel["faces"]
    shell, hole = face["rings"]
    assert shell["nodes"] == [5, 6, 7, 8, 5]
    assert shell["segments"][0]["reversed"] is False
    assert hole["nodes"] == [9, 12, 11, 10, 9]
    assert hole["segments"][0]["reversed"] is True
    assert shape(face["geometry"]).area == 12.0

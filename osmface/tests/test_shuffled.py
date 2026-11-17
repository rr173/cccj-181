"""Shuffled fixture: member/definition order shuffled and every way
reversed wholesale — the logical faces must not change."""
from shapely.geometry import MultiPolygon, shape

import osmface
from conftest import relation


def _faces_geometry(faces):
    return MultiPolygon([shape(f["geometry"]) for f in faces])


def test_shuffled_same_faces(loaded):
    db_base = loaded("base.jsonl")
    db_shuf = loaded("shuffled.jsonl")
    osmface.assemble_database(db_base)
    osmface.assemble_database(db_shuf)

    for rid in (100, 200):
        fb = relation(db_base, rid)["faces"]
        fs = relation(db_shuf, rid)["faces"]
        # canonical ring node sequences are identical
        assert [[r["nodes"] for r in f["rings"]] for f in fb] == \
               [[r["nodes"] for r in f["rings"]] for f in fs]
        # geometry is identical
        assert _faces_geometry(fb).equals(_faces_geometry(fs))
        # areas unchanged
        assert sorted(shape(f["geometry"]).area for f in fb) == \
               sorted(shape(f["geometry"]).area for f in fs)


def test_shuffled_provenance_reflects_reversal(loaded):
    db = loaded("shuffled.jsonl")
    osmface.assemble_database(db)
    shell = relation(db, 200)["faces"][0]["rings"][0]
    # ways were reversed wholesale, so the reversed flags flip vs base
    assert [(s["way"], s["reversed"]) for s in shell["segments"]] == [
        (1, True), (2, False), (3, True), (4, False),
    ]
    # member order changed, so the member-index chain reflects the new order
    assert [s["path"] for s in shell["segments"]] == [
        [[200, 3]], [[200, 1]], [[200, 4]], [[200, 2]],
    ]
    assert [s["nodes"] for s in shell["segments"]] == [
        [1, 2], [2, 3], [3, 4], [4, 1],
    ]

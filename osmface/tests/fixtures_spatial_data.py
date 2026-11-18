"""Hand-written spatial world records shared by tests and independent checks.

Kept free of any production import so the independent check script can use
it without pulling in query or index-maintenance code.
"""
from __future__ import annotations


def node(i, x, y):
    return {"type": "node", "id": i, "x": x, "y": y}


def way(i, nodes):
    return {"type": "way", "id": i, "nodes": nodes}


def rel(i, members):
    return {"type": "relation", "id": i, "members": members}


def wm(ref, role="outer"):
    return {"type": "way", "ref": ref, "role": role}


def rm(ref, role="outer"):
    return {"type": "relation", "ref": ref, "role": role}


def spatial_world_records():
    """Outer square with a true empty hole, an independent island, a
    disjoint square, an overlapping square and a two-face MultiPolygon.

        R (400): outer square (0,0)-(20,20), hole (4,4)-(12,12), no inner
                 object -> one face, area 400-64 = 336
        I (800): independent island square (6,6)-(10,10), one face, area 16
        U (600): disjoint square (40,40)-(50,50), one face, area 100
        V (700): overlapping square (15,15)-(25,25), one face, area 100
        M (900): two disjoint outer squares -> MultiPolygon, faces 0 and 1

    Point (5,5) lies in R's hole interior and in no other relation: it must
    return an empty hit set even though every bbox-based candidate index
    includes R's outer face.
    """
    return [
        # R: outer square way 20 + inner hole way 21
        node(20, 0, 0), node(21, 20, 0), node(22, 20, 20), node(23, 0, 20),
        way(20, [20, 21, 22, 23, 20]),
        node(24, 4, 4), node(25, 12, 4), node(26, 12, 12), node(27, 4, 12),
        way(21, [24, 25, 26, 27, 24]),
        rel(400, [wm(20, "outer"), wm(21, "inner")]),
        # I: independent island way 22
        node(28, 6, 6), node(29, 10, 6), node(30, 10, 10), node(31, 6, 10),
        way(22, [28, 29, 30, 31, 28]),
        rel(800, [wm(22, "outer")]),
        # U: disjoint square way 23
        node(32, 40, 40), node(33, 50, 40), node(34, 50, 50), node(35, 40, 50),
        way(23, [32, 33, 34, 35, 32]),
        rel(600, [wm(23, "outer")]),
        # V: overlapping square way 24
        node(36, 15, 15), node(37, 25, 15), node(38, 25, 25), node(39, 15, 25),
        way(24, [36, 37, 38, 39, 36]),
        rel(700, [wm(24, "outer")]),
        # M: two disjoint outer squares, ways 30 and 31 -> two faces
        node(40, 60, 60), node(41, 64, 60), node(42, 64, 64), node(43, 60, 64),
        way(30, [40, 41, 42, 43, 40]),
        node(44, 70, 70), node(45, 74, 70), node(46, 74, 74), node(47, 70, 74),
        way(31, [44, 45, 46, 47, 44]),
        rel(900, [wm(30, "outer"), wm(31, "outer")]),
    ]

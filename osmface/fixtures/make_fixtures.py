#!/usr/bin/env python3
"""Regenerate the small hand-designed fixtures deterministically.

    python3 fixtures/make_fixtures.py

Every file is a self-contained JSONL dataset exercising one behaviour of
the assembler.  Layout of the base fixture (areas are exact):

    square  (0,0)-(10,0)-(10,10)-(0,10)   area 100, four ways, mixed directions
    H hole  (2,2)-(6,2)-(6,6)-(2,6)       area 16,  closed way 5, outer in C
    I island(3,3)-(5,3)-(5,5)-(3,5)       area 4,   closed way 6, inner in C

    relation 100 "C": outer way5 (H), inner way6 (I)
    relation 200 "R": ways 3,1,4,2 outer + relation 100 as inner

    => in R, H flips to inner (hole), I flips to outer (island)
    => R has 2 faces: 100-16=84 and 4, total 88; C has 1 face: 16-4=12
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


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


# --- shared building blocks -------------------------------------------------

SQUARE_NODES = [node(1, 0, 0), node(2, 10, 0), node(3, 10, 10), node(4, 0, 10)]
HOLE_NODES = [node(5, 2, 2), node(6, 6, 2), node(7, 6, 6), node(8, 2, 6)]
ISLAND_NODES = [node(9, 3, 3), node(10, 5, 3), node(11, 5, 5), node(12, 3, 5)]

# four open ways, deliberately mixed directions ("乱向")
SQUARE_WAYS = [
    way(1, [1, 2]),     # (0,0)  -> (10,0)
    way(2, [3, 2]),     # (10,10)-> (10,0)   reversed
    way(3, [3, 4]),     # (10,10)-> (0,10)
    way(4, [1, 4]),     # (0,0)  -> (0,10)   reversed
]
HOLE_WAY = way(5, [5, 6, 7, 8, 5])        # H, closed, CCW
ISLAND_WAY = way(6, [9, 10, 11, 12, 9])   # I, closed, CCW

REL_C = rel(100, [wm(5, "outer"), wm(6, "inner")])
REL_R = rel(200, [wm(3), wm(1), rm(100, "inner"), wm(4), wm(2)])


def base_records():
    # definition order deliberately scrambled across id domains
    return [
        REL_R,
        way(1, [1, 2]),
        node(1, 0, 0),
        node(2, 10, 0),
        way(2, [3, 2]),
        node(3, 10, 10),
        node(4, 0, 10),
        way(3, [3, 4]),
        way(4, [1, 4]),
        REL_C,
        *HOLE_NODES,
        HOLE_WAY,
        *ISLAND_NODES,
        ISLAND_WAY,
    ]


def shuffled_records():
    """Same logical dataset: member/definition order shuffled, every way
    reversed wholesale.  Must assemble to the same faces as base."""
    return [
        node(12, 3, 5),
        way(6, [9, 12, 11, 10, 9]),          # reversed
        node(9, 3, 3),
        node(10, 5, 3),
        node(11, 5, 5),
        rel(100, [wm(6, "inner"), wm(5, "outer")]),   # member order swapped
        way(5, [5, 8, 7, 6, 5]),             # reversed
        node(5, 2, 2),
        node(6, 6, 2),
        node(7, 6, 6),
        node(8, 2, 6),
        rel(200, [rm(100, "inner"), wm(2), wm(4), wm(1), wm(3)]),
        way(4, [4, 1]),                      # reversed
        way(3, [4, 3]),                      # reversed
        node(4, 0, 10),
        node(3, 10, 10),
        way(2, [2, 3]),                      # reversed
        node(2, 10, 0),
        node(1, 0, 0),
        way(1, [2, 1]),                      # reversed
    ]


def missing_node_records():
    """Node 2 deleted; relation 300 references R to check propagation;
    relation 600 references a missing way and a missing relation."""
    recs = [r for r in base_records()
            if not (r["type"] == "node" and r["id"] == 2)]
    recs.append(rel(300, [rm(200, "outer")]))
    recs.append(rel(600, [wm(999), rm(998, "outer")]))
    return recs


def cycle_records():
    """D<->E cycle, self-loop F, parent Q of the cycle, empty relation G."""
    recs = base_records() + [
        rel(400, [rm(401, "outer")]),
        rel(401, [rm(400, "outer")]),
        rel(402, [rm(400, "outer")]),
        rel(403, [rm(403, "outer")]),
        rel(404, []),
    ]
    return recs


def dup_endpoint_coords_records():
    """Way 2 ends at node 22 which merely shares (10,0) with node 2.

    Equal coordinates, different node identity: the square must NOT close.
    """
    recs = []
    for r in base_records():
        if r["type"] == "way" and r["id"] == 2:
            r = way(2, [3, 22])
        recs.append(r)
    recs.append(node(22, 10, 0))
    return recs


def junction_records():
    """Extra outer fragment dangling off node 2 -> degree 3 (三岔)."""
    recs = base_records()
    recs.append(node(13, 15, 0))
    recs.append(way(7, [2, 13]))
    recs[0] = rel(200, [wm(3), wm(1), rm(100, "inner"), wm(4), wm(2), wm(7)])
    return recs


def shared_way_records():
    """Two relations share boundary way 12; a third uses one way twice.

    A: (0,0)-(5,0)-(5,10)-(0,10)   area 50, ways 11,12,13,14
    B: (5,0)-(10,0)-(10,10)-(5,10) area 50, ways 15,16,17,12
    DUP: way 12 twice in the same role -> degenerate ring conflict
    """
    return [
        node(1, 0, 0), node(2, 10, 0), node(3, 10, 10), node(4, 0, 10),
        node(14, 5, 0), node(15, 5, 10),
        way(11, [1, 14]),
        way(12, [14, 15]),      # shared boundary
        way(13, [15, 4]),
        way(14, [1, 4]),
        way(15, [14, 2]),
        way(16, [3, 2]),
        way(17, [3, 15]),
        rel(500, [wm(11), wm(12), wm(13), wm(14)]),
        rel(501, [wm(15), wm(16), wm(17), wm(12)]),
        rel(502, [wm(12), wm(12)]),
    ]


def touching_records():
    """Island ring shares corner node 7 (6,6) with the hole ring H."""
    recs = []
    for r in base_records():
        if r["type"] == "way" and r["id"] == 6:
            r = way(6, [9, 16, 7, 17, 9])   # touches H at node 7
        recs.append(r)
    recs.extend([node(16, 6, 3), node(17, 3, 6)])
    return recs


def dup_identical_records():
    """Byte-identical duplicate definitions merge silently."""
    return base_records() + [node(1, 0, 0), way(3, [3, 4])]


def dup_conflict_records():
    """Node 1 redefined with different coordinates -> input fault."""
    return base_records() + [node(1, 0, 1)]


def spatial_world_records():
    """Hand-written world for historical spatial face queries.

    All coordinates are exact integers; nothing here is scale-generated.

        R (400): outer square (0,0)-(20,20) way 20 + inner hole
                 (4,4)-(12,12) way 21 -> one face, area 400-64 = 336
        I (800): independent island (6,6)-(10,10) way 22, area 16
        U (600): disjoint square (40,40)-(50,50) way 23, area 100
        V (700): overlapping square (15,15)-(25,25) way 24, area 100
        M (900): two disjoint outer squares (ways 30, 31) -> MultiPolygon

    Point (5,5) is inside R's hole and inside no other relation, so a hole
    query must return an empty set despite the bbox candidate on R.
    """
    return [
        node(20, 0, 0), node(21, 20, 0), node(22, 20, 20), node(23, 0, 20),
        way(20, [20, 21, 22, 23, 20]),
        node(24, 4, 4), node(25, 12, 4), node(26, 12, 12), node(27, 4, 12),
        way(21, [24, 25, 26, 27, 24]),
        rel(400, [wm(20, "outer"), wm(21, "inner")]),
        node(28, 6, 6), node(29, 10, 6), node(30, 10, 10), node(31, 6, 10),
        way(22, [28, 29, 30, 31, 28]),
        rel(800, [wm(22, "outer")]),
        node(32, 40, 40), node(33, 50, 40), node(34, 50, 50), node(35, 40, 50),
        way(23, [32, 33, 34, 35, 32]),
        rel(600, [wm(23, "outer")]),
        node(36, 15, 15), node(37, 25, 15), node(38, 25, 25), node(39, 15, 25),
        way(24, [36, 37, 38, 39, 36]),
        rel(700, [wm(24, "outer")]),
        node(40, 60, 60), node(41, 64, 60), node(42, 64, 64), node(43, 60, 64),
        way(30, [40, 41, 42, 43, 40]),
        node(44, 70, 70), node(45, 74, 70), node(46, 74, 74), node(47, 70, 74),
        way(31, [44, 45, 46, 47, 44]),
        rel(900, [wm(30, "outer"), wm(31, "outer")]),
    ]


def shared_child_records():
    """Sub-relation C is compiled once and used by two parents.

    200 R: square + C(inner)   -> faces 84 + 4
    210 S: bigger square + C(inner) -> faces 884 + 4
    Each parent's occurrences keep their own member-index chains.
    """
    recs = base_records() + [
        node(31, -10, -10), node(32, 20, -10), node(33, 20, 20), node(34, -10, 20),
        way(8, [31, 32, 33, 34, 31]),
        rel(210, [wm(8), rm(100, "inner")]),
    ]
    return recs


def bad_rings_records():
    """Ring/nesting rule violations, one relation each."""
    return [
        # bowtie: self-intersecting closed way
        node(1, 0, 0), node(2, 2, 2), node(3, 2, 0), node(4, 0, 2),
        way(1, [1, 2, 3, 4, 1]),
        rel(610, [wm(1)]),
        # a lone inner ring with no containing outer
        node(5, 0, 0), node(6, 1, 0), node(7, 1, 1), node(8, 0, 1),
        way(2, [5, 6, 7, 8, 5]),
        rel(611, [wm(2, "inner")]),
        # nesting roles must alternate: innermost ring should be outer here
        node(9, 0, 0), node(10, 10, 0), node(11, 10, 10), node(12, 0, 10),
        node(13, 2, 2), node(14, 6, 2), node(15, 6, 6), node(16, 2, 6),
        node(17, 3, 3), node(18, 5, 3), node(19, 5, 5), node(20, 3, 5),
        way(3, [9, 10, 11, 12, 9]),
        way(4, [13, 14, 15, 16, 13]),
        way(5, [17, 18, 19, 20, 17]),
        rel(612, [wm(3), wm(4, "inner"), wm(5, "inner")]),
    ]


FIXTURES = {
    "base.jsonl": base_records,
    "shuffled.jsonl": shuffled_records,
    "missing_node.jsonl": missing_node_records,
    "cycle.jsonl": cycle_records,
    "dup_endpoint_coords.jsonl": dup_endpoint_coords_records,
    "junction.jsonl": junction_records,
    "shared_way.jsonl": shared_way_records,
    "shared_child.jsonl": shared_child_records,
    "touching.jsonl": touching_records,
    "bad_rings.jsonl": bad_rings_records,
    "dup_identical.jsonl": dup_identical_records,
    "dup_conflict.jsonl": dup_conflict_records,
    "spatial_world.jsonl": spatial_world_records,
}


def main():
    for name, build in sorted(FIXTURES.items()):
        path = os.path.join(HERE, name)
        with open(path, "w", encoding="utf-8") as fh:
            for rec in build():
                fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        print(f"wrote {path}")
    # prove the checked-in files are exactly what the generator emits
    print("fixtures are deterministic: re-running this script must be a no-op")


if __name__ == "__main__":
    main()

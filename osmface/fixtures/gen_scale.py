#!/usr/bin/env python3
"""Runtime scale-data generator (large fixtures are never checked in).

    python3 fixtures/gen_scale.py /tmp/scale.jsonl \
        --unused 100000 --ring-nodes 20000 --grid 30

Produces:
  * one relation (900000) whose boundary is a single closed way with
    --ring-nodes nodes on a circle;
  * GRID x GRID small square relations (800000+), each 4 nodes + 1 way;
  * --unused extra nodes that no relation references, emulating the rest
    of the map (assembly must not pull them into memory).
"""
from __future__ import annotations

import argparse
import json
import math

BIG_REL = 900000
BIG_WAY = 900000
BIG_NODE_BASE = 9_000_000
GRID_REL_BASE = 800000
GRID_WAY_BASE = 800000
GRID_NODE_BASE = 8_000_000
UNUSED_NODE_BASE = 5_000_000


def generate(path: str, unused: int, ring_nodes: int, grid: int) -> dict:
    counts = {"node": 0, "way": 0, "relation": 0}

    def write(fh, rec):
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        counts[rec["type"]] += 1

    with open(path, "w", encoding="utf-8") as fh:
        # big ring: points on a circle, angle-sorted => simple ring
        radius = 1_000_000.0
        ids = [BIG_NODE_BASE + i for i in range(ring_nodes)]
        for i, nid in enumerate(ids):
            t = 2.0 * math.pi * i / ring_nodes
            write(fh, {"type": "node", "id": nid,
                       "x": radius * math.cos(t), "y": radius * math.sin(t)})
        write(fh, {"type": "way", "id": BIG_WAY, "nodes": ids + [ids[0]]})
        write(fh, {"type": "relation", "id": BIG_REL,
                   "members": [{"type": "way", "ref": BIG_WAY, "role": "outer"}]})

        # grid of small disjoint squares far away from the circle
        ox, oy, side, gap = 3_000_000.0, 3_000_000.0, 10.0, 5.0
        for r in range(grid):
            for c in range(grid):
                k = r * grid + c
                n0 = GRID_NODE_BASE + 4 * k
                x0, y0 = ox + c * (side + gap), oy + r * (side + gap)
                x1, y1 = x0 + side, y0 + side
                for nid, x, y in ((n0, x0, y0), (n0 + 1, x1, y0),
                                  (n0 + 2, x1, y1), (n0 + 3, x0, y1)):
                    write(fh, {"type": "node", "id": nid, "x": x, "y": y})
                write(fh, {"type": "way", "id": GRID_WAY_BASE + k,
                           "nodes": [n0, n0 + 1, n0 + 2, n0 + 3, n0]})
                write(fh, {"type": "relation", "id": GRID_REL_BASE + k,
                           "members": [{"type": "way", "ref": GRID_WAY_BASE + k,
                                        "role": "outer"}]})

        # unused bulk nodes (the rest of the map)
        for i in range(unused):
            write(fh, {"type": "node", "id": UNUSED_NODE_BASE + i,
                       "x": 10_000_000.0 + i, "y": -10_000_000.0})
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    ap.add_argument("--unused", type=int, default=100_000)
    ap.add_argument("--ring-nodes", type=int, default=20_000)
    ap.add_argument("--grid", type=int, default=30)
    args = ap.parse_args()
    counts = generate(args.path, args.unused, args.ring_nodes, args.grid)
    print(f"wrote {args.path}: {counts}")


if __name__ == "__main__":
    main()

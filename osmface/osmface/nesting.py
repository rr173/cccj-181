"""Ring nesting and face construction.

Rings may nest but their boundaries must never intersect or touch.  The
minimal containing ring is the parent; roles must alternate outer/inner
down the nesting chain and every top-level ring must be outer.  Each
outer ring together with its *direct* inner children forms one face;
deeper outer rings are independent islands with faces of their own, not
holes to be flattened away.
"""
from __future__ import annotations

from .model import MAX_REPORT, OUTER, TopologyConflict, flip_role


def _ring_brief(ring: dict) -> dict:
    return {"role": ring["role"], "nodes": ring["nodes"][:MAX_REPORT]}


def _bbox_overlap(a, b) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def build_faces(rings: list) -> list:
    """Return faces as [{shell: ring_index, holes: [ring_index, ...]}].

    ``rings`` entries need: role, nodes, line (LinearRing), poly (Polygon),
    area, bounds.  Raises TopologyConflict on boundary contact, bad
    nesting roles or invalid faces.
    """
    n = len(rings)
    if n == 0:
        return []

    # pairwise boundary disjointness + containment, bbox-sweep prefilter
    containers = [set() for _ in range(n)]
    order = sorted(range(n), key=lambda i: (rings[i]["bounds"][0], i))
    active = []
    for i in order:
        minx_i = rings[i]["bounds"][0]
        active = [j for j in active if rings[j]["bounds"][2] >= minx_i]
        for j in active:
            if not _bbox_overlap(rings[i]["bounds"], rings[j]["bounds"]):
                continue
            if rings[i]["line"].intersects(rings[j]["line"]):
                raise TopologyConflict(
                    "boundary_intersect",
                    {
                        "detail": "ring boundaries must not intersect or touch",
                        "rings": [_ring_brief(rings[i]), _ring_brief(rings[j])],
                    },
                )
            big, small = (i, j) if rings[i]["area"] > rings[j]["area"] else (j, i)
            if rings[big]["poly"].contains(rings[small]["poly"]):
                containers[small].add(big)
        active.append(i)

    # minimal containing ring = parent
    parent = [None] * n
    for j in range(n):
        if containers[j]:
            parent[j] = min(containers[j], key=lambda i: (rings[i]["area"], i))

    # role structure: top level outer, strict outer/inner alternation
    for i in range(n):
        if parent[i] is None:
            if rings[i]["role"] != OUTER:
                raise TopologyConflict(
                    "toplevel_not_outer",
                    {"detail": "a ring without containing parent must be outer",
                     "ring": _ring_brief(rings[i])},
                )
        else:
            want = flip_role(rings[parent[i]]["role"])
            if rings[i]["role"] != want:
                raise TopologyConflict(
                    "role_alternation",
                    {
                        "detail": "nested roles must alternate outer/inner",
                        "ring": _ring_brief(rings[i]),
                        "expected_role": want,
                        "parent": _ring_brief(rings[parent[i]]),
                    },
                )

    children = [[] for _ in range(n)]
    for j in range(n):
        if parent[j] is not None:
            children[parent[j]].append(j)

    faces = []
    for i in range(n):
        if rings[i]["role"] == OUTER:
            faces.append({"shell": i, "holes": sorted(children[i])})
    faces.sort(key=lambda f: (rings[f["shell"]]["nodes"][0], f["shell"]))
    return faces

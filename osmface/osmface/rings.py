"""Ring assembly from way fragments.

Joining is by endpoint *node identity* only — never by coordinates.  Two
nodes with identical coordinates but different ids are different points.
Open fragments are joined only when every endpoint has degree exactly 2,
which makes the undirected cycle decomposition unique; anything else is a
topology_conflict, never an arbitrary branch choice.

Shapely is used for geometric validity (simplicity, non-zero area); all
member expansion and ring assembly is done here, not by an OSM-specific
polygon assembler.
"""
from __future__ import annotations

from shapely.geometry import LinearRing, Polygon

from .model import MAX_REPORT, OUTER, TopologyConflict


def join_open_fragments(frag_nodes: list) -> list:
    """Join open fragments (first node id != last node id) into rings.

    Returns a list of (ring_nodes_closed, [(frag_index, reversed), ...]).
    Raises TopologyConflict('endpoint_degree') when the endpoint-degree
    condition fails (three-way junctions, dangling ends, unclosable
    chains), or ('unclosed_chain') as a defensive fallback.
    """
    adj = {}
    for idx, nodes in enumerate(frag_nodes):
        adj.setdefault(nodes[0], []).append((idx, 0))
        adj.setdefault(nodes[-1], []).append((idx, 1))
    bad = sorted((n, len(entries)) for n, entries in adj.items() if len(entries) != 2)
    if bad:
        raise TopologyConflict(
            "endpoint_degree",
            {
                "detail": "open fragment endpoints must all have degree exactly 2",
                "endpoints": [{"node": n, "degree": d} for n, d in bad[:MAX_REPORT]],
                "total": len(bad),
            },
        )
    visited = [False] * len(frag_nodes)
    rings = []
    for start_idx in range(len(frag_nodes)):
        if visited[start_idx]:
            continue
        ring_nodes = []
        ring_frags = []
        cur, rev = start_idx, False
        start_node = frag_nodes[cur][0]
        while True:
            visited[cur] = True
            seq = frag_nodes[cur][::-1] if rev else frag_nodes[cur]
            ring_nodes.extend(seq if not ring_nodes else seq[1:])
            ring_frags.append((cur, rev))
            end_node = ring_nodes[-1]
            if end_node == start_node:
                break
            (f1, _e1), (f2, _e2) = adj[end_node]
            nxt, nend = (f1, _e1) if f2 == cur else (f2, _e2)
            if visited[nxt]:  # unreachable under degree == 2; never guess
                raise TopologyConflict(
                    "unclosed_chain",
                    {"detail": "fragment chain cannot be closed uniquely",
                     "at_node": end_node},
                )
            cur, rev = nxt, nend == 1
        rings.append((ring_nodes, ring_frags))
    return rings


def validate_ring(ring_nodes: list, coords_of: dict):
    """Geometric validity for one closed candidate ring.

    Requires >= 3 distinct nodes (>= 4 closed positions), a simple
    (non-self-intersecting) LinearRing and non-zero area.  Returns
    (LinearRing, Polygon).
    """
    if len(ring_nodes) < 4:
        raise TopologyConflict(
            "degenerate_ring",
            {"detail": "ring has fewer than 3 distinct nodes",
             "nodes": ring_nodes[:MAX_REPORT]},
        )
    coords = [coords_of[n] for n in ring_nodes]
    try:
        line = LinearRing(coords)
    except Exception as exc:  # shapely/GEOS construction failure
        raise TopologyConflict(
            "invalid_ring",
            {"detail": str(exc), "nodes": ring_nodes[:MAX_REPORT]},
        ) from exc
    if not line.is_simple:
        raise TopologyConflict(
            "ring_self_intersection",
            {"detail": "ring self-intersects or touches itself",
             "nodes": ring_nodes[:MAX_REPORT]},
        )
    poly = Polygon(line)
    if not poly.is_valid:
        raise TopologyConflict(
            "invalid_ring",
            {"detail": "ring does not form a valid polygon",
             "nodes": ring_nodes[:MAX_REPORT]},
        )
    if not poly.area > 0.0:
        raise TopologyConflict(
            "zero_area_ring",
            {"detail": "ring encloses no area", "nodes": ring_nodes[:MAX_REPORT]},
        )
    return line, poly


def _signed_area(coords) -> float:
    area = 0.0
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        area += x1 * y2 - x2 * y1
    return area / 2.0


def canonicalize_ring(ring_nodes, frag_occ_rev, role, ways_nodes, coords_of):
    """Normalize orientation and start node; rebuild segment provenance.

    Orientation: outer rings CCW, inner rings CW (coordinates and node
    identity are untouched — only direction/start point are normalized).
    Start: smallest node id.  Returns (closed_node_ids, segments) where
    each segment is {way, path, reversed, nodes} aligned to the output
    ring.  A fragment split by the rotation point appears as two segments
    of the same way occurrence; nothing is deduplicated or dropped.
    """
    want_ccw = role == OUTER
    is_ccw = _signed_area([coords_of[n] for n in ring_nodes]) > 0
    if is_ccw != want_ccw:
        core = ring_nodes[:-1]
        core = [core[0]] + core[1:][::-1]
        ring_nodes = core + [core[0]]
        frag_occ_rev = [(occ, not rev) for occ, rev in reversed(frag_occ_rev)]
    core = ring_nodes[:-1]
    k = min(range(len(core)), key=lambda i: core[i])
    core = core[k:] + core[:k]
    ring_nodes = core + [core[0]]

    # map every directed edge of the ring to its fragment occurrence
    edge_owner = {}
    for occ, rev in frag_occ_rev:
        seq = ways_nodes[occ.way]
        seq = seq[::-1] if rev else seq
        for a, b in zip(seq, seq[1:]):
            edge_owner[(a, b)] = (occ, rev)

    segments = []
    current = None
    m = len(core)
    for i in range(m):
        edge = (core[i], core[(i + 1) % m])
        owner = edge_owner[edge]
        if current is not None and current[0] == owner:
            current[1].append(edge[1])
        else:
            if current is not None:
                segments.append(current)
            current = [owner, [edge[0], edge[1]]]
    segments.append(current)
    out = [
        {
            "way": occ.way,
            "path": [list(p) for p in occ.path],
            "reversed": bool(rev),
            "nodes": nodes,
        }
        for (occ, rev), nodes in segments
    ]
    return ring_nodes, out

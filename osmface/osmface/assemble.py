"""Assembly orchestrator: expand relations, build rings, nest, emit faces.

Per relation the pipeline is:

1. gap analysis — missing way/relation references and missing nodes are
   listed and propagated to every parent relation; unaffected relations
   still assemble;
2. expansion — member ways plus recursively expanded sub-relations
   (compiled once per child, re-specialized per use site so parent roles
   and occurrence provenance survive); the boundary-size limit is
   enforced here, before geometry is fetched;
3. ring assembly — per effective role, endpoint-identity joining;
4. nesting — containment forest, role alternation, faces;
5. persistence — results replace the previous ones in a single
   transaction; the output package is a separate, explicit step.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from shapely.geometry import MultiPolygon, Polygon, mapping

from . import db as dbmod
from .graph import build_graph, find_cycles, postorder
from .model import (
    COMPLETE,
    INCOMPLETE,
    MAX_REPORT,
    OUTER,
    INNER,
    STATUSES,
    STAGING_LOADED,
    TOPOLOGY_CONFLICT,
    StagingStateError,
    TopologyConflict,
    flip_role,
)
from .nesting import build_faces
from .rings import canonicalize_ring, join_open_fragments, validate_ring

DEFAULT_MAX_BOUNDARY_WAYS = 100_000
DEFAULT_MAX_BOUNDARY_NODES = 1_000_000


@dataclass(frozen=True)
class Occ:
    """One way occurrence: way id + full member-index chain + final role."""

    way: int
    path: tuple  # ((relation_id, member_index), ...) from the root relation
    role: str


@dataclass
class Outcome:
    status: str
    reason: dict | None = None
    geometry: dict | None = None      # GeoJSON Polygon/MultiPolygon mapping
    faces: list | None = None         # [{geojson, rings:[{role,hole,nodes,segments}]}]
    expansion: list | None = None     # cached [Occ] for parent relations
    n_occ: int = 0
    n_nodes: int = 0


# ---------------------------------------------------------------------------
# per-relation resolution
# ---------------------------------------------------------------------------

def _resolve(conn, rid, relations, results, max_ways, max_nodes, stats) -> Outcome:
    members = relations[rid]
    if not members:
        return Outcome(status=INCOMPLETE, reason={"code": "empty_relation"})

    # -- gap analysis: direct references + propagated child gaps -----------
    gaps = []
    bad_children = []
    for idx, m in enumerate(members):
        if m["type"] != "relation":
            continue
        ref = m["ref"]
        if ref not in relations:
            gaps.append({"kind": "relation", "id": ref, "path": [[rid, idx]]})
            continue
        child = results[ref]
        if child.status != COMPLETE:
            bad_children.append(
                {
                    "relation": ref,
                    "status": child.status,
                    "code": (child.reason or {}).get("code"),
                }
            )
            for g in (child.reason or {}).get("gaps", []):
                gaps.append({**g, "path": [[rid, idx]] + g["path"]})

    way_refs = [m["ref"] for m in members if m["type"] == "way"]
    existing_ways = dbmod.way_ids_exist(conn, way_refs)
    for idx, m in enumerate(members):
        if m["type"] == "way" and m["ref"] not in existing_ways:
            gaps.append({"kind": "way", "id": m["ref"], "path": [[rid, idx]]})

    if gaps or bad_children:
        reason = {"code": "missing_references" if gaps else "subrelation_unresolved"}
        if gaps:
            reason["gaps"] = gaps[:MAX_REPORT]
            reason["total_gaps"] = len(gaps)
        if bad_children:
            reason["children"] = bad_children
        return Outcome(status=INCOMPLETE, reason=reason)

    # -- expansion with boundary-size limit --------------------------------
    occs: list[Occ] = []
    n_nodes = 0
    lengths = dbmod.fetch_way_lengths(conn, way_refs)
    for idx, m in enumerate(members):
        if m["type"] == "way":
            occs.append(Occ(m["ref"], ((rid, idx),), m["role"]))
            n_nodes += lengths[m["ref"]]
        else:
            child = results[m["ref"]]  # complete by now
            for o in child.expansion:
                role = o.role if m["role"] == OUTER else flip_role(o.role)
                occs.append(Occ(o.way, ((rid, idx),) + o.path, role))
            n_nodes += child.n_nodes
        if len(occs) > max_ways or n_nodes > max_nodes:
            return Outcome(
                status=INCOMPLETE,
                reason={
                    "code": "boundary_limit_exceeded",
                    "limits": {"ways": max_ways, "nodes": max_nodes},
                    "observed_at_least": {"ways": len(occs), "nodes": n_nodes},
                },
            )

    # -- fetch exactly this boundary's ways/nodes from the on-disk index ---
    ways_nodes = dbmod.fetch_ways(conn, {o.way for o in occs}, stats)
    needed = set()
    for o in occs:
        needed.update(ways_nodes[o.way])
    coords = dbmod.fetch_nodes(conn, needed, stats)
    missing = set(needed) - set(coords)
    if missing:
        seen = set()
        node_gaps = []
        for o in occs:
            for n in ways_nodes[o.way]:
                if n in missing:
                    key = (n, o.way, o.path)
                    if key not in seen:
                        seen.add(key)
                        node_gaps.append(
                            {
                                "kind": "node",
                                "id": n,
                                "way": o.way,
                                "path": [list(p) for p in o.path],
                            }
                        )
        return Outcome(
            status=INCOMPLETE,
            reason={
                "code": "missing_references",
                "gaps": node_gaps[:MAX_REPORT],
                "total_gaps": len(node_gaps),
            },
        )

    # -- ring assembly + nesting -------------------------------------------
    try:
        rings = _build_all_rings(occs, ways_nodes, coords)
        faces_idx = build_faces(rings)
        if not faces_idx:
            raise TopologyConflict(
                "empty_boundary",
                {"detail": "expansion produced no rings"},
            )
        faces = _materialize_faces(rings, faces_idx, coords)
    except TopologyConflict as exc:
        return Outcome(
            status=TOPOLOGY_CONFLICT,
            reason={"code": exc.code, **exc.detail},
        )

    polygons = [f["polygon"] for f in faces]
    geometry = mapping(polygons[0] if len(polygons) == 1 else MultiPolygon(polygons))
    out_faces = [
        {
            "geojson": mapping(f["polygon"]),
            "rings": f["ring_records"],
        }
        for f in faces
    ]
    return Outcome(
        status=COMPLETE,
        geometry=geometry,
        faces=out_faces,
        expansion=occs,
        n_occ=len(occs),
        n_nodes=n_nodes,
    )


def _build_all_rings(occs, ways_nodes, coords) -> list:
    """All rings of one relation, grouped by effective role."""
    rings = []
    for role in (OUTER, INNER):
        group = [o for o in occs if o.role == role]
        open_occs = []
        frag_nodes = []
        for o in group:
            wn = ways_nodes[o.way]
            if wn[0] == wn[-1]:
                # an already-closed way is a candidate ring as-is; its
                # internal nodes are never altered
                rings.append(_make_ring(wn, [(o, False)], role, ways_nodes, coords))
            else:
                open_occs.append(o)
                frag_nodes.append(wn)
        for ring_nodes, frags in join_open_fragments(frag_nodes):
            occ_rev = [(open_occs[i], rev) for i, rev in frags]
            rings.append(_make_ring(ring_nodes, occ_rev, role, ways_nodes, coords))
    return rings


def _make_ring(ring_nodes, occ_rev, role, ways_nodes, coords) -> dict:
    line, poly = validate_ring(ring_nodes, coords)
    nodes, segments = canonicalize_ring(ring_nodes, occ_rev, role, ways_nodes, coords)
    return {
        "role": role,
        "nodes": nodes,
        "segments": segments,
        "line": line,
        "poly": poly,
        "area": poly.area,
        "bounds": line.bounds,
    }


def _materialize_faces(rings, faces_idx, coords) -> list:
    faces = []
    for f in faces_idx:
        shell = rings[f["shell"]]
        holes = [rings[h] for h in f["holes"]]
        shell_coords = [coords[n] for n in shell["nodes"]]
        hole_coords = [[coords[n] for n in h["nodes"]] for h in holes]
        poly = Polygon(shell_coords, hole_coords)
        if not poly.is_valid:
            raise TopologyConflict(
                "invalid_face",
                {"detail": "shell/hole combination is not a valid polygon",
                 "shell": shell["nodes"][:MAX_REPORT]},
            )
        ring_records = [
            {"role": shell["role"], "hole": False, "nodes": shell["nodes"],
             "segments": shell["segments"]}
        ]
        ring_records += [
            {"role": h["role"], "hole": True, "nodes": h["nodes"],
             "segments": h["segments"]}
            for h in holes
        ]
        faces.append({"polygon": poly, "ring_records": ring_records})
    return faces


# ---------------------------------------------------------------------------
# whole-database assemble
# ---------------------------------------------------------------------------

def assemble(conn, max_boundary_ways=DEFAULT_MAX_BOUNDARY_WAYS,
             max_boundary_nodes=DEFAULT_MAX_BOUNDARY_NODES):
    """Resolve every relation; rewrite result tables in one transaction."""
    staging = dbmod.get_meta(conn, "staging_state")
    if staging != STAGING_LOADED:
        raise StagingStateError(
            f"staging state is {staging!r}, expected 'loaded' — "
            "refusing to assemble from unusable input"
        )
    relations = dbmod.fetch_relations(conn)
    graph = build_graph(relations)
    cyclic = find_cycles(graph)

    results: dict[int, Outcome] = {}
    for rid, chain in cyclic.items():
        results[rid] = Outcome(
            status=INCOMPLETE,
            reason={"code": "relation_cycle", "chain": chain},
        )

    stats = dbmod.FetchStats()
    for rid in postorder(relations, set(cyclic)):
        results[rid] = _resolve(
            conn, rid, relations, results,
            max_boundary_ways, max_boundary_nodes, stats,
        )

    _write_results(conn, results)
    counts = {s: 0 for s in STATUSES}
    for out in results.values():
        counts[out.status] += 1
    summary = {
        "relations": len(results),
        "status_counts": counts,
        "limits": {"ways": max_boundary_ways, "nodes": max_boundary_nodes},
        "fetched": stats.as_dict(),
        "assembled_at": dbmod.utcnow(),
    }
    dbmod.set_meta(conn, "last_assemble", summary)
    conn.commit()
    return summary, results


def _write_results(conn, results: dict) -> None:
    for table in ("relation_status", "faces", "rings", "ring_segments"):
        conn.execute(f"DELETE FROM {table}")
    face_id = 0
    ring_id = 0
    segment_id = 0
    for rid in sorted(results):
        out = results[rid]
        conn.execute(
            "INSERT INTO relation_status(relation_id, status, reason) VALUES(?,?,?)",
            (rid, out.status, json.dumps(out.reason) if out.reason else None),
        )
        if out.status != COMPLETE:
            continue
        for face_index, face in enumerate(out.faces):
            conn.execute(
                "INSERT INTO faces(id, relation_id, face_index, geojson) VALUES(?,?,?,?)",
                (face_id, rid, face_index, json.dumps(face["geojson"])),
            )
            for ring_index, ring in enumerate(face["rings"]):
                conn.execute(
                    "INSERT INTO rings(id, face_id, ring_index, role, is_hole, node_ids)"
                    " VALUES(?,?,?,?,?,?)",
                    (
                        ring_id,
                        face_id,
                        ring_index,
                        ring["role"],
                        1 if ring["hole"] else 0,
                        json.dumps(ring["nodes"]),
                    ),
                )
                for seq, seg in enumerate(ring["segments"]):
                    conn.execute(
                        "INSERT INTO ring_segments(id, ring_id, seq, way_id, path,"
                        " reversed, node_ids) VALUES(?,?,?,?,?,?,?)",
                        (
                            segment_id,
                            ring_id,
                            seq,
                            seg["way"],
                            json.dumps(seg["path"]),
                            1 if seg["reversed"] else 0,
                            json.dumps(seg["nodes"]),
                        ),
                    )
                    segment_id += 1
                ring_id += 1
            face_id += 1

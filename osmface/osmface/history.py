"""Revisioned change intake and historical querying.

This module turns a one-shot staging database into a durable, continuously
updated map warehouse.  It adds *no* geometry code of its own: the existing
``assemble._resolve`` per-relation pipeline (gap analysis, expansion, ring
joining, nesting, provenance) runs unchanged against a :class:`RevisionView`,
which serves entities and references exactly as they were at one revision.

Storage design (all on disk, SQLite):

* ``entity_versions``      — one head row per (type, id): current version,
                             tombstone flag, payload;
* ``entity_history``       — append-only (type, id, version) rows, one row
                             per object version with the revision that made
                             it current (tombstones keep their version);
* ``ref_edge_history``     — versioned member/reference edges.  A way or
                             relation writes its full outgoing edge set only
                             on revisions where it changed; edges to missing
                             objects are stored like any other edge;
* ``rel_result_history``   — one row per (revision, relation) that changed,
                             including ``absent`` markers for deletions, with
                             cached expansion, geometry hash and full source
                             descriptor so untouched parents are reused;
* ``face/ring/segment``    — full faces, rings and per-segment object
  ``_history``               versions for every recomputed relation;
* ``batch_registry``       — batch_id -> input hash + receipt, powering
                             idempotent resends and batch-id conflicts;
* ``apply_lock``           — writer holder, guarded by ``BEGIN IMMEDIATE``.

Apply never scans raw node/way/relation payloads to find dependencies:
reverse reachability uses only the versioned edge index, and geometry
fetches only load the affected closure's forward dependencies.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import deque

from . import db as dbmod
from .assemble import (
    Occ,
    Outcome,
    _resolve,
)
from .graph import build_graph, find_cycles, postorder
from .model import (
    ABSENT,
    COMPLETE,
    INCOMPLETE,
    STATUSES,
    STAGING_LOADED,
    BatchError,
    BatchIdConflict,
    HistoryError,
    NotHistoryError,
    RevisionConflict,
    SyncPoint,
)

HISTORY_TABLES = (
    "rev_meta",
    "entity_versions",
    "entity_history",
    "ref_edge_history",
    "rel_result_history",
    "face_history",
    "ring_history",
    "ring_segment_history",
    "batch_registry",
    "apply_lock",
)

HISTORY_SCHEMA = """
CREATE TABLE rev_meta(
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    head_revision      INTEGER NOT NULL,
    max_boundary_ways  INTEGER NOT NULL,
    max_boundary_nodes INTEGER NOT NULL
);
CREATE TABLE entity_versions(
    type    TEXT NOT NULL,
    id      INTEGER NOT NULL,
    version INTEGER NOT NULL,
    rev     INTEGER NOT NULL,
    deleted INTEGER NOT NULL,
    payload TEXT,
    PRIMARY KEY (type, id)
);
CREATE TABLE entity_history(
    type    TEXT NOT NULL,
    id      INTEGER NOT NULL,
    version INTEGER NOT NULL,
    rev     INTEGER NOT NULL,
    deleted INTEGER NOT NULL,
    payload TEXT,
    PRIMARY KEY (type, id, version)
);
CREATE TABLE ref_edge_history(
    revision     INTEGER NOT NULL,
    src_type     TEXT NOT NULL,
    src_id       INTEGER NOT NULL,
    member_index INTEGER NOT NULL,
    ref_type     TEXT NOT NULL,
    ref_id       INTEGER NOT NULL,
    role         TEXT
);
CREATE INDEX idx_ref_edge_reverse
    ON ref_edge_history(ref_type, ref_id, revision);
CREATE INDEX idx_ref_edge_src
    ON ref_edge_history(src_type, src_id, revision);
CREATE TABLE rel_result_history(
    revision    INTEGER NOT NULL,
    relation_id INTEGER NOT NULL,
    status      TEXT NOT NULL,
    reason      TEXT,
    geometry    TEXT,
    source_desc TEXT,
    expansion   TEXT,
    n_occ       INTEGER NOT NULL,
    n_nodes     INTEGER NOT NULL,
    PRIMARY KEY (revision, relation_id)
);
CREATE INDEX idx_rel_result_rid ON rel_result_history(relation_id, revision);
CREATE TABLE face_history(
    revision    INTEGER NOT NULL,
    relation_id INTEGER NOT NULL,
    face_index  INTEGER NOT NULL,
    geojson     TEXT NOT NULL,
    PRIMARY KEY (revision, relation_id, face_index)
);
CREATE TABLE ring_history(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    revision    INTEGER NOT NULL,
    relation_id INTEGER NOT NULL,
    face_index  INTEGER NOT NULL,
    ring_index  INTEGER NOT NULL,
    role        TEXT NOT NULL,
    is_hole     INTEGER NOT NULL,
    node_ids    TEXT NOT NULL
);
CREATE INDEX idx_ring_history ON ring_history(revision, relation_id);
CREATE TABLE ring_segment_history(
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    revision     INTEGER NOT NULL,
    relation_id  INTEGER NOT NULL,
    face_index   INTEGER NOT NULL,
    ring_index   INTEGER NOT NULL,
    seq          INTEGER NOT NULL,
    way_id       INTEGER NOT NULL,
    path         TEXT NOT NULL,
    reversed     INTEGER NOT NULL,
    node_ids     TEXT NOT NULL,
    way_version  INTEGER NOT NULL,
    node_versions TEXT NOT NULL
);
CREATE INDEX idx_segment_history ON ring_segment_history(revision, relation_id);
CREATE TABLE batch_registry(
    batch_id      TEXT PRIMARY KEY,
    sha256        TEXT NOT NULL,
    base_revision INTEGER NOT NULL,
    revision      INTEGER NOT NULL,
    committed_at  TEXT NOT NULL,
    receipt       TEXT NOT NULL
);
CREATE TABLE apply_lock(
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    holder      TEXT NOT NULL,
    acquired_at TEXT NOT NULL
);
"""

ENTITY_TYPES = ("node", "way", "relation")


# ---------------------------------------------------------------------------
# connection helpers
# ---------------------------------------------------------------------------

def connect(path: str) -> sqlite3.Connection:
    conn = dbmod.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(
        "PRAGMA busy_timeout=%s"
        % int(os.environ.get("OSMFACE_BUSY_TIMEOUT", "10000"))
    )
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def close(conn) -> None:
    """Close after checkpointing WAL so the DB file alone is self-contained."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass
    conn.close()


def is_history_enabled(conn) -> bool:
    return bool(dbmod.get_meta(conn, "history_enabled", False))


def _require_history(conn) -> None:
    if not is_history_enabled(conn):
        raise NotHistoryError(
            "database is not history-enabled; run enable_history() / "
            "`osmface enable-history` first"
        )


def head_revision(conn) -> int:
    _require_history(conn)
    row = conn.execute("SELECT head_revision FROM rev_meta WHERE id = 1").fetchone()
    return row["head_revision"]


def _budget(conn):
    row = conn.execute(
        "SELECT max_boundary_ways, max_boundary_nodes FROM rev_meta WHERE id = 1"
    ).fetchone()
    return row["max_boundary_ways"], row["max_boundary_nodes"]


# ---------------------------------------------------------------------------
# enable history on an existing loaded + assembled database
# ---------------------------------------------------------------------------

def enable_history(db_path: str) -> dict:
    """Enable revisioning in place; the existing snapshot becomes revision 0.

    Safe to retry: an already-enabled database returns its revision-0
    identity without touching raw records, results or sources.
    """
    conn = connect(db_path)
    try:
        if is_history_enabled(conn):
            return {
                "history_enabled": True,
                "revision": head_revision(conn),
                "idempotent": True,
            }
        staging = dbmod.get_meta(conn, "staging_state")
        if staging != STAGING_LOADED:
            raise HistoryError(
                f"refusing to enable history: staging_state={staging!r}, "
                f"need {STAGING_LOADED!r} (corrupted/input-failed databases "
                "cannot be enabled)"
            )
        last = dbmod.get_meta(conn, "last_assemble")
        if last is None:
            raise HistoryError(
                "refusing to enable history: database has no assemble "
                "result; run assemble first"
            )
        max_ways = last["limits"]["ways"]
        max_nodes = last["limits"]["nodes"]

        # one-time full assemble is permitted here: it also reconstructs the
        # cached expansions and lets us verify the stored results.  It writes
        # nothing to the legacy tables.
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
                conn, rid, relations, results, max_ways, max_nodes, stats
            )
        _verify_legacy_results(conn, results)

        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executescript(HISTORY_SCHEMA)
            conn.execute(
                "INSERT INTO rev_meta(id, head_revision, max_boundary_ways,"
                " max_boundary_nodes) VALUES(1, 0, ?, ?)",
                (max_ways, max_nodes),
            )
            _seed_entities_rev0(conn)
            _seed_edges_rev0(conn)
            rev0_view = RevisionView(conn, 0)
            for rid in sorted(results):
                outcome = results[rid]
                _annotate_segment_versions(rev0_view, outcome)
                _persist_result(
                    conn, 0, rid, outcome,
                    _source_descriptor(outcome, rev0_view, 0),
                )
            dbmod.set_meta(conn, "history_enabled", True)
            dbmod.set_meta(conn, "history_head", 0)
            dbmod.set_meta(
                conn,
                "history_budget",
                {"ways": max_ways, "nodes": max_nodes},
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {
            "history_enabled": True,
            "revision": 0,
            "relations": len(results),
            "limits": {"ways": max_ways, "nodes": max_nodes},
            "idempotent": False,
        }
    finally:
        close(conn)


def _verify_legacy_results(conn, results: dict) -> None:
    stored = {
        r["relation_id"]: r["status"]
        for r in conn.execute("SELECT relation_id, status FROM relation_status")
    }
    if stored != {rid: out.status for rid, out in results.items()}:
        raise HistoryError(
            "stored assemble results do not match the reproducible assembly; "
            "refusing to enable history on an inconsistent database"
        )


def _seed_entities_rev0(conn) -> None:
    rows = []
    for r in conn.execute("SELECT id, x, y FROM nodes"):
        payload = json.dumps([r["x"], r["y"]], separators=(",", ":"))
        rows.append(("node", r["id"], 1, 0, 0, payload))
    for r in conn.execute("SELECT id, nodes FROM ways"):
        rows.append(("way", r["id"], 1, 0, 0, r["nodes"]))
    for r in conn.execute("SELECT id, members FROM relations"):
        rows.append(("relation", r["id"], 1, 0, 0, r["members"]))
    conn.executemany(
        "INSERT INTO entity_versions(type,id,version,rev,deleted,payload)"
        " VALUES(?,?,?,?,?,?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO entity_history(type,id,version,rev,deleted,payload)"
        " VALUES(?,?,?,?,?,?)",
        rows,
    )


def _edges_of(kind: str, rid: int, payload: str):
    """Yield (src_type, src_id, member_index, ref_type, ref_id, role)."""
    if kind == "way":
        for idx, node_id in enumerate(json.loads(payload)):
            yield ("way", rid, idx, "node", node_id, None)
    else:
        for idx, m in enumerate(json.loads(payload)):
            yield ("relation", rid, idx, m["type"], m["ref"], m["role"])


def _seed_edges_rev0(conn) -> None:
    rows = []
    for r in conn.execute("SELECT id, nodes FROM ways"):
        rows.extend(_edges_of("way", r["id"], r["nodes"]))
    for r in conn.execute("SELECT id, members FROM relations"):
        rows.extend(_edges_of("relation", r["id"], r["members"]))
    conn.executemany(
        "INSERT INTO ref_edge_history(revision, src_type, src_id, member_index,"
        " ref_type, ref_id, role) VALUES(0,?,?,?,?,?,?)",
        rows,
    )


# ---------------------------------------------------------------------------
# revision view: entity/reference store as of one revision
# ---------------------------------------------------------------------------

class RevisionView:
    """Serves the assemble seam exactly the entities visible at a revision."""

    def __init__(self, conn, revision: int):
        self.conn = conn
        self.revision = revision
        self.stats = dbmod.FetchStats()

    # -- effective entity rows (latest object version at this revision) ----
    def _effective_rows(self, kind: str, ids):
        out = {}
        ids = list(ids)
        for i in range(0, len(ids), dbmod._CHUNK):
            chunk = ids[i : i + dbmod._CHUNK]
            if not chunk:
                continue
            marks = ",".join("?" * len(chunk))
            q = (
                "SELECT h.id, h.version, h.deleted, h.payload FROM entity_history h"
                " WHERE h.type = ? AND h.rev <= ? AND h.id IN (%s)"
                " AND h.version = (SELECT MAX(h2.version) FROM entity_history h2"
                "   WHERE h2.type = h.type AND h2.id = h.id AND h2.rev <= ?)"
                % marks
            )
            params = [kind, self.revision, *chunk, self.revision]
            for r in self.conn.execute(q, params):
                if not r["deleted"]:
                    out[r["id"]] = (r["version"], r["payload"])
        return out

    def fetch_nodes(self, ids, stats=None) -> dict:
        rows = self._effective_rows("node", ids)
        if stats is not None:
            stats.node_rows += len(rows)
        return {nid: tuple(json.loads(v[1])) for nid, v in rows.items()}

    def fetch_ways(self, ids, stats=None) -> dict:
        rows = self._effective_rows("way", ids)
        if stats is not None:
            stats.way_rows += len(rows)
        return {wid: json.loads(v[1]) for wid, v in rows.items()}

    def fetch_way_lengths(self, ids) -> dict:
        return {wid: len(nodes) for wid, nodes in self.fetch_ways(ids).items()}

    def way_ids_exist(self, ids) -> set:
        return set(self._effective_rows("way", ids))

    def version_of(self, kind: str, entity_id: int):
        rows = self._effective_rows(kind, [entity_id])
        if entity_id not in rows:
            return None
        return rows[entity_id][0]

    def relation(self, rid: int):
        rows = self._effective_rows("relation", [rid])
        if rid not in rows:
            return None
        return json.loads(rows[rid][1])

    def relations_map(self, ids=None) -> dict:
        if ids is None:
            q = (
                "SELECT h.id, h.payload FROM entity_history h"
                " WHERE h.type = 'relation' AND h.rev <= ?"
                " AND h.version = (SELECT MAX(h2.version) FROM entity_history h2"
                "   WHERE h2.type = h.type AND h2.id = h.id AND h2.rev <= ?)"
                " AND h.deleted = 0"
            )
            return {r["id"]: json.loads(r["payload"])
                    for r in self.conn.execute(q, (self.revision, self.revision))}
        rows = self._effective_rows("relation", ids)
        return {rid: json.loads(v[1]) for rid, v in rows.items()}

    # -- versioned reverse reference index ---------------------------------
    def referrers(self, ref_type: str, ref_id: int):
        """Live (src_type, src_id, member_index, role) edges pointing at
        (ref_type, ref_id) at this revision, using the latest edge snapshot
        of each source.  Edges whose source is dead at this revision are
        dropped; edges pointing at a missing target are returned normally."""
        q = (
            "SELECT e.src_type, e.src_id, e.member_index, e.role"
            " FROM ref_edge_history e"
            " WHERE e.ref_type = ? AND e.ref_id = ? AND e.revision <= ?"
            " AND e.revision = (SELECT MAX(h.revision) FROM ref_edge_history h"
            "   WHERE h.src_type = e.src_type AND h.src_id = e.src_id"
            "     AND h.revision <= ?)"
        )
        edges = [
            (r["src_type"], r["src_id"], r["member_index"], r["role"])
            for r in self.conn.execute(q, (ref_type, ref_id,
                                           self.revision, self.revision))
        ]
        # filter sources that no longer exist at this revision
        live = {}
        for src_type in ENTITY_TYPES:
            src_ids = {s[1] for s in edges if s[0] == src_type}
            if src_ids:
                live[src_type] = set(self._effective_rows(src_type, src_ids))
        return [e for e in edges if e[1] in live.get(e[0], ())]

    def child_relations(self, rid: int):
        """Relation->relation outgoing edges effective at this revision."""
        q = (
            "SELECT e.member_index, e.ref_id, e.role FROM ref_edge_history e"
            " WHERE e.src_type = 'relation' AND e.src_id = ? AND e.revision <= ?"
            " AND e.revision = (SELECT MAX(h.revision) FROM ref_edge_history h"
            "   WHERE h.src_type = e.src_type AND h.src_id = e.src_id"
            "     AND h.revision <= ?) AND e.ref_type = 'relation'"
        )
        return [
            (r["member_index"], r["ref_id"], r["role"])
            for r in self.conn.execute(q, (rid, self.revision, self.revision))
        ]


# ---------------------------------------------------------------------------
# change file parsing
# ---------------------------------------------------------------------------

def _strict_int(v, what: str):
    if not isinstance(v, int) or isinstance(v, bool):
        raise BatchError(f"{what} must be a non-boolean integer, got {v!r}")
    return v


def parse_change_file(path: str):
    """Read and validate a UTF-8 JSONL change file.

    Returns (header, raw_bytes, sha256, changes).  Any I/O or semantic
    failure raises BatchError (or OSError for unreadable files); nothing is
    consumed before this returns successfully.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        raise
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BatchError(f"change file is not valid UTF-8: {exc}") from exc

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise BatchError("change file is empty")
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise BatchError(f"first non-empty line must be the batch header: {exc}")
    if not isinstance(header, dict) or set(header) != {"batch_id", "base_revision"}:
        raise BatchError(
            "header must be exactly {\"batch_id\", \"base_revision\"}"
        )
    batch_id = header.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id:
        raise BatchError("batch_id must be a non-empty string")
    base_revision = _strict_int(header.get("base_revision"), "base_revision")
    if base_revision < 0:
        raise BatchError("base_revision must be non-negative")
    if len(lines) < 2:
        raise BatchError("batch contains no changes")

    changes = []
    seen = set()
    for lineno, line in enumerate(lines[1:], start=2):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BatchError(f"line {lineno}: invalid JSON: {exc}")
        if not isinstance(rec, dict):
            raise BatchError(f"line {lineno}: change must be a JSON object")
        op = rec.get("op")
        if op not in ("put", "delete"):
            raise BatchError(f"line {lineno}: op must be 'put' or 'delete'")
        expected = _strict_int(rec.get("expected_version"),
                               f"line {lineno}: expected_version")
        if expected < 0:
            raise BatchError(f"line {lineno}: expected_version must be >= 0")
        if op == "put":
            entity = rec.get("entity")
            if not isinstance(entity, dict):
                raise BatchError(f"line {lineno}: put requires an entity")
            try:
                kind, entity_id, canonical = dbmod.validate_record(entity)
            except ValueError as exc:
                raise BatchError(f"line {lineno}: {exc}")
            key = (kind, entity_id)
            if key in seen:
                raise BatchError(
                    f"line {lineno}: {kind} {entity_id} appears more than once"
                    " in this batch"
                )
            seen.add(key)
            changes.append(
                {"op": "put", "expected_version": expected, "type": kind,
                 "id": entity_id, "payload": canonical}
            )
        else:
            kind = rec.get("type")
            if kind not in ENTITY_TYPES:
                raise BatchError(
                    f"line {lineno}: delete type must be node/way/relation"
                )
            entity_id = _strict_int(rec.get("id"), f"line {lineno}: id")
            key = (kind, entity_id)
            if key in seen:
                raise BatchError(
                    f"line {lineno}: {kind} {entity_id} appears more than once"
                    " in this batch"
                )
            seen.add(key)
            changes.append(
                {"op": "delete", "expected_version": expected,
                 "type": kind, "id": entity_id, "payload": None}
            )
    return (
        {"batch_id": batch_id, "base_revision": base_revision},
        raw,
        digest,
        changes,
    )


# ---------------------------------------------------------------------------
# reverse dependency traversal with real, revision-grounded witness paths
# ---------------------------------------------------------------------------

def _reverse_bfs(view: RevisionView, starts):
    """Reverse BFS over edges effective at the view's revision.

    Returns {relation_id: witness_path} where each path entry is
    [src_type, src_id, member_index, ref_type, ref_id, role] ordered from
    the changed entity outward to the relation.
    """
    reached: dict = {}
    queue = deque()
    visited = set()
    for kind, entity_id in starts:
        queue.append(((kind, entity_id), []))
        visited.add((kind, entity_id))
    while queue:
        ((cur_type, cur_id), path) = queue.popleft()
        for src_type, src_id, member_index, role in view.referrers(
            cur_type, cur_id
        ):
            if (src_type, src_id) in visited:
                continue
            visited.add((src_type, src_id))
            edge = [src_type, src_id, member_index, cur_type, cur_id, role]
            new_path = path + [edge]
            if src_type == "relation" and src_id not in reached:
                reached[src_id] = new_path
            queue.append(((src_type, src_id), new_path))
    return reached


def _new_referrers_override(changes, base_revision):
    """Referrer sets contributed by ways/relations changed in this batch.

    Returns (changed_srcs, refs_by_target) describing the *new* graph's
    edges originating at changed sources; old snapshots of these sources
    are replaced wholesale.
    """
    changed_srcs = set()
    outgoing = {}
    for ch in changes:
        if ch["type"] in ("way", "relation"):
            changed_srcs.add((ch["type"], ch["id"]))
            if ch["op"] == "put":
                outgoing[(ch["type"], ch["id"])] = list(
                    _edges_of(ch["type"], ch["id"], ch["payload"])
                )
            else:
                outgoing[(ch["type"], ch["id"])] = []
    refs_by_target: dict = {}
    for (src_type, src_id), edges in outgoing.items():
        for edge in edges:
            _, _, member_index, ref_type, ref_id, role = edge
            refs_by_target.setdefault((ref_type, ref_id), []).append(
                (src_type, src_id, member_index, role)
            )
    return changed_srcs, refs_by_target


def _reverse_bfs_new(view: RevisionView, starts, changed_srcs, refs_by_target):
    """Reverse BFS over the post-batch graph, built from the base revision's
    edge index with changed sources replaced by their new member sets.

    No edge is invented: every non-changed edge comes from a real edge
    snapshot at the base revision, every changed edge from the batch's full
    entity definition.
    """
    reached: dict = {}
    queue = deque()
    visited = set()
    for kind, entity_id in starts:
        queue.append(((kind, entity_id), []))
        visited.add((kind, entity_id))
    while queue:
        cur = (cur_type, cur_id) = queue[0][0]
        path = queue[0][1]
        queue.popleft()
        referrers = []
        for edge in view.referrers(cur_type, cur_id):
            if (edge[0], edge[1]) not in changed_srcs:
                referrers.append(edge)
        referrers.extend(refs_by_target.get(cur, ()))
        for src_type, src_id, member_index, role in referrers:
            if (src_type, src_id) in visited:
                continue
            visited.add((src_type, src_id))
            new_path = path + [
                [src_type, src_id, member_index, cur_type, cur_id, role]
            ]
            if src_type == "relation" and src_id not in reached:
                reached[src_id] = new_path
            queue.append(((src_type, src_id), new_path))
    return reached


def _affected_relations(conn, changes, base_revision):
    starts = {(c["type"], c["id"]) for c in changes}
    old_view = RevisionView(conn, base_revision)
    old_reached = _reverse_bfs(old_view, starts)
    changed_srcs, refs_by_target = _new_referrers_override(changes, base_revision)
    new_reached = _reverse_bfs_new(
        old_view, starts, changed_srcs, refs_by_target
    )
    affected = {}
    for rid in sorted(set(old_reached) | set(new_reached)):
        in_old, in_new = rid in old_reached, rid in new_reached
        basis = "both" if in_old and in_new else ("old" if in_old else "new")
        paths = []
        if in_old:
            paths.append({"basis": "old", "path": old_reached[rid]})
        if in_new and (not in_old or new_reached[rid] != old_reached[rid]):
            paths.append({"basis": "new", "path": new_reached[rid]})
        affected[rid] = {"basis": basis, "paths": paths}
    # directly created/modified/deleted relations are affected even with no
    # incoming reference edge
    for ch in changes:
        if ch["type"] != "relation":
            continue
        rid = ch["id"]
        entry = affected.setdefault(
            rid,
            {"basis": "both",
             "paths": [{"basis": "direct", "path": []}]},
        )
        if not any(p["basis"] == "direct" for p in entry["paths"]):
            entry["paths"].insert(0, {"basis": "direct", "path": []})
        if ch["op"] == "put" and ch["expected_version"] == 0:
            entry["basis"] = "new"
        elif ch["op"] == "delete":
            entry["basis"] = "old"
    return affected


# ---------------------------------------------------------------------------
# incremental assembly of the affected closure
# ---------------------------------------------------------------------------

def _forward_closure(conn, revision, affected: set):
    """All relations needed to resolve ``affected``: the affected set plus
    its relation-child closure at the new revision."""
    view = RevisionView(conn, revision)
    closure = set(affected)
    stack = list(affected)
    while stack:
        rid = stack.pop()
        for _idx, child, _role in view.child_relations(rid):
            if child not in closure and view.relation(child) is not None:
                closure.add(child)
                stack.append(child)
    return closure, view


def _load_cached_outcome(conn, revision, rid):
    row = conn.execute(
        "SELECT status, reason, expansion, n_occ, n_nodes FROM rel_result_history"
        " WHERE relation_id = ? AND revision <= ?"
        " ORDER BY revision DESC LIMIT 1",
        (rid, revision),
    ).fetchone()
    if row is None:
        return None
    expansion = None
    if row["expansion"]:
        expansion = [
            Occ(way=o[0], path=tuple(tuple(p) for p in o[1]), role=o[2])
            for o in json.loads(row["expansion"])
        ]
    return Outcome(
        status=row["status"],
        reason=json.loads(row["reason"]) if row["reason"] else None,
        expansion=expansion,
        n_occ=row["n_occ"],
        n_nodes=row["n_nodes"],
    )


def _recompute(conn, new_revision, affected: set, max_ways, max_nodes):
    """Resolve exactly the affected relations at the new revision.

    Non-affected children keep their cached outcomes; geometry fetches are
    bounded by the affected closure.
    """
    closure, view = _forward_closure(conn, new_revision, set(affected))
    relations = view.relations_map(closure)
    # include definitions of cached (out-of-closure) children so the seam's
    # gap analysis sees every real reference
    cached = {}
    extra_defs = {}
    for rid in closure - affected:
        out = _load_cached_outcome(conn, new_revision - 1, rid)
        if out is not None:
            cached[rid] = out
            extra_defs[rid] = view.relation(rid)
    for rid in list(closure):
        for _idx, child, _role in view.child_relations(rid):
            if child not in relations:
                definition = view.relation(child)
                if definition is not None:
                    extra_defs[child] = definition
                    out = _load_cached_outcome(conn, new_revision - 1, child)
                    if out is not None:
                        cached[child] = out
    relations.update({k: v for k, v in extra_defs.items() if v is not None})

    graph = build_graph(relations)
    cyclic = find_cycles(graph)
    # never re-resolve a cached relation: cycles wholly inside the cached
    # region already have their stored outcomes.  In practice any SCC touched
    # by a change is entirely inside the affected closure (all its members are
    # mutually reachable), but guard explicitly so a cached outcome can never
    # be silently overwritten by a freshly derived cycle marker.
    skip = set(cyclic) | set(cached)
    results: dict = dict(cached)
    for rid, chain in cyclic.items():
        if rid in affected and rid not in cached:
            results[rid] = Outcome(
                status=INCOMPLETE,
                reason={"code": "relation_cycle", "chain": chain},
            )
    for rid in postorder(relations, skip):
        if rid in cached:
            continue
        results[rid] = _resolve(
            view, rid, relations, results, max_ways, max_nodes, view.stats
        )
    return results, view, sorted(closure)


# ---------------------------------------------------------------------------
# provenance / source descriptors and result persistence
# ---------------------------------------------------------------------------

def _source_descriptor(outcome: Outcome, view, revision):
    """Full source fingerprint: every segment's relation/way occurrence path
    plus the object versions of the way and the nodes actually used."""
    if outcome.status != COMPLETE:
        return None
    desc = []
    for face in outcome.faces:
        for ring in face["rings"]:
            for seg in ring["segments"]:
                way_version = view.version_of("way", seg["way"])
                node_versions = [
                    [nid, view.version_of("node", nid)] for nid in seg["nodes"]
                ]
                desc.append(
                    {
                        "way": seg["way"],
                        "way_version": way_version,
                        "path": seg["path"],
                        "reversed": seg["reversed"],
                        "node_versions": node_versions,
                    }
                )
    return json.dumps(desc, sort_keys=True, separators=(",", ":"))


def _expansion_json(outcome: Outcome):
    if outcome.expansion is None:
        return None
    return json.dumps(
        [[o.way, [list(p) for p in o.path], o.role] for o in outcome.expansion]
    )


def _persist_result(conn, revision, rid, outcome: Outcome, source_desc):
    conn.execute(
        "INSERT INTO rel_result_history(revision, relation_id, status, reason,"
        " geometry, source_desc, expansion, n_occ, n_nodes)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (
            revision,
            rid,
            outcome.status,
            json.dumps(outcome.reason) if outcome.reason else None,
            json.dumps(outcome.geometry) if outcome.geometry else None,
            source_desc,
            _expansion_json(outcome),
            outcome.n_occ,
            outcome.n_nodes,
        ),
    )
    if outcome.status != COMPLETE:
        return
    for face_index, face in enumerate(outcome.faces):
        conn.execute(
            "INSERT INTO face_history(revision, relation_id, face_index, geojson)"
            " VALUES(?,?,?,?)",
            (revision, rid, face_index, json.dumps(face["geojson"])),
        )
        for ring_index, ring in enumerate(face["rings"]):
            conn.execute(
                "INSERT INTO ring_history(revision, relation_id, face_index,"
                " ring_index, role, is_hole, node_ids) VALUES(?,?,?,?,?,?,?)",
                (
                    revision, rid, face_index, ring_index, ring["role"],
                    1 if ring["hole"] else 0, json.dumps(ring["nodes"]),
                ),
            )
            for seq, seg in enumerate(ring["segments"]):
                conn.execute(
                    "INSERT INTO ring_segment_history(revision, relation_id,"
                    " face_index, ring_index, seq, way_id, path, reversed,"
                    " node_ids, way_version, node_versions)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        revision, rid, face_index, ring_index, seq, seg["way"],
                        json.dumps(seg["path"]), 1 if seg["reversed"] else 0,
                        json.dumps(seg["nodes"]),
                        seg.get("_way_version", 0),
                        json.dumps(seg.get("_node_versions", [])),
                    ),
                )


def _annotate_segment_versions(view, outcome):
    """Stamp object versions onto segment dicts before persistence."""
    if outcome.status != COMPLETE:
        return
    for face in outcome.faces:
        for ring in face["rings"]:
            for seg in ring["segments"]:
                seg["_way_version"] = view.version_of("way", seg["way"])
                seg["_node_versions"] = [
                    [nid, view.version_of("node", nid)] for nid in seg["nodes"]
                ]


def _mirror_legacy_results(conn, new_revision, outcomes):
    """Keep the legacy result tables pointing at the head revision, touching
    only recomputed (or deleted) relations."""
    for rid, outcome in outcomes.items():
        conn.execute(
            "DELETE FROM ring_segments WHERE ring_id IN ("
            " SELECT r.id FROM rings r JOIN faces f ON r.face_id = f.id"
            " WHERE f.relation_id = ?)",
            (rid,),
        )
        conn.execute(
            "DELETE FROM rings WHERE face_id IN ("
            " SELECT id FROM faces WHERE relation_id = ?)",
            (rid,),
        )
        conn.execute("DELETE FROM faces WHERE relation_id = ?", (rid,))
        conn.execute("DELETE FROM relation_status WHERE relation_id = ?", (rid,))
        if outcome is None or outcome.status == ABSENT:
            continue
        conn.execute(
            "INSERT INTO relation_status(relation_id, status, reason) VALUES(?,?,?)",
            (rid, outcome.status,
             json.dumps(outcome.reason) if outcome.reason else None),
        )
        if outcome.status != COMPLETE:
            continue
        face_id = _next_id(conn, "faces")
        ring_id = _next_id(conn, "rings")
        segment_id = _next_id(conn, "ring_segments")
        for face_index, face in enumerate(outcome.faces):
            conn.execute(
                "INSERT INTO faces(id, relation_id, face_index, geojson)"
                " VALUES(?,?,?,?)",
                (face_id, rid, face_index, json.dumps(face["geojson"])),
            )
            for ring_index, ring in enumerate(face["rings"]):
                conn.execute(
                    "INSERT INTO rings(id, face_id, ring_index, role, is_hole,"
                    " node_ids) VALUES(?,?,?,?,?,?)",
                    (ring_id, face_id, ring_index, ring["role"],
                     1 if ring["hole"] else 0, json.dumps(ring["nodes"])),
                )
                for seq, seg in enumerate(ring["segments"]):
                    conn.execute(
                        "INSERT INTO ring_segments(id, ring_id, seq, way_id,"
                        " path, reversed, node_ids) VALUES(?,?,?,?,?,?,?)",
                        (
                            segment_id, ring_id, seq, seg["way"],
                            json.dumps(seg["path"]),
                            1 if seg["reversed"] else 0,
                            json.dumps(seg["nodes"]),
                        ),
                    )
                    segment_id += 1
                ring_id += 1
            face_id += 1


def _next_id(conn, table):
    row = conn.execute(f"SELECT COALESCE(MAX(id), -1) + 1 AS n FROM {table}").fetchone()
    return row["n"]


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def apply_changes(db_path: str, change_path: str, sync: SyncPoint | None = None) -> dict:
    """Apply one change batch atomically and return its receipt."""
    sync = sync or SyncPoint()
    header, raw, digest, changes = parse_change_file(change_path)
    batch_id = header["batch_id"]
    base_revision = header["base_revision"]
    sync.reach("validated")

    conn = connect(db_path)
    try:
        _require_history(conn)
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            # another writer holds the commit lock; the loser must get an
            # explicit stale/in-progress conflict, never a raw SQLite error
            raise RevisionConflict(
                "database is locked by another in-progress apply; retry"
                " against the new head revision"
            ) from exc
        try:
            holder = f"pid={os.getpid()}"
            try:
                # any existing row is a stale lock left by a crashed writer;
                # BEGIN IMMEDIATE already guarantees no live writer holds it
                conn.execute("DELETE FROM apply_lock WHERE id = 1")
                conn.execute(
                    "INSERT INTO apply_lock(id, holder, acquired_at) VALUES(1,?,?)",
                    (holder, dbmod.utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise RevisionConflict(
                    "another apply is in progress (lock held)"
                ) from exc

            registered = conn.execute(
                "SELECT receipt, sha256 FROM batch_registry WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if registered is not None:
                if registered["sha256"] != digest:
                    raise BatchIdConflict(
                        f"batch_id {batch_id!r} was already received with"
                        " different input bytes"
                    )
                conn.rollback()
                return json.loads(registered["receipt"])

            head = conn.execute(
                "SELECT head_revision FROM rev_meta WHERE id = 1"
            ).fetchone()["head_revision"]
            if base_revision != head:
                raise RevisionConflict(
                    f"base_revision={base_revision} is stale; head is {head}"
                )
            new_revision = head + 1
            max_ways, max_nodes = _budget(conn)

            change_entries = _check_versions_and_stage(
                conn, changes, new_revision
            )
            affected = _affected_relations(conn, changes, head)
            sync.reach("precommit")

            results, view, closure = _recompute(
                conn, new_revision, set(affected), max_ways, max_nodes
            )
            relation_changes = {
                c["id"]: c for c in changes if c["type"] == "relation"
            }
            receipt_relations = _write_revision(
                conn, head, new_revision, changes, change_entries,
                affected, results, view, relation_changes,
                max_ways, max_nodes,
            )

            conn.execute(
                "UPDATE rev_meta SET head_revision = ? WHERE id = 1",
                (new_revision,),
            )
            dbmod.set_meta(conn, "history_head", new_revision)
            receipt = {
                "batch_id": batch_id,
                "input_sha256": digest,
                "input_bytes": len(raw),
                "base_revision": base_revision,
                "revision": new_revision,
                "committed_at": dbmod.utcnow(),
                "changes": change_entries,
                "affected_relations": receipt_relations,
                "recomputed_relations": sorted(affected),
                "fetched": view.stats.as_dict(),
                "limits": {"ways": max_ways, "nodes": max_nodes},
            }
            conn.execute(
                "INSERT INTO batch_registry(batch_id, sha256, base_revision,"
                " revision, committed_at, receipt) VALUES(?,?,?,?,?,?)",
                (
                    batch_id, digest, base_revision, new_revision,
                    receipt["committed_at"], json.dumps(receipt, sort_keys=True),
                ),
            )
            conn.execute("DELETE FROM apply_lock WHERE id = 1")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        sync.reach("committed")
        return receipt
    finally:
        close(conn)


def _check_versions_and_stage(conn, changes, new_revision):
    """Validate expected_version against head rows; write object versions."""
    entries = []
    for ch in changes:
        kind, entity_id = ch["type"], ch["id"]
        row = conn.execute(
            "SELECT version, deleted FROM entity_versions WHERE type = ? AND id = ?",
            (kind, entity_id),
        ).fetchone()
        if row is None:
            if ch["expected_version"] != 0:
                raise BatchError(
                    f"{kind} {entity_id}: new object requires expected_version 0,"
                    f" got {ch['expected_version']}"
                )
            if ch["op"] == "delete":
                raise BatchError(
                    f"{kind} {entity_id}: cannot delete an object that does"
                    " not exist"
                )
            old_version, new_version, deleted = None, 1, 0
        else:
            current_version, is_deleted = row["version"], bool(row["deleted"])
            if ch["expected_version"] != current_version:
                raise RevisionConflict(
                    f"{kind} {entity_id}: expected_version {ch['expected_version']}"
                    f" does not match current version {current_version}"
                )
            if is_deleted and ch["op"] == "delete":
                raise BatchError(
                    f"{kind} {entity_id}: object is already deleted (tombstone"
                    f" version {current_version})"
                )
            old_version = current_version
            new_version = current_version + 1
            deleted = 1 if ch["op"] == "delete" else 0

        if row is None:
            conn.execute(
                "INSERT INTO entity_versions(type,id,version,rev,deleted,payload)"
                " VALUES(?,?,?,?,?,?)",
                (kind, entity_id, new_version, new_revision, deleted,
                 ch["payload"]),
            )
        else:
            conn.execute(
                "UPDATE entity_versions SET version=?, rev=?, deleted=?, payload=?"
                " WHERE type=? AND id=?",
                (new_version, new_revision, deleted, ch["payload"],
                 kind, entity_id),
            )
        conn.execute(
            "INSERT INTO entity_history(type,id,version,rev,deleted,payload)"
            " VALUES(?,?,?,?,?,?)",
            (kind, entity_id, new_version, new_revision, deleted, ch["payload"]),
        )
        if ch["type"] in ("way", "relation") and ch["op"] == "put":
            for edge in _edges_of(kind, entity_id, ch["payload"]):
                src_type, src_id, member_index, ref_type, ref_id, role = edge
                conn.execute(
                    "INSERT INTO ref_edge_history(revision, src_type, src_id,"
                    " member_index, ref_type, ref_id, role)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (new_revision, src_type, src_id, member_index,
                     ref_type, ref_id, role),
                )
        entries.append(
            {
                "type": kind,
                "id": entity_id,
                "op": ch["op"],
                "old_version": old_version,
                "new_version": new_version,
                "tombstone": bool(deleted),
            }
        )
    entries.sort(key=lambda e: (e["type"], e["id"]))
    return entries


def _effective_result_row(conn, revision, rid):
    return conn.execute(
        "SELECT * FROM rel_result_history WHERE relation_id = ? AND revision <= ?"
        " ORDER BY revision DESC LIMIT 1",
        (rid, revision),
    ).fetchone()


def _geometry_equal(geo_a, geo_b):
    if geo_a is None and geo_b is None:
        return True
    if geo_a is None or geo_b is None:
        return False
    from shapely.geometry import shape

    return shape(json.loads(geo_a)).equals(shape(json.loads(geo_b)))


def _write_revision(conn, head, new_revision, changes, change_entries,
                    affected, results, view, relation_changes,
                    max_ways, max_nodes):
    receipt_relations = []
    mirror = {}
    for rid in sorted(affected):
        old_row = _effective_result_row(conn, head, rid)
        old_status = old_row["status"] if old_row is not None else ABSENT
        old_geometry = old_row["geometry"] if old_row is not None else None
        old_source = old_row["source_desc"] if old_row is not None else None

        ch = relation_changes.get(rid)
        is_deleted = bool(ch and ch["op"] == "delete")
        is_new = old_row is None
        outcome = results.get(rid)
        if is_deleted:
            new_status = ABSENT
            new_geometry = None
            new_source = None
            # explicit absent marker: history readers must not see old faces
            conn.execute(
                "INSERT INTO rel_result_history(revision, relation_id, status,"
                " reason, geometry, source_desc, expansion, n_occ, n_nodes)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (new_revision, rid, ABSENT, None, None, None, None, 0, 0),
            )
            mirror[rid] = None
        else:
            if outcome is None:
                # affected relation that exists only through cached closure
                outcome = _load_cached_outcome(conn, head, rid)
            new_status = outcome.status
            new_geometry = (
                json.dumps(outcome.geometry) if outcome.geometry else None
            )
            _annotate_segment_versions(view, outcome)
            new_source = _source_descriptor(outcome, view, new_revision)
            _persist_result(conn, new_revision, rid, outcome, new_source)
            mirror[rid] = outcome

        geometry_changed = not _geometry_equal(old_geometry, new_geometry)
        source_changed = (old_source or None) != (new_source or None)
        entry = {
            "relation": rid,
            "basis": affected[rid]["basis"],
            "paths": affected[rid]["paths"],
            "status_before": old_status,
            "status_after": new_status,
            "is_new": is_new,
            "is_deleted": is_deleted,
            "geometry_changed": geometry_changed,
            "source_changed": source_changed,
        }
        receipt_relations.append(entry)
    _mirror_legacy_results(conn, new_revision, mirror)
    return receipt_relations


# ---------------------------------------------------------------------------
# historical reads
# ---------------------------------------------------------------------------

def _pin_revision(conn, revision):
    head = head_revision(conn)
    if revision is None:
        return head
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise HistoryError("revision must be a non-negative integer")
    if revision > head:
        raise HistoryError(
            f"revision {revision} does not exist; head is {head}"
        )
    return revision


def inspect_relation(conn, revision, rid):
    """Single-relation detail fully bound to one revision."""
    revision = _pin_revision(conn, revision)
    view = RevisionView(conn, revision)
    entity_rows = view._effective_rows("relation", [rid])
    row = _effective_result_row(conn, revision, rid)
    detail = {
        "revision": revision,
        "relation": rid,
        "defined": rid in entity_rows,
        "members": json.loads(entity_rows[rid][1]) if rid in entity_rows else None,
        "object_version": entity_rows[rid][0] if rid in entity_rows else None,
    }
    if row is None or row["status"] == ABSENT:
        detail.update(
            status=ABSENT if row is not None else None,
            reason=None,
            faces=[],
        )
        return detail
    result_rev = row["revision"]
    detail.update(
        status=row["status"],
        reason=json.loads(row["reason"]) if row["reason"] else None,
        result_revision=result_rev,
        faces=[],
    )
    for face in conn.execute(
        "SELECT face_index, geojson FROM face_history"
        " WHERE revision = ? AND relation_id = ? ORDER BY face_index",
        (result_rev, rid),
    ):
        rings = []
        for ring in conn.execute(
            "SELECT ring_index, role, is_hole, node_ids FROM ring_history"
            " WHERE revision = ? AND relation_id = ? AND face_index = ?"
            " ORDER BY ring_index",
            (result_rev, rid, face["face_index"]),
        ):
            segments = []
            for seg in conn.execute(
                "SELECT seq, way_id, path, reversed, node_ids, way_version,"
                " node_versions FROM ring_segment_history"
                " WHERE revision = ? AND relation_id = ? AND face_index = ?"
                " AND ring_index = ? ORDER BY seq",
                (result_rev, rid, face["face_index"], ring["ring_index"]),
            ):
                way_rows = view._effective_rows("way", [seg["way_id"]])
                definition = (
                    json.loads(way_rows[seg["way_id"]][1])
                    if seg["way_id"] in way_rows
                    else None
                )
                segments.append(
                    {
                        "way": seg["way_id"],
                        "way_version": seg["way_version"],
                        "path": json.loads(seg["path"]),
                        "reversed": bool(seg["reversed"]),
                        "nodes": json.loads(seg["node_ids"]),
                        "node_versions": json.loads(seg["node_versions"]),
                        "way_definition_nodes": definition,
                    }
                )
            rings.append(
                {
                    "role": ring["role"],
                    "hole": bool(ring["is_hole"]),
                    "nodes": json.loads(ring["node_ids"]),
                    "segments": segments,
                }
            )
        detail["faces"].append(
            {
                "face_index": face["face_index"],
                "geometry": json.loads(face["geojson"]),
                "rings": rings,
            }
        )
    return detail


def revision_overview(conn, revision):
    """Counts and per-relation statuses effective at one revision."""
    revision = _pin_revision(conn, revision)
    counts = {}
    for kind in ENTITY_TYPES:
        r = conn.execute(
            "SELECT COUNT(*) AS n FROM entity_history h"
            " WHERE h.type = ? AND h.rev <= ? AND h.deleted = 0"
            " AND h.version = (SELECT MAX(h2.version) FROM entity_history h2"
            "   WHERE h2.type = h.type AND h2.id = h.id AND h2.rev <= ?)",
            (kind, revision, revision),
        ).fetchone()
        counts[kind] = r["n"]
    status_counts = {s: 0 for s in STATUSES}
    status_counts[ABSENT] = 0
    rows = conn.execute(
        "SELECT relation_id, status FROM rel_result_history r"
        " WHERE revision = (SELECT MAX(revision) FROM rel_result_history h"
        "   WHERE h.relation_id = r.relation_id AND h.revision <= ?)",
        (revision,),
    ).fetchall()
    for r in rows:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    return {
        "revision": revision,
        "head_revision": head_revision(conn),
        "entity_counts": counts,
        "status_counts": status_counts,
        "relations": sorted(r["relation_id"] for r in rows),
    }


def get_receipt(conn, batch_id: str):
    row = conn.execute(
        "SELECT receipt FROM batch_registry WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if row is None:
        raise HistoryError(f"unknown batch_id: {batch_id!r}")
    return json.loads(row["receipt"])


def list_batches(conn):
    return [
        {
            "batch_id": r["batch_id"],
            "sha256": r["sha256"],
            "base_revision": r["base_revision"],
            "revision": r["revision"],
            "committed_at": r["committed_at"],
        }
        for r in conn.execute(
            "SELECT batch_id, sha256, base_revision, revision, committed_at"
            " FROM batch_registry ORDER BY revision"
        )
    ]


def revision_identity(conn, revision):
    """Revision metadata incl. the batch that produced it (rev 0 = base)."""
    revision = _pin_revision(conn, revision)
    identity = {"revision": revision, "head_revision": head_revision(conn)}
    if revision == 0:
        identity.update(batch_id=None, kind="baseline")
    else:
        row = conn.execute(
            "SELECT batch_id, sha256, committed_at FROM batch_registry"
            " WHERE revision = ?",
            (revision,),
        ).fetchone()
        identity.update(
            batch_id=row["batch_id"],
            input_sha256=row["sha256"],
            committed_at=row["committed_at"],
            kind="batch",
        )
    return identity

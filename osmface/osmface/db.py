"""SQLite staging layer.

All raw input records (nodes / ways / relations) are persisted here with
integer primary keys, so assembly can fetch exactly the rows it needs via
the on-disk index instead of loading the whole map into memory.

Staging state machine (meta.staging_state):
    loading  -> load in progress (never exposed as usable)
    loaded   -> input validated, no faults; assemble may run
    failed   -> faults recorded; assemble refuses to run
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timezone

from .model import (
    LoadError,
    STAGING_FAILED,
    STAGING_LOADED,
    STAGING_LOADING,
)

TOOL_NAME = "osmface"
TOOL_VERSION = "1.0.0"

SCHEMA = """
CREATE TABLE meta(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE nodes(
    id INTEGER PRIMARY KEY,
    x  REAL NOT NULL,
    y  REAL NOT NULL
);
CREATE TABLE ways(
    id      INTEGER PRIMARY KEY,
    n_nodes INTEGER NOT NULL,
    nodes   TEXT NOT NULL          -- JSON array of node ids, definition order
);
CREATE TABLE relations(
    id      INTEGER PRIMARY KEY,
    members TEXT NOT NULL          -- JSON array of {type, ref, role}
);
CREATE TABLE faults(
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    file    TEXT,
    line    INTEGER,
    kind    TEXT NOT NULL,
    message TEXT NOT NULL
);
-- assemble results (rewritten on every successful assemble)
CREATE TABLE relation_status(
    relation_id INTEGER PRIMARY KEY,
    status      TEXT NOT NULL,
    reason      TEXT               -- JSON, NULL when complete
);
CREATE TABLE faces(
    id          INTEGER PRIMARY KEY,
    relation_id INTEGER NOT NULL,
    face_index  INTEGER NOT NULL,
    geojson     TEXT NOT NULL      -- JSON Polygon geometry of this face
);
CREATE TABLE rings(
    id         INTEGER PRIMARY KEY,
    face_id    INTEGER NOT NULL,
    ring_index INTEGER NOT NULL,   -- 0 = shell, 1.. = holes of the face
    role       TEXT NOT NULL,
    is_hole    INTEGER NOT NULL,
    node_ids   TEXT NOT NULL       -- JSON closed node-id ring, canonical form
);
CREATE TABLE ring_segments(
    id       INTEGER PRIMARY KEY,
    ring_id  INTEGER NOT NULL,
    seq      INTEGER NOT NULL,
    way_id   INTEGER NOT NULL,
    path     TEXT NOT NULL,        -- JSON [[relation_id, member_index], ...]
    reversed INTEGER NOT NULL,     -- traversed opposite to the way definition
    node_ids TEXT NOT NULL         -- JSON node ids of this segment, ring order
);
"""

_CHUNK = 900  # stay below SQLite's host-parameter limit


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: str, force: bool = False) -> sqlite3.Connection:
    if os.path.exists(path):
        if not force:
            raise LoadError(f"database already exists: {path} (use --force to rebuild)")
        os.remove(path)
    conn = connect(path)
    conn.executescript(SCHEMA)
    set_meta(conn, "staging_state", STAGING_LOADING)
    set_meta(conn, "staging_db", os.path.abspath(path))
    set_meta(conn, "tool", TOOL_NAME)
    set_meta(conn, "tool_version", TOOL_VERSION)
    set_meta(conn, "created", utcnow())
    conn.commit()
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return default if row is None else json.loads(row["value"])


# ---------------------------------------------------------------------------
# record validation
# ---------------------------------------------------------------------------

def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_finite_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_record(rec):
    """Validate one JSONL record.

    Returns (kind, record_id, canonical_json).  The canonical form is what
    duplicate-detection compares: byte-identical semantics, key order fixed.
    Raises ValueError on any schema violation.
    """
    if not isinstance(rec, dict):
        raise ValueError("record is not a JSON object")
    kind = rec.get("type")
    if kind not in ("node", "way", "relation"):
        raise ValueError(f"unknown or missing record type: {kind!r}")
    rid = rec.get("id")
    if not _is_int(rid):
        raise ValueError("id must be an integer")
    if kind == "node":
        x, y = rec.get("x"), rec.get("y")
        if not _is_finite_number(x) or not _is_finite_number(y):
            raise ValueError("node coordinates must be finite numbers")
        canonical = json.dumps([x, y], separators=(",", ":"))
    elif kind == "way":
        nodes = rec.get("nodes")
        if (
            not isinstance(nodes, list)
            or len(nodes) < 2
            or not all(_is_int(n) for n in nodes)
        ):
            raise ValueError("way nodes must be a list of at least two integer node ids")
        canonical = json.dumps(nodes, separators=(",", ":"))
    else:
        members = rec.get("members")
        if not isinstance(members, list):
            raise ValueError("relation members must be a list")
        norm = []
        for i, m in enumerate(members):
            if not isinstance(m, dict):
                raise ValueError(f"member {i} is not an object")
            mtype = m.get("type")
            if mtype not in ("way", "relation"):
                raise ValueError(f"member {i}: unsupported member type {mtype!r}")
            ref = m.get("ref")
            if not _is_int(ref):
                raise ValueError(f"member {i}: ref must be an integer")
            role = m.get("role")
            if role not in ("outer", "inner"):
                raise ValueError(f"member {i}: role must be 'outer' or 'inner'")
            norm.append({"type": mtype, "ref": ref, "role": role})
        canonical = json.dumps(norm, separators=(",", ":"))
    return kind, rid, canonical


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def _fault(conn, path, lineno, kind, message):
    conn.execute(
        "INSERT INTO faults(file, line, kind, message) VALUES(?,?,?,?)",
        (path, lineno, kind, message),
    )


def _is_identical(conn, kind, rid, canonical) -> bool:
    if kind == "node":
        row = conn.execute("SELECT x, y FROM nodes WHERE id = ?", (rid,)).fetchone()
        # compare numerically: REAL storage renders ints as floats (0 -> 0.0)
        return row is not None and json.loads(canonical) == [row["x"], row["y"]]
    if kind == "way":
        row = conn.execute("SELECT nodes FROM ways WHERE id = ?", (rid,)).fetchone()
        return row is not None and row["nodes"] == canonical
    row = conn.execute("SELECT members FROM relations WHERE id = ?", (rid,)).fetchone()
    return row is not None and row["members"] == canonical


def load_files(conn, files) -> dict:
    """Stream JSONL files into the staging tables.

    Exactly identical duplicate definitions merge silently; conflicting
    redefinitions and malformed records are recorded as faults and mark the
    staging database failed.
    """
    counts = {"node": 0, "way": 0, "relation": 0}
    merged = 0
    nfaults = 0
    sources = []
    for path in files:
        with open(path, "rb") as fh:
            raw = fh.read()
        sources.append(
            {
                "file": os.path.abspath(path),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            }
        )
        for lineno, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
            text = line.strip()
            if not text:
                continue
            try:
                rec = json.loads(text)
            except json.JSONDecodeError as exc:
                _fault(conn, path, lineno, "parse_error", str(exc))
                nfaults += 1
                continue
            try:
                kind, rid, canonical = validate_record(rec)
            except ValueError as exc:
                _fault(conn, path, lineno, "schema_error", str(exc))
                nfaults += 1
                continue
            try:
                if kind == "node":
                    x, y = json.loads(canonical)
                    conn.execute("INSERT INTO nodes(id, x, y) VALUES(?,?,?)", (rid, x, y))
                elif kind == "way":
                    nodes = json.loads(canonical)
                    conn.execute(
                        "INSERT INTO ways(id, n_nodes, nodes) VALUES(?,?,?)",
                        (rid, len(nodes), canonical),
                    )
                else:
                    conn.execute(
                        "INSERT INTO relations(id, members) VALUES(?,?)", (rid, canonical)
                    )
                counts[kind] += 1
            except sqlite3.IntegrityError:
                if _is_identical(conn, kind, rid, canonical):
                    merged += 1
                else:
                    _fault(
                        conn,
                        path,
                        lineno,
                        "conflicting_definition",
                        f"{kind} {rid} redefined with different content",
                    )
                    nfaults += 1
    state = STAGING_FAILED if nfaults else STAGING_LOADED
    set_meta(conn, "counts", counts)
    set_meta(conn, "merged_duplicates", merged)
    set_meta(conn, "fault_count", nfaults)
    set_meta(conn, "sources", sources)
    set_meta(conn, "staging_state", state)
    conn.commit()
    return {
        "counts": counts,
        "merged_duplicates": merged,
        "faults": nfaults,
        "staging_state": state,
    }


# ---------------------------------------------------------------------------
# indexed fetch helpers (assembly reads only what one relation needs)
# ---------------------------------------------------------------------------

class FetchStats:
    """Operational counters proving assembly touches only indexed subsets."""

    def __init__(self):
        self.node_rows = 0
        self.way_rows = 0

    def as_dict(self):
        return {"node_rows": self.node_rows, "way_rows": self.way_rows}


def _chunks(ids):
    ids = list(ids)
    for i in range(0, len(ids), _CHUNK):
        yield ids[i : i + _CHUNK]


def fetch_nodes(conn, ids, stats: FetchStats | None = None) -> dict:
    out = {}
    for chunk in _chunks(ids):
        q = "SELECT id, x, y FROM nodes WHERE id IN (%s)" % ",".join("?" * len(chunk))
        for row in conn.execute(q, chunk):
            out[row["id"]] = (row["x"], row["y"])
            if stats is not None:
                stats.node_rows += 1
    return out


def fetch_ways(conn, ids, stats: FetchStats | None = None) -> dict:
    out = {}
    for chunk in _chunks(ids):
        q = "SELECT id, nodes FROM ways WHERE id IN (%s)" % ",".join("?" * len(chunk))
        for row in conn.execute(q, chunk):
            out[row["id"]] = json.loads(row["nodes"])
            if stats is not None:
                stats.way_rows += 1
    return out


def fetch_way_lengths(conn, ids) -> dict:
    out = {}
    for chunk in _chunks(ids):
        q = "SELECT id, n_nodes FROM ways WHERE id IN (%s)" % ",".join("?" * len(chunk))
        for row in conn.execute(q, chunk):
            out[row["id"]] = row["n_nodes"]
    return out


def way_ids_exist(conn, ids) -> set:
    return set(fetch_way_lengths(conn, ids))


def fetch_relations(conn) -> dict:
    """All relation member lists (structure, not geometry)."""
    return {
        row["id"]: json.loads(row["members"])
        for row in conn.execute("SELECT id, members FROM relations")
    }


def fetch_faults(conn, limit=100) -> list:
    return [
        {"file": r["file"], "line": r["line"], "kind": r["kind"], "message": r["message"]}
        for r in conn.execute(
            "SELECT file, line, kind, message FROM faults ORDER BY seq LIMIT ?", (limit,)
        )
    ]

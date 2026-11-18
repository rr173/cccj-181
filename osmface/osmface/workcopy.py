"""Offline work copies: checkout, edit, preview, transport package export.

A work copy is a self-contained SQLite database checked out from one
history-enabled warehouse at one pinned revision.  It records *which*
warehouse (stable source identity) and *which* revision it came from and
holds a streaming one-time snapshot of that revision's entity definitions,
tombstones and object versions.  After checkout the copy never opens the
warehouse path again: offline edits and previews run against the copy
alone, in a fresh process, with the warehouse possibly moved or offline.

Field edits use the ordinary put/delete model.  Existing objects keep
their warehouse integer ids; objects created in the field are identified
by per-copy *local names* (e.g. ``n1``) that live in a per-entity-type
namespace and can never be confused with integer ids.  References to a
local name are written as ``{"local": "n1"}`` inside way node lists and
relation member refs.

The transport package exported from a fixed copy head carries the
checkout baseline snapshot, the net final modifications and real
digests/lengths for every file, so a receiver can independently review
provenance, baseline and final edits without the copy directory or the
original warehouse path.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3

from . import db as dbmod
from . import history as hist
from .assemble import _resolve
from .graph import build_graph, find_cycles, postorder
from .model import (
    COMPLETE,
    INCOMPLETE,
    STATUSES,
    PackageError,
    WorkcopyConflict,
    WorkcopyError,
)

WORKCOPY_SCHEMA = """
CREATE TABLE meta(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE baseline_entities(
    type    TEXT NOT NULL,
    id      INTEGER NOT NULL,
    version INTEGER NOT NULL,
    deleted INTEGER NOT NULL,
    payload TEXT,
    PRIMARY KEY (type, id)
);
CREATE TABLE wc_entities(
    type     TEXT NOT NULL,
    key      TEXT NOT NULL,
    is_local INTEGER NOT NULL,
    version  INTEGER NOT NULL,
    deleted  INTEGER NOT NULL,
    payload  TEXT,
    PRIMARY KEY (type, key)
);
CREATE TABLE edit_registry(
    edit_id    TEXT PRIMARY KEY,
    sha256     TEXT NOT NULL,
    base_edit  INTEGER NOT NULL,
    edit_head  INTEGER NOT NULL,
    applied_at TEXT NOT NULL,
    receipt    TEXT NOT NULL
);
"""

PACKAGE_FORMAT = "osmface-workcopy/1"
PACKAGE_FILES = ("provenance.json", "baseline.jsonl", "edits.jsonl")

_LOCAL_NAME_RE = re.compile(r"^(?!-?\d+$)[A-Za-z0-9_.\-]{1,64}$")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate_local_name(name, what="local name"):
    if not isinstance(name, str) or not _LOCAL_NAME_RE.match(name):
        raise WorkcopyError(
            f"{what} must be 1-64 chars of [A-Za-z0-9_.-] and must not be"
            f" an integer literal, got {name!r}"
        )
    return name


# ---------------------------------------------------------------------------
# canonical snapshot/digest helpers (shared with the merge receiver)
# ---------------------------------------------------------------------------

def _canon(payload) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _canon_numbers(value):
    """Recursively normalize numbers for digest canonicalization.

    Business definitions compare numerically (0 and 0.0 are the same
    coordinate), so the digest must render them identically: integral
    floats become ints (exact within 64 bits), everything else is kept.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value.is_integer() and abs(value) <= 2**63:
            return int(value)
        return value
    if isinstance(value, list):
        return [_canon_numbers(v) for v in value]
    if isinstance(value, dict):
        return {k: _canon_numbers(v) for k, v in value.items()}
    return value


def baseline_line(kind, entity_id, version, deleted, payload) -> str:
    """Canonical line for one baseline snapshot row (versions included).

    ``sort_keys`` normalizes object key order inside payloads; list order
    (node sequences, member sequences) is untouched and significant.
    """
    parsed = json.loads(payload) if payload is not None else None
    return json.dumps(
        [kind, entity_id, version, 1 if deleted else 0,
         _canon_numbers(parsed)],
        separators=(",", ":"), sort_keys=True,
    )


def baseline_digest(rows) -> str:
    """rows: iterable of (type, id, version, deleted, payload) sorted by
    (type, id); the digest binds checkout-time definitions AND versions."""
    digest = hashlib.sha256()
    for kind, entity_id, version, deleted, payload in rows:
        digest.update(
            baseline_line(kind, entity_id, version, deleted, payload).encode(
                "utf-8"
            )
        )
        digest.update(b"\n")
    return digest.hexdigest()


def final_state_line(kind, key, is_local, deleted, payload) -> str:
    """Canonical line for one final business-state row (no versions)."""
    parsed = json.loads(payload) if payload is not None else None
    return json.dumps(
        [kind, key, 1 if is_local else 0, 1 if deleted else 0,
         _canon_numbers(parsed)],
        separators=(",", ":"), sort_keys=True,
    )


def final_state_digest(rows) -> str:
    """rows: (type, key, is_local, deleted, payload) sorted by (type, key)."""
    digest = hashlib.sha256()
    for kind, key, is_local, deleted, payload in rows:
        digest.update(
            final_state_line(kind, key, is_local, deleted, payload).encode(
                "utf-8"
            )
        )
        digest.update(b"\n")
    return digest.hexdigest()


def delta_op_line(op: dict) -> str:
    return json.dumps(_canon_numbers(op), separators=(",", ":"),
                      sort_keys=True)


def delta_digest(ops) -> str:
    digest = hashlib.sha256()
    for op in ops:
        digest.update(delta_op_line(op).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# entity definitions with local references
# ---------------------------------------------------------------------------

def _validate_ref(ref, what):
    """A reference is a warehouse integer id or {"local": "<name>"}."""
    if _is_int(ref):
        return ref
    if isinstance(ref, dict) and set(ref) == {"local"}:
        return {"local": validate_local_name(ref["local"], what)}
    raise WorkcopyError(
        f"{what} must be an integer id or {{\"local\": name}}, got {ref!r}"
    )


def validate_edit_entity(entity):
    """Validate one offline entity definition.

    Returns (kind, key, is_local, canonical_payload).  ``key`` is the
    decimal string of the integer id for warehouse objects or the local
    name for field-created objects.  The canonical payload keeps local
    references as ``{"local": name}`` markers.
    """
    if not isinstance(entity, dict):
        raise WorkcopyError("entity is not a JSON object")
    kind = entity.get("type")
    if kind not in ("node", "way", "relation"):
        raise WorkcopyError(f"unknown or missing entity type: {kind!r}")
    has_id = "id" in entity
    has_local = "local_id" in entity
    if has_id == has_local:
        raise WorkcopyError("entity must carry exactly one of id / local_id")
    if has_id:
        if not _is_int(entity["id"]):
            raise WorkcopyError("id must be a non-boolean integer")
        key, is_local = str(entity["id"]), False
    else:
        key = validate_local_name(entity["local_id"], "local_id")
        is_local = True
    if kind == "node":
        x, y = entity.get("x"), entity.get("y")
        if not dbmod._is_finite_number(x) or not dbmod._is_finite_number(y):
            raise WorkcopyError("node coordinates must be finite numbers")
        payload = json.dumps([x, y], separators=(",", ":"))
    elif kind == "way":
        nodes = entity.get("nodes")
        if not isinstance(nodes, list) or len(nodes) < 2:
            raise WorkcopyError("way nodes must be a list of at least two"
                                " node references")
        refs = [_validate_ref(n, f"way node {i}") for i, n in enumerate(nodes)]
        payload = json.dumps(refs, separators=(",", ":"))
    else:
        members = entity.get("members")
        if not isinstance(members, list):
            raise WorkcopyError("relation members must be a list")
        norm = []
        for i, m in enumerate(members):
            if not isinstance(m, dict):
                raise WorkcopyError(f"member {i} is not an object")
            mtype = m.get("type")
            if mtype not in ("way", "relation"):
                raise WorkcopyError(
                    f"member {i}: unsupported member type {mtype!r}"
                )
            ref = _validate_ref(m.get("ref"), f"member {i} ref")
            role = m.get("role")
            if role not in ("outer", "inner"):
                raise WorkcopyError(
                    f"member {i}: role must be 'outer' or 'inner'"
                )
            norm.append({"type": mtype, "ref": ref, "role": role})
        payload = json.dumps(norm, separators=(",", ":"))
    return kind, key, is_local, payload


def entity_record(kind, key, is_local, payload):
    """Inverse of validate_edit_entity: rebuild the JSON entity dict."""
    parsed = json.loads(payload)
    if kind == "node":
        rec = {"type": "node", "x": parsed[0], "y": parsed[1]}
    elif kind == "way":
        rec = {"type": "way", "nodes": parsed}
    else:
        rec = {"type": "relation", "members": parsed}
    if is_local:
        rec["local_id"] = key
    else:
        rec["id"] = int(key)
    return rec


# ---------------------------------------------------------------------------
# checkout
# ---------------------------------------------------------------------------

def _effective_snapshot_rows(conn, revision):
    """Stream (type, id, version, deleted, payload) effective at revision,
    ordered by (type, id) — one pass, never the whole history in memory."""
    q = (
        "SELECT h.type, h.id, h.version, h.deleted, h.payload"
        " FROM entity_history h"
        " WHERE h.rev <= ?"
        " AND h.version = (SELECT MAX(h2.version) FROM entity_history h2"
        "   WHERE h2.type = h.type AND h2.id = h.id AND h2.rev <= ?)"
        " ORDER BY h.type, h.id"
    )
    cursor = conn.execute(q, (revision, revision))
    while True:
        batch = cursor.fetchmany(512)
        if not batch:
            break
        for row in batch:
            yield (row["type"], row["id"], row["version"], row["deleted"],
                   row["payload"])


def checkout_workcopy(db_path: str, workcopy_path: str,
                      revision: int | None = None, force: bool = False) -> dict:
    """Create a self-contained offline work copy at a pinned revision."""
    if os.path.exists(workcopy_path):
        if not force:
            raise WorkcopyError(
                f"work copy already exists: {workcopy_path} (use force to"
                " replace)"
            )
        os.remove(workcopy_path)
    main = hist.connect(db_path)
    try:
        hist._require_history(main)
        source_id = hist.ensure_source_id(main)
        if revision is None:
            revision = hist.head_revision(main)
        revision = hist._pin_revision(main, revision)
        max_ways, max_nodes = hist._budget(main)

        try:
            copy = dbmod.connect(workcopy_path)
            try:
                copy.executescript(WORKCOPY_SCHEMA)
                digest = hashlib.sha256()
                count = 0
                copy.execute("BEGIN IMMEDIATE")
                try:
                    for row in _effective_snapshot_rows(main, revision):
                        kind, entity_id, version, deleted, payload = row
                        digest.update(
                            baseline_line(kind, entity_id, version, deleted,
                                          payload).encode("utf-8")
                        )
                        digest.update(b"\n")
                        copy.execute(
                            "INSERT INTO baseline_entities(type,id,version,"
                            "deleted,payload) VALUES(?,?,?,?,?)",
                            (kind, entity_id, version, deleted, payload),
                        )
                        copy.execute(
                            "INSERT INTO wc_entities(type,key,is_local,"
                            "version,deleted,payload) VALUES(?,?,?,?,?,?)",
                            (kind, str(entity_id), 0, version, deleted,
                             payload),
                        )
                        count += 1
                    dbmod.set_meta(copy, "workcopy", True)
                    dbmod.set_meta(copy, "source_id", source_id)
                    dbmod.set_meta(copy, "base_revision", revision)
                    dbmod.set_meta(copy, "edit_head", 0)
                    dbmod.set_meta(copy, "created", dbmod.utcnow())
                    dbmod.set_meta(copy, "baseline_sha256",
                                   digest.hexdigest())
                    dbmod.set_meta(copy, "entity_count", count)
                    dbmod.set_meta(
                        copy, "limits", {"ways": max_ways, "nodes": max_nodes}
                    )
                    copy.commit()
                except Exception:
                    copy.rollback()
                    raise
            finally:
                copy.close()
        except Exception:
            # a failed checkout never leaves a half-written copy behind
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(workcopy_path + suffix)
                except OSError:
                    pass
            raise
    finally:
        hist.close(main)
    return {
        "workcopy": workcopy_path,
        "source_id": source_id,
        "base_revision": revision,
        "edit_head": 0,
        "entities": count,
        "baseline_sha256": digest.hexdigest(),
    }


def connect_workcopy(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise WorkcopyError(f"no such work copy: {path}")
    conn = dbmod.connect(path)
    try:
        is_copy = bool(dbmod.get_meta(conn, "workcopy", False))
    except sqlite3.OperationalError:
        is_copy = False
    if not is_copy:
        conn.close()
        raise WorkcopyError(f"not an osmface work copy: {path}")
    return conn


def workcopy_info(conn) -> dict:
    counts = {}
    locals_count = {}
    for kind in hist.ENTITY_TYPES:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM wc_entities WHERE type = ?"
            " AND is_local = 0 AND deleted = 0",
            (kind,),
        ).fetchone()
        counts[kind] = row["n"]
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM wc_entities WHERE type = ?"
            " AND is_local = 1 AND deleted = 0",
            (kind,),
        ).fetchone()
        locals_count[kind] = row["n"]
    return {
        "workcopy": True,
        "source_id": dbmod.get_meta(conn, "source_id"),
        "base_revision": dbmod.get_meta(conn, "base_revision"),
        "edit_head": dbmod.get_meta(conn, "edit_head"),
        "baseline_sha256": dbmod.get_meta(conn, "baseline_sha256"),
        "limits": dbmod.get_meta(conn, "limits"),
        "counts": counts,
        "local_counts": locals_count,
        "edits": [
            {"edit_id": r["edit_id"], "edit_head": r["edit_head"],
             "applied_at": r["applied_at"]}
            for r in conn.execute(
                "SELECT edit_id, edit_head, applied_at FROM edit_registry"
                " ORDER BY edit_head"
            )
        ],
    }


# ---------------------------------------------------------------------------
# offline edit batches
# ---------------------------------------------------------------------------

def parse_edit_file(path: str):
    """Read and validate a UTF-8 JSONL offline edit file.

    Returns (header, raw_bytes, sha256, edits).  Any failure raises
    WorkcopyError (or OSError); nothing is consumed before this returns.
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
        raise WorkcopyError(f"edit file is not valid UTF-8: {exc}") from exc
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise WorkcopyError("edit file is empty")
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise WorkcopyError(
            f"first non-empty line must be the edit header: {exc}"
        )
    if not isinstance(header, dict) or set(header) != {"edit_id", "base_edit"}:
        raise WorkcopyError('header must be exactly {"edit_id", "base_edit"}')
    edit_id = header.get("edit_id")
    if not isinstance(edit_id, str) or not edit_id:
        raise WorkcopyError("edit_id must be a non-empty string")
    base_edit = header.get("base_edit")
    if not _is_int(base_edit) or base_edit < 0:
        raise WorkcopyError("base_edit must be a non-negative integer")
    if len(lines) < 2:
        raise WorkcopyError("edit batch contains no changes")

    edits = []
    seen = set()
    for lineno, line in enumerate(lines[1:], start=2):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkcopyError(f"line {lineno}: invalid JSON: {exc}")
        if not isinstance(rec, dict):
            raise WorkcopyError(f"line {lineno}: edit must be a JSON object")
        op = rec.get("op")
        if op not in ("put", "delete"):
            raise WorkcopyError(f"line {lineno}: op must be 'put' or 'delete'")
        expected = rec.get("expected_version")
        if not _is_int(expected) or expected < 0:
            raise WorkcopyError(
                f"line {lineno}: expected_version must be a non-negative"
                " integer"
            )
        if op == "put":
            entity = rec.get("entity")
            if not isinstance(entity, dict):
                raise WorkcopyError(f"line {lineno}: put requires an entity")
            try:
                kind, key, is_local, payload = validate_edit_entity(entity)
            except WorkcopyError as exc:
                raise WorkcopyError(f"line {lineno}: {exc}") from exc
        else:
            kind = rec.get("type")
            if kind not in hist.ENTITY_TYPES:
                raise WorkcopyError(
                    f"line {lineno}: delete type must be node/way/relation"
                )
            has_id = "id" in rec
            has_local = "local_id" in rec
            if has_id == has_local:
                raise WorkcopyError(
                    f"line {lineno}: delete needs exactly one of id/local_id"
                )
            if has_id:
                if not _is_int(rec["id"]):
                    raise WorkcopyError(
                        f"line {lineno}: id must be a non-boolean integer"
                    )
                key, is_local = str(rec["id"]), False
            else:
                key = validate_local_name(rec["local_id"])
                is_local = True
            payload = None
        obj = (kind, key)
        if obj in seen:
            raise WorkcopyError(
                f"line {lineno}: {kind} {key!r} appears more than once in"
                " this batch"
            )
        seen.add(obj)
        edits.append(
            {"op": op, "expected_version": expected, "type": kind, "key": key,
             "is_local": is_local, "payload": payload}
        )
    return {"edit_id": edit_id, "base_edit": base_edit}, raw, digest, edits


def _check_local_references(conn) -> None:
    """Every local reference in a live way/relation must resolve to a live
    local object of the matching type in the copy's final state."""
    live_locals = set()
    for kind in hist.ENTITY_TYPES:
        for r in conn.execute(
            "SELECT key FROM wc_entities WHERE type = ? AND is_local = 1"
            " AND deleted = 0",
            (kind,),
        ):
            live_locals.add((kind, r["key"]))
    problems = []
    for kind, ref_kind in (("way", "node"), ("relation", "way"),
                           ("relation", "relation")):
        for r in conn.execute(
            "SELECT key, payload FROM wc_entities WHERE type = ?"
            " AND deleted = 0",
            (kind,),
        ):
            payload = json.loads(r["payload"])
            if kind == "way":
                refs = [(n["local"],) for n in payload
                        if isinstance(n, dict)]
                missing = [name for (name,) in refs
                           if (ref_kind, name) not in live_locals]
            else:
                missing = [
                    m["ref"]["local"] for m in payload
                    if m["type"] == ref_kind and isinstance(m["ref"], dict)
                    and (ref_kind, m["ref"]["local"]) not in live_locals
                ]
            for name in missing:
                problems.append(
                    f"{kind} {r['key']!r}: unknown or dead local"
                    f" {ref_kind} {name!r}"
                )
    if problems:
        raise WorkcopyError(
            "unresolved local references at batch completion: "
            + "; ".join(problems[:20])
        )


def workcopy_edit(workcopy_path: str, edit_path: str) -> dict:
    """Apply one offline edit batch atomically; return its receipt."""
    header, raw, digest, edits = parse_edit_file(edit_path)
    edit_id = header["edit_id"]
    base_edit = header["base_edit"]

    conn = connect_workcopy(workcopy_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            head = dbmod.get_meta(conn, "edit_head")
            registered = conn.execute(
                "SELECT receipt, sha256 FROM edit_registry WHERE edit_id = ?",
                (edit_id,),
            ).fetchone()
            if registered is not None:
                if registered["sha256"] != digest:
                    raise WorkcopyConflict(
                        f"edit_id {edit_id!r} was already received with"
                        " different input bytes"
                    )
                conn.rollback()
                return json.loads(registered["receipt"])
            if base_edit != head:
                raise WorkcopyConflict(
                    f"base_edit={base_edit} is stale; work copy edit head"
                    f" is {head}"
                )
            new_head = head + 1
            entries = []
            for ch in edits:
                row = conn.execute(
                    "SELECT is_local, version, deleted FROM wc_entities"
                    " WHERE type = ? AND key = ?",
                    (ch["type"], ch["key"]),
                ).fetchone()
                if row is None:
                    if not ch["is_local"]:
                        raise WorkcopyError(
                            f"{ch['type']} {ch['key']}: integer ids are"
                            " assigned by the warehouse only; create new"
                            " field objects with a local_id"
                        )
                    if ch["expected_version"] != 0:
                        raise WorkcopyError(
                            f"{ch['type']} {ch['key']}: new local object"
                            f" requires expected_version 0, got"
                            f" {ch['expected_version']}"
                        )
                    if ch["op"] == "delete":
                        raise WorkcopyError(
                            f"{ch['type']} {ch['key']}: cannot delete an"
                            " object that does not exist"
                        )
                    old_version, new_version, deleted = None, 1, 0
                    conn.execute(
                        "INSERT INTO wc_entities(type,key,is_local,version,"
                        "deleted,payload) VALUES(?,?,1,?,?,?)",
                        (ch["type"], ch["key"], new_version, deleted,
                         ch["payload"]),
                    )
                else:
                    if bool(row["is_local"]) != ch["is_local"]:
                        raise WorkcopyError(
                            f"{ch['type']} {ch['key']}: identity kind"
                            " (warehouse id vs local name) does not match"
                            " the copy"
                        )
                    current, is_deleted = row["version"], bool(row["deleted"])
                    if ch["expected_version"] != current:
                        raise WorkcopyConflict(
                            f"{ch['type']} {ch['key']}: expected_version"
                            f" {ch['expected_version']} does not match work"
                            f" copy version {current}"
                        )
                    if is_deleted and ch["op"] == "delete":
                        raise WorkcopyError(
                            f"{ch['type']} {ch['key']}: object is already"
                            f" deleted (tombstone version {current})"
                        )
                    old_version = current
                    new_version = current + 1
                    deleted = 1 if ch["op"] == "delete" else 0
                    conn.execute(
                        "UPDATE wc_entities SET version=?, deleted=?,"
                        " payload=? WHERE type=? AND key=?",
                        (new_version, deleted, ch["payload"],
                         ch["type"], ch["key"]),
                    )
                entries.append(
                    {"type": ch["type"],
                     "id" if not ch["is_local"] else "local_id":
                         int(ch["key"]) if not ch["is_local"] else ch["key"],
                     "op": ch["op"], "old_version": old_version,
                     "new_version": new_version, "tombstone": bool(deleted)}
                )
            _check_local_references(conn)
            dbmod.set_meta(conn, "edit_head", new_head)
            entries.sort(key=lambda e: (e["type"], str(
                e.get("id", e.get("local_id")))))
            receipt = {
                "edit_id": edit_id,
                "input_sha256": digest,
                "input_bytes": len(raw),
                "base_edit": base_edit,
                "edit_head": new_head,
                "applied_at": dbmod.utcnow(),
                "changes": entries,
            }
            conn.execute(
                "INSERT INTO edit_registry(edit_id, sha256, base_edit,"
                " edit_head, applied_at, receipt) VALUES(?,?,?,?,?,?)",
                (edit_id, digest, base_edit, new_head,
                 receipt["applied_at"], json.dumps(receipt, sort_keys=True)),
            )
            conn.commit()
            return receipt
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# offline preview (existing assembly pipeline against the copy)
# ---------------------------------------------------------------------------

def _token(kind, key, is_local):
    """Assembly-facing identity: integers for warehouse ids, the local
    name string for field-created objects."""
    return key if is_local else int(key)


class WorkcopyView:
    """Serves the assemble seam the copy's current entity state."""

    def __init__(self, conn):
        self.conn = conn
        self.stats = dbmod.FetchStats()

    def _rows(self, kind, tokens):
        out = {}
        for token in tokens:
            key = token if isinstance(token, str) else str(token)
            row = self.conn.execute(
                "SELECT payload, deleted FROM wc_entities WHERE type = ?"
                " AND key = ?",
                (kind, key),
            ).fetchone()
            if row is not None and not row["deleted"]:
                out[token] = row["payload"]
        return out

    def fetch_nodes(self, ids, stats=None) -> dict:
        rows = self._rows("node", list(ids))
        if stats is not None:
            stats.node_rows += len(rows)
        return {tok: tuple(json.loads(p)) for tok, p in rows.items()}

    def fetch_ways(self, ids, stats=None) -> dict:
        rows = self._rows("way", list(ids))
        if stats is not None:
            stats.way_rows += len(rows)
        return {
            tok: [n["local"] if isinstance(n, dict) else n
                  for n in json.loads(p)]
            for tok, p in rows.items()
        }

    def fetch_way_lengths(self, ids) -> dict:
        return {tok: len(nodes) for tok, nodes in
                self.fetch_ways(list(ids)).items()}

    def way_ids_exist(self, ids) -> set:
        return set(self._rows("way", list(ids)))

    def relation(self, rid):
        rows = self._rows("relation", [rid])
        if rid not in rows:
            return None
        return _serve_members(json.loads(rows[rid]))

    def relations_map(self, ids=None) -> dict:
        if ids is None:
            q = ("SELECT key, is_local, payload FROM wc_entities"
                 " WHERE type = 'relation' AND deleted = 0")
            return {
                _token("relation", r["key"], r["is_local"]): _serve_members(
                    json.loads(r["payload"])
                )
                for r in self.conn.execute(q)
            }
        rows = self._rows("relation", list(ids))
        return {tok: _serve_members(json.loads(p))
                for tok, p in rows.items()}


def _serve_members(payload):
    return [
        {"type": m["type"],
         "ref": m["ref"]["local"] if isinstance(m["ref"], dict) else m["ref"],
         "role": m["role"]}
        for m in payload
    ]


def workcopy_preview(workcopy_path: str, relation_id=None) -> dict:
    """Assemble the copy's current state with the ordinary pipeline.

    Read-only: never writes to the copy and never touches the warehouse.
    """
    from .assemble import Outcome

    conn = connect_workcopy(workcopy_path)
    try:
        conn.execute("BEGIN")  # consistent snapshot of the copy head
        try:
            info = workcopy_info(conn)
            view = WorkcopyView(conn)
            relations = view.relations_map()
            graph = build_graph(relations)
            cyclic = find_cycles(graph)
            limits = info["limits"] or {"ways": 100_000, "nodes": 1_000_000}
            results = {}
            for rid, chain in cyclic.items():
                results[rid] = Outcome(
                    status=INCOMPLETE,
                    reason={"code": "relation_cycle", "chain": chain},
                )
            for rid in postorder(relations, set(cyclic)):
                results[rid] = _resolve(
                    view, rid, relations, results,
                    limits["ways"], limits["nodes"], view.stats,
                )
        finally:
            conn.execute("COMMIT")
        counts = {s: 0 for s in STATUSES}
        for out in results.values():
            counts[out.status] += 1
        preview = {
            "workcopy": True,
            "source_id": info["source_id"],
            "base_revision": info["base_revision"],
            "edit_head": info["edit_head"],
            "relations": len(results),
            "status_counts": counts,
            "statuses": {
                str(rid): {"status": out.status, "reason": out.reason}
                for rid, out in sorted(results.items(),
                                       key=lambda kv: str(kv[0]))
            },
        }
        if relation_id is not None:
            token = relation_id
            if isinstance(token, str) and token.lstrip("-").isdigit():
                # local names may never be integer literals, so a numeric
                # token always means a warehouse integer id
                token = int(token)
            if token not in results and str(relation_id) in results:
                token = str(relation_id)
            out = results.get(token)
            preview["relation"] = {
                "relation": relation_id,
                "defined": token in relations,
                "status": out.status if out else None,
                "reason": out.reason if out else None,
                "faces": out.faces if out else None,
            }
        return preview
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# transport package export
# ---------------------------------------------------------------------------

def _delta_ops(conn):
    """Net final modifications relative to the checkout baseline.

    Definition-level differences only: object versions are the warehouse's
    business, never copied.  Live local objects join the intake set; dead
    locals leave no trace.
    """
    ops = []
    baseline = {}
    for r in conn.execute(
        "SELECT type, id, deleted, payload FROM baseline_entities"
    ):
        baseline[(r["type"], r["id"])] = (r["deleted"], r["payload"])
    for r in conn.execute(
        "SELECT type, key, is_local, deleted, payload FROM wc_entities"
        " ORDER BY type, key"
    ):
        kind, key = r["type"], r["key"]
        if r["is_local"]:
            if not r["deleted"]:
                ops.append({
                    "op": "put", "type": kind, "local_id": key,
                    "entity": entity_record(kind, key, True, r["payload"]),
                })
            continue
        entity_id = int(key)
        before = baseline.get((kind, entity_id))
        now_deleted, now_payload = r["deleted"], r["payload"]
        if before is None:  # pragma: no cover - defensive
            raise WorkcopyError(
                f"work copy holds warehouse object {kind} {entity_id} that"
                " is not in its baseline snapshot"
            )
        before_deleted, before_payload = before
        same = (bool(before_deleted) == bool(now_deleted)
                and (before_deleted
                     or json.loads(before_payload) == json.loads(now_payload)))
        if same:
            continue
        if now_deleted:
            ops.append({"op": "delete", "type": kind, "id": entity_id})
        else:
            ops.append({
                "op": "put", "type": kind, "id": entity_id,
                "entity": entity_record(kind, key, False, now_payload),
            })
    ops.sort(key=lambda o: (o["type"], str(o.get("id", o.get("local_id")))))
    return ops


def _final_state_rows(conn):
    rows = []
    for r in conn.execute(
        "SELECT type, key, is_local, deleted, payload FROM wc_entities"
        " ORDER BY type, key"
    ):
        if r["is_local"] and r["deleted"]:
            # dead local objects are copy-internal bookkeeping: they never
            # reach the package and must not materialize on the warehouse
            continue
        rows.append((r["type"], r["key"], r["is_local"], r["deleted"],
                     r["payload"]))
    return rows


def workcopy_export(workcopy_path: str, package_dir: str,
                    force: bool = False) -> dict:
    """Export the fixed copy head as a portable, self-verifying package."""
    from .export import _fsync_dir, _write_json_sync

    conn = connect_workcopy(workcopy_path)
    try:
        # one read transaction: the exported head is a single consistent
        # snapshot even if another process edits the copy concurrently
        conn.execute("BEGIN")
        try:
            info = workcopy_info(conn)
            baseline_rows = [
                (r["type"], r["id"], r["version"], r["deleted"], r["payload"])
                for r in conn.execute(
                    "SELECT type, id, version, deleted, payload"
                    " FROM baseline_entities ORDER BY type, id"
                )
            ]
            ops = _delta_ops(conn)
            final_rows = _final_state_rows(conn)
        finally:
            conn.execute("COMMIT")
        provenance = {
            "source_id": info["source_id"],
            "base_revision": info["base_revision"],
            "workcopy_head": info["edit_head"],
            "exported_at": dbmod.utcnow(),
            "baseline_sha256": baseline_digest(baseline_rows),
            "final_sha256": final_state_digest(final_rows),
            "delta_sha256": delta_digest(ops),
            "counts": {
                "baseline_entities": len(baseline_rows),
                "delta_ops": len(ops),
                "final_entities": len(final_rows),
            },
        }
    finally:
        conn.close()

    parent = os.path.dirname(os.path.abspath(package_dir)) or "."
    if not os.path.isdir(parent):
        raise PackageError(f"output parent directory does not exist: {parent}")
    if os.path.exists(package_dir) and not force:
        raise PackageError(
            f"output already exists: {package_dir} (use force to replace)"
        )
    import shutil

    tmp = f"{package_dir}.building-{os.getpid()}"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    manifest = {
        "state": "building",
        "tool": dbmod.TOOL_NAME,
        "version": dbmod.TOOL_VERSION,
        "format": PACKAGE_FORMAT,
        "created": dbmod.utcnow(),
        "files": [],
    }
    try:
        payloads = {
            "provenance.json": json.dumps(provenance, indent=2,
                                          sort_keys=True) + "\n",
            "baseline.jsonl": "".join(
                json.dumps(
                    {"type": k, "id": i, "version": v, "deleted": bool(d),
                     "payload": json.loads(p) if p is not None else None},
                    sort_keys=True) + "\n"
                for k, i, v, d, p in baseline_rows
            ),
            "edits.jsonl": "".join(delta_op_line(op) + "\n" for op in ops),
        }
        for name in PACKAGE_FILES:
            data = payloads[name].encode("utf-8")
            path = os.path.join(tmp, name)
            with open(path, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            manifest["files"].append({
                "path": name,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
        manifest["package_id"] = hashlib.sha256(
            json.dumps({
                "format": PACKAGE_FORMAT,
                "provenance": provenance,
                "files": manifest["files"],
            }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        _write_json_sync(os.path.join(tmp, "manifest.json"), manifest)
        _fsync_dir(tmp)
        if os.path.exists(package_dir):
            shutil.rmtree(package_dir)
        os.rename(tmp, package_dir)
        _fsync_dir(parent)
        manifest["state"] = "complete"
        manifest["completed"] = dbmod.utcnow()
        _write_json_sync(os.path.join(package_dir, "manifest.json"), manifest)
        _fsync_dir(package_dir)
    except Exception:
        for d in (tmp, package_dir):
            marker = os.path.join(d, "manifest.json")
            if os.path.isdir(d):
                try:
                    manifest["state"] = "failed"
                    _write_json_sync(marker, manifest)
                except Exception:
                    pass
        raise
    return manifest

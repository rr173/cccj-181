"""Merge intake: review plans, rulings, candidate preview, confirmation.

A transport package (see :mod:`osmface.workcopy`) is never applied
directly.  The receiver first persists a *merge plan* that pins the
package content, the checkout baseline B and the warehouse head H, and
lists, per touched object, the automatic outcome (keep the warehouse
side, take the field side, or accept a common state) or the conflict
that needs a ruling.  Rulings are saved stepwise; a candidate preview
re-interprets references over the whole candidate state after every
ruling.  Confirmation requires the head to still be exactly H, allocates
brand-new integer ids for field-created objects (tombstones count as
occupied), rewrites every local reference to the permanent ids, and
lands entity updates, materialization, history and the merge receipt in
one transaction — exactly one new warehouse revision, or none at all
when the ruled candidate carries no entity change relative to H.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3

from . import db as dbmod
from . import history as hist
from .model import (
    MergeConflict,
    MergeError,
    PackageError,
    SyncPoint,
)
from .workcopy import (
    PACKAGE_FILES,
    PACKAGE_FORMAT,
    baseline_digest,
    delta_digest,
    final_state_digest,
    validate_edit_entity,
    validate_local_name,
    _effective_snapshot_rows,
)

MERGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS merge_plans(
    plan_id       TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    package_id    TEXT NOT NULL,
    base_revision INTEGER NOT NULL,
    head_revision INTEGER NOT NULL,
    workcopy_head INTEGER NOT NULL,
    status        TEXT NOT NULL,
    merge_id      TEXT,
    payload       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_rulings(
    plan_id  TEXT NOT NULL,
    type     TEXT NOT NULL,
    id       INTEGER NOT NULL,
    choice   TEXT NOT NULL,
    entity   TEXT,
    reason   TEXT,
    saved_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, type, id)
);
CREATE TABLE IF NOT EXISTS merge_registry(
    merge_id        TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL,
    package_id      TEXT NOT NULL,
    rulings_sha256  TEXT NOT NULL,
    result_revision INTEGER NOT NULL,
    committed_at    TEXT NOT NULL,
    receipt         TEXT NOT NULL
);
"""

PLAN_OPEN = "open"
PLAN_CONFIRMED = "confirmed"

AUTO_KEEP_MAIN = "keep_main"
AUTO_TAKE_FIELD = "take_field"
AUTO_SAME = "same"
CONFLICT = "conflict"

CHOICES = ("main", "field", "baseline", "custom")


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------

def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _ensure_merge_schema(conn) -> None:
    conn.executescript(MERGE_SCHEMA)


def _states_at(conn, revision, keys):
    """{(type, id): (version, deleted, payload)} effective at revision,
    tombstones included; objects unknown at that revision are absent."""
    out = {}
    by_type = {}
    for kind, entity_id in keys:
        by_type.setdefault(kind, []).append(entity_id)
    for kind, ids in by_type.items():
        for i in range(0, len(ids), dbmod._CHUNK):
            chunk = ids[i : i + dbmod._CHUNK]
            if not chunk:
                continue
            marks = ",".join("?" * len(chunk))
            q = (
                "SELECT h.id, h.version, h.deleted, h.payload"
                " FROM entity_history h"
                " WHERE h.type = ? AND h.rev <= ? AND h.id IN (%s)"
                " AND h.version = (SELECT MAX(h2.version) FROM entity_history"
                "   h2 WHERE h2.type = h.type AND h2.id = h.id"
                "   AND h2.rev <= ?)" % marks
            )
            params = [kind, revision, *chunk, revision]
            for r in conn.execute(q, params):
                out[(kind, r["id"])] = (r["version"], r["deleted"],
                                        r["payload"])
    return out


def _state_desc(row):
    """JSON-able three-state description; versions are kept as evidence
    but never take part in the business-definition comparison."""
    if row is None:
        return {"state": "absent"}
    version, deleted, payload = row
    if deleted:
        return {"state": "deleted", "version": version}
    return {"state": "alive", "version": version,
            "payload": json.loads(payload)}


def _def_eq(a, b) -> bool:
    """Business-definition equality: alive/deleted/never-existed are three
    distinct identities; payloads compare as whole ordered objects."""
    if a["state"] != b["state"]:
        return False
    if a["state"] == "alive":
        return a["payload"] == b["payload"]
    return True


# ---------------------------------------------------------------------------
# transport package reading and validation
# ---------------------------------------------------------------------------

_SAFE_NAME = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)


def _check_safe_relpath(name) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name in (".", "..")
        or any(ch not in _SAFE_NAME for ch in name)
    ):
        raise PackageError(
            f"unsafe file path in package manifest: {name!r}; packages may"
            " only carry bare sibling file names"
        )
    return name


class Package:
    def __init__(self, manifest, provenance, baseline, ops, final_rows):
        self.manifest = manifest
        self.provenance = provenance
        self.baseline = baseline          # {(type,id): (version, deleted, payload)}
        self.ops = ops                    # canonical delta ops
        self.final_rows = final_rows      # [(type, key, is_local, deleted, payload)]
        self.package_id = manifest["package_id"]
        self.source_id = provenance["source_id"]
        self.base_revision = provenance["base_revision"]
        self.workcopy_head = provenance["workcopy_head"]

    @property
    def locals_alive(self):
        """Final live field-created objects: [(type, local_id, payload)]."""
        out = []
        for kind, key, is_local, deleted, payload in self.final_rows:
            if is_local and not deleted:
                out.append((kind, key, json.loads(payload)))
        return sorted(out, key=lambda e: (e[0], e[1]))


def _validate_baseline_payload(kind, payload, what):
    """Baseline payloads are pure warehouse form: integer refs only."""
    if kind == "node":
        if (
            not isinstance(payload, list)
            or len(payload) != 2
            or not all(dbmod._is_finite_number(c) for c in payload)
        ):
            raise PackageError(f"{what}: malformed node payload")
    elif kind == "way":
        if (
            not isinstance(payload, list)
            or len(payload) < 2
            or not all(_is_int(n) for n in payload)
        ):
            raise PackageError(f"{what}: malformed way payload")
    else:
        if not isinstance(payload, list):
            raise PackageError(f"{what}: malformed relation payload")
        for i, m in enumerate(payload):
            if (
                not isinstance(m, dict)
                or m.get("type") not in ("way", "relation")
                or not _is_int(m.get("ref"))
                or m.get("role") not in ("outer", "inner")
            ):
                raise PackageError(f"{what}: malformed relation member {i}")


def _parse_baseline_rows(text):
    baseline = {}
    rows_for_digest = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PackageError(f"baseline.jsonl line {lineno}: {exc}")
        if not isinstance(rec, dict) or set(rec) != {
            "type", "id", "version", "deleted", "payload"
        }:
            raise PackageError(
                f"baseline.jsonl line {lineno}: unexpected row shape"
            )
        kind = rec["type"]
        if kind not in hist.ENTITY_TYPES:
            raise PackageError(f"baseline.jsonl line {lineno}: bad type")
        if not _is_int(rec["id"]):
            raise PackageError(f"baseline.jsonl line {lineno}: bad id")
        if not _is_int(rec["version"]) or rec["version"] < 1:
            raise PackageError(f"baseline.jsonl line {lineno}: bad version")
        if not isinstance(rec["deleted"], bool):
            raise PackageError(f"baseline.jsonl line {lineno}: bad deleted")
        payload = rec["payload"]
        if rec["deleted"]:
            if payload is not None:
                raise PackageError(
                    f"baseline.jsonl line {lineno}: tombstone carries payload"
                )
        else:
            _validate_baseline_payload(kind, payload,
                                       f"baseline.jsonl line {lineno}")
        key = (kind, rec["id"])
        if key in baseline:
            raise PackageError(
                f"baseline.jsonl line {lineno}: duplicate identity {key}"
            )
        baseline[key] = (rec["version"], rec["deleted"],
                         json.dumps(payload, separators=(",", ":"))
                         if payload is not None else None)
        rows_for_digest.append(key)
    # digest rows must be in the canonical (type, id) order
    ordered = []
    for kind, entity_id in sorted(baseline):
        version, deleted, payload = baseline[(kind, entity_id)]
        ordered.append((kind, entity_id, version, deleted, payload))
    return baseline, ordered


def _parse_edit_ops(text):
    ops = []
    seen = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            op = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PackageError(f"edits.jsonl line {lineno}: {exc}")
        if not isinstance(op, dict):
            raise PackageError(f"edits.jsonl line {lineno}: not an object")
        kind = op.get("op")
        if kind not in ("put", "delete"):
            raise PackageError(f"edits.jsonl line {lineno}: bad op")
        etype = op.get("type")
        if etype not in hist.ENTITY_TYPES:
            raise PackageError(f"edits.jsonl line {lineno}: bad type")
        has_id = "id" in op
        has_local = "local_id" in op
        if has_id == has_local:
            raise PackageError(
                f"edits.jsonl line {lineno}: need exactly one of id/local_id"
            )
        if has_id and not _is_int(op["id"]):
            raise PackageError(f"edits.jsonl line {lineno}: bad id")
        if has_local:
            validate_local_name(op["local_id"])
        key = str(op["id"]) if has_id else op["local_id"]
        ident = (etype, key)
        if ident in seen:
            raise PackageError(
                f"edits.jsonl line {lineno}: duplicate identity {ident}"
            )
        seen.add(ident)
        if kind == "put":
            entity = op.get("entity")
            if not isinstance(entity, dict):
                raise PackageError(
                    f"edits.jsonl line {lineno}: put requires an entity"
                )
            try:
                ekind, ekey, _is_local, payload = validate_edit_entity(entity)
            except Exception as exc:
                raise PackageError(f"edits.jsonl line {lineno}: {exc}")
            if ekind != etype or ekey != key:
                raise PackageError(
                    f"edits.jsonl line {lineno}: entity identity does not"
                    " match the operation"
                )
            op = {"op": "put", "type": etype,
                  "id" if has_id else "local_id": op["id"] if has_id
                  else op["local_id"],
                  "entity": entity, "_payload": payload}
        else:
            op = {"op": "delete", "type": etype,
                  "id" if has_id else "local_id": op["id"] if has_id
                  else op["local_id"]}
        ops.append(op)
    return ops


def _check_final_local_refs(final_state):
    live_locals = {
        (kind, key) for (kind, key), (is_local, deleted, _p)
        in final_state.items() if is_local and not deleted
    }
    for (kind, key), (is_local, deleted, payload) in final_state.items():
        if deleted or kind == "node":
            continue
        parsed = json.loads(payload)
        refs = []
        if kind == "way":
            refs = [("node", n["local"]) for n in parsed
                    if isinstance(n, dict)]
        else:
            refs = [(m["type"], m["ref"]["local"]) for m in parsed
                    if isinstance(m["ref"], dict)]
        for ref_kind, name in refs:
            if (ref_kind, name) not in live_locals:
                raise PackageError(
                    f"final state: {kind} {key!r} references unknown or"
                    f" dead local {ref_kind} {name!r}"
                )


def read_package(package_dir: str) -> Package:
    """Open and fully validate a transport package.

    Integrity (lengths + real digests), types, duplicate identities and
    self-consistency (baseline + edits == final) are all verified here;
    provenance strings are never trusted without the digests matching.
    """
    manifest_path = os.path.join(package_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise PackageError(f"no manifest.json in {package_dir}")
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageError(f"unreadable manifest.json: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PackageError("manifest.json is not a JSON object")
    if manifest.get("state") != "complete":
        raise PackageError(
            f"package state is {manifest.get('state')!r}, not 'complete'"
        )
    if manifest.get("format") != PACKAGE_FORMAT:
        raise PackageError(
            f"unsupported package format: {manifest.get('format')!r}"
        )
    files = manifest.get("files")
    if not isinstance(files, list):
        raise PackageError("manifest files list is malformed")
    seen_names = set()
    contents = {}
    for entry in files:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "bytes", "sha256"}
        ):
            raise PackageError("manifest file entry is malformed")
        name = _check_safe_relpath(entry["path"])
        if name in seen_names:
            raise PackageError(f"duplicate file identity in manifest: {name}")
        seen_names.add(name)
        if not _is_int(entry["bytes"]) or entry["bytes"] < 0:
            raise PackageError(f"bad byte count for {name}")
        sha = entry["sha256"]
        if not isinstance(sha, str) or len(sha) != 64:
            raise PackageError(f"bad sha256 for {name}")
        full = os.path.join(package_dir, name)
        if not os.path.isfile(full):
            raise PackageError(f"package is missing {name}")
        with open(full, "rb") as fh:
            data = fh.read()
        if len(data) != entry["bytes"]:
            raise PackageError(
                f"{name}: length {len(data)} != manifest {entry['bytes']}"
            )
        if hashlib.sha256(data).hexdigest() != sha:
            raise PackageError(f"{name}: content digest mismatch")
        contents[name] = data
    missing = [n for n in PACKAGE_FILES if n not in contents]
    if missing:
        raise PackageError(f"package is missing required files: {missing}")

    try:
        provenance = json.loads(contents["provenance.json"])
    except json.JSONDecodeError as exc:
        raise PackageError(f"provenance.json: {exc}") from exc
    if not isinstance(provenance, dict):
        raise PackageError("provenance.json is not a JSON object")
    required = {
        "source_id": str,
        "base_revision": int,
        "workcopy_head": int,
        "baseline_sha256": str,
        "final_sha256": str,
        "delta_sha256": str,
    }
    for key, typ in required.items():
        value = provenance.get(key)
        if typ is str:
            if not isinstance(value, str) or not value:
                raise PackageError(f"provenance: bad {key}")
        elif not _is_int(value) or value < 0:
            raise PackageError(f"provenance: bad {key}")
    for key in ("baseline_sha256", "final_sha256", "delta_sha256"):
        if len(provenance[key]) != 64:
            raise PackageError(f"provenance: bad {key}")

    baseline, digest_rows = _parse_baseline_rows(
        contents["baseline.jsonl"].decode("utf-8")
    )
    if baseline_digest(digest_rows) != provenance["baseline_sha256"]:
        raise PackageError("baseline digest does not match provenance")
    ops = _parse_edit_ops(contents["edits.jsonl"].decode("utf-8"))
    canonical_ops = []
    for op in ops:
        clean = {k: v for k, v in op.items() if not k.startswith("_")}
        canonical_ops.append(clean)
    if delta_digest(canonical_ops) != provenance["delta_sha256"]:
        raise PackageError("delta digest does not match provenance")

    # rebuild the final state from baseline + delta and bind the digest
    final_state = {}
    for (kind, entity_id), (version, deleted, payload) in baseline.items():
        final_state[(kind, str(entity_id))] = (False, deleted, payload)
    for op in ops:
        has_id = "id" in op
        key = str(op["id"]) if has_id else op["local_id"]
        ident = (op["type"], key)
        if op["op"] == "put":
            if has_id and ident not in final_state:
                raise PackageError(
                    f"delta creates warehouse object {ident} that the"
                    " checkout baseline does not know"
                )
            final_state[ident] = (not has_id, False, op["_payload"])
        else:
            if ident not in final_state:
                raise PackageError(
                    f"delta deletes unknown object {ident}"
                )
            _is_local, was_deleted, _p = final_state[ident]
            if was_deleted:
                raise PackageError(
                    f"delta deletes already-deleted object {ident}"
                )
            final_state[ident] = (not has_id, True, None)
    _check_final_local_refs(final_state)
    final_rows = [
        (kind, key, is_local, deleted, payload)
        for (kind, key), (is_local, deleted, payload)
        in sorted(final_state.items())
    ]
    if final_state_digest(final_rows) != provenance["final_sha256"]:
        raise PackageError(
            "baseline + edits do not reproduce the packaged final state"
        )

    expected_id = hashlib.sha256(
        json.dumps({
            "format": PACKAGE_FORMAT,
            "provenance": provenance,
            "files": manifest["files"],
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if manifest.get("package_id") != expected_id:
        raise PackageError("package_id does not bind the real content")
    return Package(manifest, provenance, baseline, ops, final_rows)


# ---------------------------------------------------------------------------
# plan construction
# ---------------------------------------------------------------------------

def _delta_by_key(pkg: Package):
    out = {}
    for op in pkg.ops:
        key = str(op["id"]) if "id" in op else op["local_id"]
        out[(op["type"], key)] = op
    return out


def _l_state_for(key, b_row, op):
    """Field-side state for one warehouse object."""
    if op is None:
        return _state_desc(b_row)
    if op["op"] == "delete":
        return {"state": "deleted"}
    return {"state": "alive", "payload": json.loads(op["_payload"])}


def _compute_decisions(conn, pkg: Package, head: int):
    """Three-way per-object comparison over B (checkout baseline), L
    (field final state) and H (warehouse head)."""
    base_revision = pkg.base_revision
    delta = _delta_by_key(pkg)
    touched = {
        (kind, int(key)) for (kind, key) in delta if not key_is_local(key)
    }
    # warehouse objects whose definition moved between B and H
    h_rows = conn.execute(
        "SELECT DISTINCT type, id FROM entity_history"
        " WHERE rev > ? AND rev <= ?",
        (base_revision, head),
    ).fetchall()
    h_candidates = {(r["type"], r["id"]) for r in h_rows}
    need = touched | h_candidates
    b_states = _states_at(conn, base_revision, sorted(need))
    h_states = _states_at(conn, head, sorted(need))

    decisions = []
    for kind, entity_id in sorted(need):
        key = (kind, entity_id)
        b_row = b_states.get(key)
        h_row = h_states.get(key)
        b = _state_desc(b_row)
        h = _state_desc(h_row)
        op = delta.get((kind, str(entity_id)))
        l = _l_state_for(key, b_row, op)
        h_changed = not _def_eq(b, h)
        l_changed = not _def_eq(b, l)
        if not h_changed and not l_changed:
            continue
        if not l_changed:
            decision = AUTO_KEEP_MAIN
        elif not h_changed:
            decision = AUTO_TAKE_FIELD
        elif _def_eq(l, h):
            decision = AUTO_SAME
        else:
            decision = CONFLICT
        decisions.append({
            "type": kind,
            "id": entity_id,
            "b": b,
            "l": l,
            "h": h,
            "decision": decision,
        })
    return decisions


def key_is_local(key: str) -> bool:
    return not key.lstrip("-").isdigit()


def create_merge_plan(db_path: str, package_dir: str) -> dict:
    """Validate the package against this warehouse and persist a plan."""
    pkg = read_package(package_dir)
    conn = hist.connect(db_path)
    try:
        hist._require_history(conn)
        source_id = hist.ensure_source_id(conn)
        if pkg.source_id != source_id:
            raise MergeError(
                "package comes from a different warehouse"
                f" (package source {pkg.source_id}, this warehouse"
                f" {source_id}); identically-shaped warehouses without a"
                " common origin cannot exchange packages"
            )
        head = hist.head_revision(conn)
        if pkg.base_revision > head:
            raise MergeError(
                f"package baseline revision {pkg.base_revision} does not"
                f" exist; head is {head}"
            )
        digest = baseline_digest(
            _effective_snapshot_rows(conn, pkg.base_revision)
        )
        if digest != pkg.provenance["baseline_sha256"]:
            raise MergeError(
                "package baseline does not match this warehouse's history"
                f" at revision {pkg.base_revision}"
            )
        decisions = _compute_decisions(conn, pkg, head)
        plan_id = "plan-" + hashlib.sha256(
            f"{source_id}|{pkg.package_id}|{head}".encode("utf-8")
        ).hexdigest()[:24]
        payload = {
            "provenance": pkg.provenance,
            "package_id": pkg.package_id,
            "decisions": decisions,
            "locals": [
                {"type": kind, "local_id": name, "payload": payload}
                for kind, name, payload in pkg.locals_alive
            ],
        }
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_merge_schema(conn)
            row = conn.execute(
                "SELECT payload, created_at, status FROM merge_plans"
                " WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO merge_plans(plan_id, created_at, source_id,"
                    " package_id, base_revision, head_revision,"
                    " workcopy_head, status, merge_id, payload)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (plan_id, dbmod.utcnow(), source_id, pkg.package_id,
                     pkg.base_revision, head, pkg.workcopy_head,
                     PLAN_OPEN, None, json.dumps(payload, sort_keys=True)),
                )
                conn.commit()
            else:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        return get_merge_plan(db_path, plan_id)
    finally:
        hist.close(conn)


# ---------------------------------------------------------------------------
# plan loading, rulings, candidate state
# ---------------------------------------------------------------------------

def _load_plan_row(conn, plan_id):
    _ensure_merge_schema(conn)
    row = conn.execute(
        "SELECT * FROM merge_plans WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    if row is None:
        raise MergeError(f"unknown merge plan: {plan_id!r}")
    return row


def _load_rulings(conn, plan_id):
    _ensure_merge_schema(conn)
    out = {}
    for r in conn.execute(
        "SELECT type, id, choice, entity, reason, saved_at"
        " FROM merge_rulings WHERE plan_id = ? ORDER BY type, id",
        (plan_id,),
    ):
        out[(r["type"], r["id"])] = {
            "choice": r["choice"],
            "entity": json.loads(r["entity"]) if r["entity"] else None,
            "reason": r["reason"],
            "saved_at": r["saved_at"],
        }
    return out


def _rulings_digest(rulings) -> str:
    canon = [
        {"type": k[0], "id": k[1], **v}
        for k, v in sorted(rulings.items())
    ]
    return hashlib.sha256(
        json.dumps(canon, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _final_states(plan_payload, rulings):
    """Resolve every decided object to its candidate state.

    Returns (final, unresolved) where final maps (type, id) to
    {"state": "alive"/"deleted", "payload"?} and unresolved lists the
    conflict keys still waiting for a ruling.
    """
    final = {}
    unresolved = []
    for d in plan_payload["decisions"]:
        key = (d["type"], d["id"])
        ruling = rulings.get(key)
        decision = d["decision"]
        if decision == CONFLICT:
            if ruling is None:
                unresolved.append(key)
                continue
            choice = ruling["choice"]
            if choice == "main":
                final[key] = {"state": d["h"]["state"],
                              "payload": d["h"].get("payload")}
            elif choice == "field":
                final[key] = {"state": d["l"]["state"],
                              "payload": d["l"].get("payload")}
            elif choice == "baseline":
                final[key] = {"state": d["b"]["state"],
                              "payload": d["b"].get("payload")}
            else:  # custom
                final[key] = _custom_state(ruling)
        elif ruling is not None and ruling["choice"] == "custom":
            final[key] = _custom_state(ruling)
        elif decision in (AUTO_KEEP_MAIN, AUTO_SAME):
            final[key] = {"state": d["h"]["state"],
                          "payload": d["h"].get("payload")}
        else:  # take_field
            final[key] = {"state": d["l"]["state"],
                          "payload": d["l"].get("payload")}
    return final, unresolved


def _custom_state(ruling):
    entity = ruling["entity"]
    if entity is None:  # custom delete
        return {"state": "deleted"}
    kind, _key, _is_local, payload = validate_edit_entity(entity)
    return {"state": "alive", "payload": json.loads(payload)}


def _overlay_vs_h(plan_payload, rulings, h_states):
    """Candidate changes relative to H: {(type, token): state}."""
    final, unresolved = _final_states(plan_payload, rulings)
    overlay = {}
    for key, st in final.items():
        h = _state_desc(h_states.get(key))
        if not _def_eq(st, h):
            overlay[key] = st
    for loc in plan_payload["locals"]:
        overlay[(loc["type"], loc["local_id"])] = {
            "state": "alive", "payload": loc["payload"],
        }
    return overlay, unresolved


def _refs_of(kind, payload):
    """(ref_type, ref_token) pairs of one parsed payload."""
    if kind == "way":
        return [
            ("node", n["local"] if isinstance(n, dict) else n)
            for n in payload
        ]
    if kind == "relation":
        return [
            (m["type"],
             m["ref"]["local"] if isinstance(m["ref"], dict) else m["ref"])
            for m in payload
        ]
    return []


def _candidate_alive(conn, overlay, h_states_cache, kind, token, head):
    """Is (kind, token) alive in the candidate state?"""
    if (kind, token) in overlay:
        return overlay[(kind, token)]["state"] == "alive"
    if isinstance(token, str):
        return False  # unknown local name
    if (kind, token) not in h_states_cache:
        h_states_cache[(kind, token)] = _states_at(conn, head,
                                                   [(kind, token)]).get(
            (kind, token)
        )
    row = h_states_cache[(kind, token)]
    return row is not None and not row[1]


def _candidate_payload(conn, overlay, h_states_cache, kind, token, head):
    if (kind, token) in overlay:
        st = overlay[(kind, token)]
        return st["payload"] if st["state"] == "alive" else None
    if isinstance(token, str):
        return None
    if (kind, token) not in h_states_cache:
        h_states_cache[(kind, token)] = _states_at(conn, head,
                                                   [(kind, token)]).get(
            (kind, token)
        )
    row = h_states_cache[(kind, token)]
    if row is None or row[1]:
        return None
    return json.loads(row[2])


def _reference_gaps(conn, overlay, head):
    """Re-interpret references over the whole candidate state.

    Only objects whose reference situation can differ from H are checked:
    changed objects themselves plus warehouse referrers of changed
    objects.  A gap is a live object pointing at a non-live target.
    """
    view = hist.RevisionView(conn, head)
    check = set(overlay)
    for kind, token in overlay:
        if isinstance(token, int):
            for src_type, src_id, _idx, _role in view.referrers(kind, token):
                check.add((src_type, src_id))
    cache = {}
    gaps = []
    for kind, token in sorted(check, key=lambda k: (k[0], str(k[1]))):
        if kind == "node":
            continue
        payload = _candidate_payload(conn, overlay, cache, kind, token, head)
        if payload is None:
            continue
        for ref_type, ref_token in _refs_of(kind, payload):
            if not _candidate_alive(conn, overlay, cache, ref_type,
                                    ref_token, head):
                gaps.append({
                    "from": {"type": kind,
                             "id": token if isinstance(token, int) else None,
                             "local_id": token if isinstance(token, str)
                             else None},
                    "missing": {"type": ref_type,
                                "id": ref_token if isinstance(ref_token, int)
                                else None,
                                "local_id": ref_token
                                if isinstance(ref_token, str) else None},
                })
    return gaps


def get_merge_plan(db_path: str, plan_id: str) -> dict:
    """The persisted plan plus current rulings, resolved candidate states
    and reference diagnostics."""
    conn = hist.connect(db_path)
    try:
        hist._require_history(conn)
        row = _load_plan_row(conn, plan_id)
        payload = json.loads(row["payload"])
        rulings = _load_rulings(conn, plan_id)
        final, unresolved = _final_states(payload, rulings)
        decisions = []
        for d in payload["decisions"]:
            key = (d["type"], d["id"])
            ruling = rulings.get(key)
            entry = dict(d)
            entry["ruling"] = ruling
            if key in final:
                entry["final"] = final[key]
            else:
                entry["final"] = None
            decisions.append(entry)
        need = [(d["type"], d["id"]) for d in payload["decisions"]]
        h_states = _states_at(conn, row["head_revision"], need)
        overlay, _un = _overlay_vs_h(payload, rulings, h_states)
        gaps = _reference_gaps(conn, overlay, row["head_revision"])
        groups = {AUTO_KEEP_MAIN: [], AUTO_TAKE_FIELD: [], AUTO_SAME: [],
                  CONFLICT: []}
        for d in decisions:
            groups[d["decision"]].append({"type": d["type"], "id": d["id"]})
        return {
            "plan_id": plan_id,
            "status": row["status"],
            "merge_id": row["merge_id"],
            "created_at": row["created_at"],
            "source_id": row["source_id"],
            "package_id": row["package_id"],
            "base_revision": row["base_revision"],
            "head_revision": row["head_revision"],
            "workcopy_head": row["workcopy_head"],
            "provenance": payload["provenance"],
            "decisions": decisions,
            "locals": payload["locals"],
            "summary": {
                "keep_main": groups[AUTO_KEEP_MAIN],
                "take_field": groups[AUTO_TAKE_FIELD],
                "same": groups[AUTO_SAME],
                "conflicts": groups[CONFLICT],
                "new_local_objects": [
                    {"type": l["type"], "local_id": l["local_id"]}
                    for l in payload["locals"]
                ],
            },
            "unresolved_conflicts": [
                {"type": k[0], "id": k[1]} for k in unresolved
            ],
            "reference_gaps": gaps,
            "rulings_sha256": _rulings_digest(rulings),
        }
    finally:
        hist.close(conn)


def list_merge_plans(db_path: str) -> list:
    conn = hist.connect(db_path)
    try:
        hist._require_history(conn)
        _ensure_merge_schema(conn)
        return [
            {
                "plan_id": r["plan_id"],
                "status": r["status"],
                "package_id": r["package_id"],
                "base_revision": r["base_revision"],
                "head_revision": r["head_revision"],
                "created_at": r["created_at"],
                "merge_id": r["merge_id"],
            }
            for r in conn.execute(
                "SELECT plan_id, status, package_id, base_revision,"
                " head_revision, created_at, merge_id FROM merge_plans"
                " ORDER BY created_at, plan_id"
            )
        ]
    finally:
        hist.close(conn)


# ---------------------------------------------------------------------------
# rulings
# ---------------------------------------------------------------------------

def _validate_custom_entity(entity, decision, locals_by_key):
    if not isinstance(entity, dict):
        raise MergeError("custom ruling requires a full entity definition")
    try:
        kind, key, is_local, payload = validate_edit_entity(entity)
    except Exception as exc:
        raise MergeError(f"custom entity is invalid: {exc}") from exc
    if is_local:
        raise MergeError(
            "custom rulings define existing warehouse objects by integer"
            " id; new local identities must come from a re-edited package"
        )
    if kind != decision["type"] or int(key) != decision["id"]:
        raise MergeError(
            f"custom entity identity {kind} {key} does not match the ruled"
            f" object {decision['type']} {decision['id']}"
        )
    # local references may only name live package locals of the right type
    parsed = json.loads(payload)
    for ref_type, ref_token in _refs_of(kind, parsed):
        if isinstance(ref_token, str):
            if (ref_type, ref_token) not in locals_by_key:
                raise MergeError(
                    f"custom entity references unknown local {ref_type}"
                    f" {ref_token!r}; the plan cannot invent local"
                    " identities"
                )
    return entity


def save_merge_rulings(db_path: str, plan_id: str, rulings) -> dict:
    """Save rulings stepwise; each object may be re-ruled until the plan
    is confirmed.  Unknown objects and malformed rulings are rejected and
    never mix into the execution set."""
    if isinstance(rulings, dict):
        rulings = rulings.get("rulings")
    if not isinstance(rulings, list):
        raise MergeError("rulings must be a list (or {'rulings': [...]})")
    conn = hist.connect(db_path)
    try:
        hist._require_history(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_merge_schema(conn)
            row = conn.execute(
                "SELECT status, payload FROM merge_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise MergeError(f"unknown merge plan: {plan_id!r}")
            if row["status"] != PLAN_OPEN:
                raise MergeError(
                    f"plan {plan_id} is already {row['status']}; rulings"
                    " are closed"
                )
            payload = json.loads(row["payload"])
            decisions = {
                (d["type"], d["id"]): d for d in payload["decisions"]
            }
            locals_by_key = {
                (l["type"], l["local_id"]) for l in payload["locals"]
            }
            saved = 0
            seen_rulings = set()
            for i, ruling in enumerate(rulings):
                if not isinstance(ruling, dict):
                    raise MergeError(f"ruling {i}: not a JSON object")
                kind = ruling.get("type")
                entity_id = ruling.get("id")
                if kind not in hist.ENTITY_TYPES or not _is_int(entity_id):
                    raise MergeError(
                        f"ruling {i}: bad object identity {kind!r}"
                        f" {entity_id!r}"
                    )
                key = (kind, entity_id)
                if key in seen_rulings:
                    raise MergeError(
                        f"ruling {i}: {kind} {entity_id} ruled twice in"
                        " one call"
                    )
                seen_rulings.add(key)
                if key not in decisions:
                    raise MergeError(
                        f"ruling {i}: {kind} {entity_id} is not part of"
                        " this plan; unknown objects cannot be ruled"
                    )
                choice = ruling.get("choice")
                decision = decisions[key]
                if choice == "clear":
                    conn.execute(
                        "DELETE FROM merge_rulings WHERE plan_id = ?"
                        " AND type = ? AND id = ?",
                        (plan_id, kind, entity_id),
                    )
                    saved += 1
                    continue
                if decision["decision"] == CONFLICT:
                    if choice not in CHOICES:
                        raise MergeError(
                            f"ruling {i}: conflict rulings must choose one"
                            f" of {CHOICES}"
                        )
                elif choice != "custom":
                    raise MergeError(
                        f"ruling {i}: object has no content conflict"
                        f" ({decision['decision']}); only an explicit"
                        " custom override may be added"
                    )
                entity = None
                if choice == "custom":
                    if ruling.get("delete") is True:
                        entity = None
                    else:
                        entity = _validate_custom_entity(
                            ruling.get("entity"), decision, locals_by_key
                        )
                reason = ruling.get("reason")
                if reason is not None and not isinstance(reason, str):
                    raise MergeError(f"ruling {i}: reason must be a string")
                if decision["decision"] != CONFLICT and not reason:
                    raise MergeError(
                        f"ruling {i}: overrides on non-conflict objects"
                        " must state a reason"
                    )
                conn.execute(
                    "INSERT INTO merge_rulings(plan_id, type, id, choice,"
                    " entity, reason, saved_at) VALUES(?,?,?,?,?,?,?)"
                    " ON CONFLICT(plan_id, type, id) DO UPDATE SET"
                    " choice=excluded.choice, entity=excluded.entity,"
                    " reason=excluded.reason, saved_at=excluded.saved_at",
                    (plan_id, kind, entity_id, choice,
                     json.dumps(entity) if entity is not None else None,
                     reason, dbmod.utcnow()),
                )
                saved += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        plan = get_merge_plan(db_path, plan_id)
        plan["rulings_saved"] = saved
        return plan
    finally:
        hist.close(conn)


# ---------------------------------------------------------------------------
# candidate preview
# ---------------------------------------------------------------------------

class CandidateView:
    """Serves the assemble seam the candidate state: H overlaid with the
    ruled merge result.  Warehouse objects keep integer ids; field-created
    objects keep their local names (strings)."""

    def __init__(self, base_view, overlay):
        self.base = base_view
        # overlay tokens: int for warehouse objects, str for local names
        self.overlay = {}
        for (kind, token), st in overlay.items():
            self.overlay[(kind, token)] = st
        self.stats = dbmod.FetchStats()

    def _overlaid(self, kind, token):
        return (kind, token) in self.overlay

    def fetch_nodes(self, ids, stats=None) -> dict:
        ids = list(ids)
        out = {}
        base_ids = [i for i in ids if not self._overlaid("node", i)]
        if base_ids:
            out.update(self.base.fetch_nodes(base_ids, stats))
        for i in ids:
            st = self.overlay.get(("node", i))
            if st is not None and st["state"] == "alive":
                out[i] = tuple(st["payload"])
        return out

    def fetch_ways(self, ids, stats=None) -> dict:
        ids = list(ids)
        out = {}
        base_ids = [i for i in ids if not self._overlaid("way", i)]
        if base_ids:
            out.update(self.base.fetch_ways(base_ids, stats))
        for i in ids:
            st = self.overlay.get(("way", i))
            if st is not None and st["state"] == "alive":
                out[i] = [
                    n["local"] if isinstance(n, dict) else n
                    for n in st["payload"]
                ]
        return out

    def fetch_way_lengths(self, ids) -> dict:
        return {wid: len(nodes) for wid, nodes in
                self.fetch_ways(list(ids)).items()}

    def way_ids_exist(self, ids) -> set:
        ids = list(ids)
        out = set()
        base_ids = [i for i in ids if not self._overlaid("way", i)]
        if base_ids:
            out |= self.base.way_ids_exist(base_ids)
        for i in ids:
            st = self.overlay.get(("way", i))
            if st is not None and st["state"] == "alive":
                out.add(i)
        return out

    def relation(self, rid):
        st = self.overlay.get(("relation", rid))
        if st is not None:
            if st["state"] != "alive":
                return None
            return _serve_relation_payload(st["payload"])
        if isinstance(rid, str):
            return None
        return self.base.relation(rid)

    def relations_map(self, ids=None) -> dict:
        if ids is None:
            out = self.base.relations_map()
            for (kind, token), st in self.overlay.items():
                if kind != "relation":
                    continue
                if st["state"] == "alive":
                    out[token] = _serve_relation_payload(st["payload"])
                else:
                    out.pop(token, None)
            return out
        out = {}
        base_ids = [i for i in ids if not self._overlaid("relation", i)]
        int_base = [i for i in base_ids if isinstance(i, int)]
        if int_base:
            out.update(self.base.relations_map(int_base))
        for i in ids:
            st = self.overlay.get(("relation", i))
            if st is not None and st["state"] == "alive":
                out[i] = _serve_relation_payload(st["payload"])
        return out

    def child_relations(self, rid):
        st = self.overlay.get(("relation", rid))
        if st is not None:
            if st["state"] != "alive":
                return []
            return [
                (idx, m["ref"]["local"] if isinstance(m["ref"], dict)
                 else m["ref"], m["role"])
                for idx, m in enumerate(st["payload"])
                if m["type"] == "relation"
            ]
        if isinstance(rid, str):
            return []
        return self.base.child_relations(rid)


def _serve_relation_payload(payload):
    return [
        {"type": m["type"],
         "ref": m["ref"]["local"] if isinstance(m["ref"], dict)
         else m["ref"],
         "role": m["role"]}
        for m in payload
    ]


def _synthetic_changes(overlay):
    """Change records (affected-analysis shape) for the candidate delta."""
    changes = []
    for (kind, token), st in sorted(overlay.items(),
                                    key=lambda kv: (kv[0][0],
                                                    str(kv[0][1]))):
        if st["state"] == "alive":
            payload = _canonical_overlay_payload(kind, token, st["payload"])
            changes.append({"op": "put", "expected_version": 0,
                            "type": kind, "id": token, "payload": payload})
        else:
            changes.append({"op": "delete", "expected_version": 0,
                            "type": kind, "id": token, "payload": None})
    return changes


def _local_refs_to_names(value):
    """Preview-time identity: ``{"local": name}`` markers become the plain
    local-name string, matching how CandidateView serves references."""
    if isinstance(value, dict):
        if set(value) == {"local"}:
            return value["local"]
        return {k: _local_refs_to_names(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_local_refs_to_names(v) for v in value]
    return value


def _canonical_overlay_payload(kind, token, payload):
    """Canonical JSON for the overlay payload (local references are served
    as name strings at preview time)."""
    return json.dumps(_local_refs_to_names(payload), separators=(",", ":"))


def merge_preview(db_path: str, plan_id: str) -> dict:
    """Candidate map preview from all automatic results + current rulings.

    With unresolved conflicts there is no committable preview: the
    conflict list is returned instead.  Otherwise the affected relations
    are resolved against the candidate state with the ordinary pipeline.
    """
    conn = hist.connect(db_path)
    try:
        hist._require_history(conn)
        row = _load_plan_row(conn, plan_id)
        payload = json.loads(row["payload"])
        rulings = _load_rulings(conn, plan_id)
        head = row["head_revision"]
        final, unresolved = _final_states(payload, rulings)
        need = [(d["type"], d["id"]) for d in payload["decisions"]]
        h_states = _states_at(conn, head, need)
        overlay, _un = _overlay_vs_h(payload, rulings, h_states)
        gaps = _reference_gaps(conn, overlay, head)
        base = {
            "plan_id": plan_id,
            "status": row["status"],
            "base_revision": row["base_revision"],
            "head_revision": head,
            "unresolved_conflicts": [
                {"type": k[0], "id": k[1]} for k in unresolved
            ],
            "reference_gaps": gaps,
        }
        if unresolved:
            base["complete"] = False
            base["conflicts"] = [
                d for d in payload["decisions"] if d["decision"] == CONFLICT
            ]
            return base
        changes = _synthetic_changes(overlay)
        affected = hist._affected_relations(conn, changes, head)
        view = CandidateView(hist.RevisionView(conn, head), overlay)
        max_ways, max_nodes = hist._budget(conn)
        results, _view, closure = hist._recompute(
            conn, head + 1, set(affected), max_ways, max_nodes,
            view=view, cache_revision=head,
        )
        relations = {}
        for rid in sorted(affected, key=str):
            out = results.get(rid)
            if out is None:
                continue
            relations[str(rid)] = {
                "status": out.status,
                "reason": out.reason,
                "faces": out.faces if out.status == "complete" else [],
            }
        base.update(
            complete=True,
            affected_relations=sorted(affected, key=str),
            relations=relations,
            new_objects=[
                {"type": l["type"], "local_id": l["local_id"]}
                for l in payload["locals"]
            ],
        )
        return base
    finally:
        hist.close(conn)


# ---------------------------------------------------------------------------
# confirmation
# ---------------------------------------------------------------------------

def _allocate_ids(conn, locals_alive):
    """Fresh warehouse integer ids for live field-created objects.

    Ids come from above every id this warehouse has ever used per type —
    tombstones included — and are assigned deterministically in local-name
    order.  Two copies reusing the same local name therefore receive
    different permanent identities.
    """
    allocation = {}
    by_type = {}
    for loc in locals_alive:
        by_type.setdefault(loc["type"], []).append(loc["local_id"])
    for kind, names in by_type.items():
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM entity_versions"
            " WHERE type = ?",
            (kind,),
        ).fetchone()
        next_id = row["m"] + 1
        for name in sorted(names):
            allocation[(kind, name)] = next_id
            next_id += 1
    return allocation


def _materialize(kind, payload, allocation):
    """Rewrite every local reference in one parsed payload to the
    allocated permanent id; validate and canonicalize for the warehouse."""
    if kind == "node":
        entity = {"type": "node", "id": 0,
                  "x": payload[0], "y": payload[1]}
    elif kind == "way":
        nodes = []
        for n in payload:
            if isinstance(n, dict):
                nodes.append(allocation[("node", n["local"])])
            else:
                nodes.append(n)
        entity = {"type": "way", "id": 0, "nodes": nodes}
    else:
        members = []
        for m in payload:
            ref = m["ref"]
            if isinstance(ref, dict):
                ref = allocation[(m["type"], ref["local"])]
            members.append({"type": m["type"], "ref": ref,
                            "role": m["role"]})
        entity = {"type": "relation", "id": 0, "members": members}
    _kind, _id, canonical = dbmod.validate_record(entity)
    return canonical


def confirm_merge(db_path: str, plan_id: str, merge_id: str,
                  sync: SyncPoint | None = None) -> dict:
    """Land one merge plan as exactly one warehouse revision (or none)."""
    sync = sync or SyncPoint()
    if not isinstance(merge_id, str) or not merge_id:
        raise MergeError("merge_id must be a non-empty string")
    conn = hist.connect(db_path)
    try:
        hist._require_history(conn)
        row = _load_plan_row(conn, plan_id)
        payload = json.loads(row["payload"])
        rulings = _load_rulings(conn, plan_id)
        rulings_sha = _rulings_digest(rulings)
        sync.reach("validated")
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise MergeConflict(
                "database is locked by another writer; retry"
            ) from exc
        try:
            _ensure_merge_schema(conn)
            registered = conn.execute(
                "SELECT plan_id, package_id, rulings_sha256, receipt"
                " FROM merge_registry WHERE merge_id = ?",
                (merge_id,),
            ).fetchone()
            if registered is not None:
                if (
                    registered["plan_id"] == plan_id
                    and registered["package_id"] == row["package_id"]
                    and registered["rulings_sha256"] == rulings_sha
                ):
                    conn.rollback()
                    return json.loads(registered["receipt"])
                raise MergeConflict(
                    f"merge_id {merge_id!r} was already received with a"
                    " different plan, package or ruling set"
                )
            final, unresolved = _final_states(payload, rulings)
            if unresolved:
                raise MergeError(
                    f"plan has {len(unresolved)} unresolved conflicts:"
                    f" {sorted(unresolved)[:5]}; rule them or create a new"
                    " plan"
                )
            head = hist.head_revision(conn)
            if head != row["head_revision"]:
                raise MergeConflict(
                    f"warehouse head moved to {head} but the plan pins"
                    f" {row['head_revision']}; build a fresh plan instead"
                    " of reusing old rulings"
                )
            allocation = _allocate_ids(conn, payload["locals"])
            need = [(d["type"], d["id"]) for d in payload["decisions"]]
            h_states = _states_at(conn, head, need)

            changes = []
            for (kind, entity_id), st in sorted(final.items()):
                h_row = h_states.get((kind, entity_id))
                h = _state_desc(h_row)
                if _def_eq(st, h):
                    continue
                if st["state"] == "alive":
                    canonical = _materialize(kind, st["payload"], allocation)
                    changes.append({
                        "op": "put",
                        "expected_version": h_row[0],
                        "type": kind,
                        "id": entity_id,
                        "payload": canonical,
                    })
                else:
                    changes.append({
                        "op": "delete",
                        "expected_version": h_row[0],
                        "type": kind,
                        "id": entity_id,
                        "payload": None,
                    })
            for loc in payload["locals"]:
                kind, name = loc["type"], loc["local_id"]
                new_id = allocation[(kind, name)]
                canonical = _materialize(kind, loc["payload"], allocation)
                changes.append({
                    "op": "put",
                    "expected_version": 0,
                    "type": kind,
                    "id": new_id,
                    "payload": canonical,
                })
            changes.sort(key=lambda c: (c["type"], c["id"]))

            max_ways, max_nodes = hist._budget(conn)
            change_entries = []
            receipt_relations = []
            recomputed = []
            fetched = {"node_rows": 0, "way_rows": 0}
            if changes:
                new_revision = head + 1
                change_entries = hist._check_versions_and_stage(
                    conn, changes, new_revision
                )
                affected = hist._affected_relations(conn, changes, head)
                sync.reach("precommit")
                results, view, _closure = hist._recompute(
                    conn, new_revision, set(affected), max_ways, max_nodes
                )
                relation_changes = {
                    c["id"]: c for c in changes if c["type"] == "relation"
                }
                receipt_relations = hist._write_revision(
                    conn, head, new_revision, changes, change_entries,
                    affected, results, view, relation_changes,
                    max_ways, max_nodes,
                )
                conn.execute(
                    "UPDATE rev_meta SET head_revision = ? WHERE id = 1",
                    (new_revision,),
                )
                dbmod.set_meta(conn, "history_head", new_revision)
                recomputed = sorted(affected)
                fetched = view.stats.as_dict()
                result_revision = new_revision
            else:
                result_revision = head

            receipt = {
                "merge_id": merge_id,
                "kind": "merge",
                "plan_id": plan_id,
                "package_id": row["package_id"],
                "rulings_sha256": rulings_sha,
                "source_id": row["source_id"],
                "base_revision": row["base_revision"],
                "head_revision": head,
                "workcopy_head": row["workcopy_head"],
                "result_revision": result_revision,
                "committed_at": dbmod.utcnow(),
                "allocation": {
                    kind: {name: new_id
                           for (k, name), new_id in sorted(allocation.items())
                           if k == kind}
                    for kind in sorted({k for k, _n in allocation})
                },
                "baseline_versions": {
                    f"{d['type']}:{d['id']}": d["b"].get("version")
                    for d in payload["decisions"]
                    if d["b"]["state"] != "absent"
                },
                "changes": change_entries,
                "affected_relations": receipt_relations,
                "recomputed_relations": recomputed,
                "fetched": fetched,
                "limits": {"ways": max_ways, "nodes": max_nodes},
            }
            conn.execute(
                "INSERT INTO merge_registry(merge_id, plan_id, package_id,"
                " rulings_sha256, result_revision, committed_at, receipt)"
                " VALUES(?,?,?,?,?,?,?)",
                (merge_id, plan_id, row["package_id"], rulings_sha,
                 result_revision, receipt["committed_at"],
                 json.dumps(receipt, sort_keys=True)),
            )
            conn.execute(
                "UPDATE merge_plans SET status = ?, merge_id = ?"
                " WHERE plan_id = ?",
                (PLAN_CONFIRMED, merge_id, plan_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        sync.reach("committed")
        return receipt
    finally:
        hist.close(conn)


def get_merge_receipt(db_path: str, merge_id: str) -> dict:
    conn = hist.connect(db_path)
    try:
        hist._require_history(conn)
        _ensure_merge_schema(conn)
        row = conn.execute(
            "SELECT receipt FROM merge_registry WHERE merge_id = ?",
            (merge_id,),
        ).fetchone()
        if row is None:
            raise MergeError(f"unknown merge_id: {merge_id!r}")
        return json.loads(row["receipt"])
    finally:
        hist.close(conn)


def list_merge_receipts(db_path: str) -> list:
    conn = hist.connect(db_path)
    try:
        hist._require_history(conn)
        _ensure_merge_schema(conn)
        return [
            {
                "merge_id": r["merge_id"],
                "plan_id": r["plan_id"],
                "package_id": r["package_id"],
                "result_revision": r["result_revision"],
                "committed_at": r["committed_at"],
            }
            for r in conn.execute(
                "SELECT merge_id, plan_id, package_id, result_revision,"
                " committed_at FROM merge_registry ORDER BY committed_at,"
                " merge_id"
            )
        ]
    finally:
        hist.close(conn)

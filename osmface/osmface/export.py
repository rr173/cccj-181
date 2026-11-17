"""Warehouse package export.

The staging database and the output package are strictly separate states.
A package is written into a sibling ``*.building-<pid>`` directory with a
``building`` manifest, atomically renamed into place, and only then is the
manifest flipped to ``complete``.  A crash or write failure anywhere in
that sequence can never leave a ``complete`` package behind: the worst
case is a stray building directory or a ``building``/``failed`` manifest,
neither of which is a usable warehouse package.
"""
from __future__ import annotations

import json
import os
import shutil

from . import db as dbmod
from .model import (
    PKG_BUILDING,
    PKG_COMPLETE,
    PKG_FAILED,
    STATUSES,
    ExportError,
)


def _write_json_sync(path: str, payload) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def export_package(conn, results: dict, out_dir: str, force: bool = False) -> dict:
    """Write relations.jsonl + manifest.json for all resolved relations."""
    parent = os.path.dirname(os.path.abspath(out_dir)) or "."
    if not os.path.isdir(parent):
        raise ExportError(f"output parent directory does not exist: {parent}")
    if os.path.exists(out_dir) and not force:
        raise ExportError(f"output already exists: {out_dir} (use --force to replace)")

    tmp = f"{out_dir}.building-{os.getpid()}"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)

    counts = {s: 0 for s in STATUSES}
    manifest = {
        "state": PKG_BUILDING,
        "tool": dbmod.TOOL_NAME,
        "version": dbmod.TOOL_VERSION,
        "created": dbmod.utcnow(),
        "source": {
            "staging_db": dbmod.get_meta(conn, "staging_db"),
            "staging_state": dbmod.get_meta(conn, "staging_state"),
            "inputs": dbmod.get_meta(conn, "sources", []),
        },
        "counts": {},
        "files": ["relations.jsonl", "manifest.json"],
    }
    try:
        rel_path = os.path.join(tmp, "relations.jsonl")
        with open(rel_path, "w", encoding="utf-8") as fh:
            for rid in sorted(results):
                out = results[rid]
                counts[out.status] += 1
                fh.write(json.dumps(_package_line(rid, out), sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        manifest["counts"] = dict(counts, relations=len(results))
        _write_json_sync(os.path.join(tmp, "manifest.json"), manifest)
        _fsync_dir(tmp)

        if os.path.exists(out_dir):
            shutil.rmtree(out_dir)
        os.rename(tmp, out_dir)
        _fsync_dir(parent)

        # only now may the package become visible as complete
        manifest["state"] = PKG_COMPLETE
        manifest["completed"] = dbmod.utcnow()
        _write_json_sync(os.path.join(out_dir, "manifest.json"), manifest)
        _fsync_dir(out_dir)
    except Exception:
        # best effort: never leave anything that looks complete
        for d in (tmp, out_dir):
            marker = os.path.join(d, "manifest.json")
            if os.path.isdir(d):
                try:
                    manifest["state"] = PKG_FAILED
                    _write_json_sync(marker, manifest)
                except Exception:
                    pass
        raise
    return manifest


def _package_line(rid: int, out) -> dict:
    line = {"relation": rid, "status": out.status}
    if out.status == "complete":
        line["geometry"] = out.geometry
        line["faces"] = [{"rings": face["rings"]} for face in out.faces]
    else:
        line["reason"] = out.reason
    return line


def read_manifest(path: str) -> dict:
    manifest_path = os.path.join(path, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise ExportError(f"no manifest.json in {path}")
    with open(manifest_path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# revision package export (history-enabled warehouses)
# ---------------------------------------------------------------------------

def _write_jsonl(path: str, lines: list) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def export_revision(conn, out_dir: str, revision: int,
                    force: bool = False) -> dict:
    """Export an immutable package bound to one revision.

    Every detail row comes from the history tables at the chosen revision,
    never from the current entity tables.  Already-exported directories are
    never rewritten; a failed write leaves only a building/failed marker.
    """
    from .history import (
        head_revision,
        inspect_relation,
        revision_identity,
        revision_overview,
    )
    from .model import ABSENT

    if revision > head_revision(conn):
        raise ExportError(
            f"revision {revision} does not exist; head is {head_revision(conn)}"
        )
    parent = os.path.dirname(os.path.abspath(out_dir)) or "."
    if not os.path.isdir(parent):
        raise ExportError(f"output parent directory does not exist: {parent}")
    if os.path.exists(out_dir) and not force:
        raise ExportError(f"output already exists: {out_dir} (use --force to replace)")

    tmp = f"{out_dir}.building-{os.getpid()}"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)

    overview = revision_overview(conn, revision)
    identity = revision_identity(conn, revision)
    manifest = {
        "state": PKG_BUILDING,
        "tool": dbmod.TOOL_NAME,
        "version": dbmod.TOOL_VERSION,
        "created": dbmod.utcnow(),
        "revision": revision,
        "head_revision": head_revision(conn),
        "revision_identity": identity,
        "entity_counts": overview["entity_counts"],
        "status_counts": {
            k: v for k, v in overview["status_counts"].items() if k != ABSENT
        },
        "counts": dict(
            overview["entity_counts"],
            relations=len(overview["relations"]),
        ),
        "files": ["relations.jsonl", "manifest.json", "revision.json",
                  "batches.jsonl"],
    }
    try:
        lines = []
        for rid in overview["relations"]:
            detail = inspect_relation(conn, revision, rid)
            line = {
                "revision": revision,
                "relation": rid,
                "status": detail["status"],
                "object_version": detail["object_version"],
            }
            if detail["status"] == "complete":
                line["faces"] = detail["faces"]
            else:
                line["reason"] = detail["reason"]
            lines.append(line)
        _write_jsonl(os.path.join(tmp, "relations.jsonl"), lines)
        _write_jsonl(
            os.path.join(tmp, "batches.jsonl"),
            [
                dict(r)
                for r in conn.execute(
                    "SELECT batch_id, sha256, base_revision, revision,"
                    " committed_at FROM batch_registry WHERE revision <= ?"
                    " ORDER BY revision",
                    (revision,),
                )
            ],
        )
        _write_json_sync(os.path.join(tmp, "revision.json"), identity)
        _write_json_sync(os.path.join(tmp, "manifest.json"), manifest)
        _fsync_dir(tmp)

        if os.path.exists(out_dir):
            shutil.rmtree(out_dir)
        os.rename(tmp, out_dir)
        _fsync_dir(parent)

        manifest["state"] = PKG_COMPLETE
        manifest["completed"] = dbmod.utcnow()
        _write_json_sync(os.path.join(out_dir, "manifest.json"), manifest)
        _fsync_dir(out_dir)
    except Exception:
        for d in (tmp, out_dir):
            marker = os.path.join(d, "manifest.json")
            if os.path.isdir(d):
                try:
                    manifest["state"] = PKG_FAILED
                    _write_json_sync(marker, manifest)
                except Exception:
                    pass
        raise
    return manifest

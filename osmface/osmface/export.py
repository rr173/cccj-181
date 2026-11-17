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

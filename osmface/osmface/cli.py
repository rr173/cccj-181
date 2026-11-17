"""Command line interface: load / assemble / inspect."""
from __future__ import annotations

import argparse
import json
import sys

from . import api
from .assemble import DEFAULT_MAX_BOUNDARY_NODES, DEFAULT_MAX_BOUNDARY_WAYS
from .model import OsmFaceError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DOMAIN = 2  # load faults, bad staging state, export failures


def _print_json(payload) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _cmd_load(args) -> int:
    summary = api.load_database(args.db, args.files, force=args.force)
    print(
        "load: {counts} merged_duplicates={merged} faults={faults} "
        "staging_state={state}".format(
            counts=summary["counts"],
            merged=summary["merged_duplicates"],
            faults=summary["faults"],
            state=summary["staging_state"],
        )
    )
    if summary["faults"]:
        print("load: input faults recorded; staging marked FAILED", file=sys.stderr)
        return EXIT_DOMAIN
    return EXIT_OK


def _cmd_assemble(args) -> int:
    summary = api.assemble_database(
        args.db,
        out_dir=args.out,
        force=args.force,
        max_boundary_ways=args.max_boundary_ways,
        max_boundary_nodes=args.max_boundary_nodes,
    )
    counts = summary["status_counts"]
    print(
        "assemble: relations={relations} complete={complete} "
        "incomplete={incomplete} topology_conflict={conflict}".format(
            relations=summary["relations"],
            complete=counts["complete"],
            incomplete=counts["incomplete"],
            conflict=counts["topology_conflict"],
        )
    )
    if "package" in summary:
        pkg = summary["package"]
        print(f"package: {pkg['path']} state={pkg['state']} counts={pkg['counts']}")
    return EXIT_OK


def _cmd_inspect(args) -> int:
    if args.package:
        _print_json(api.inspect_package(args.package))
        return EXIT_OK
    if not args.db:
        print("inspect: a staging database path is required", file=sys.stderr)
        return EXIT_ERROR
    info = api.inspect_database(
        args.db, relation_id=args.relation, faults=args.faults,
        revision=args.revision,
    )
    if args.json:
        _print_json(info)
        return EXIT_OK
    _print_human(info, args)
    return EXIT_OK


def _cmd_enable_history(args) -> int:
    summary = api.enable_history(args.db)
    rev = summary.get("revision", 0)
    note = " (idempotent, already enabled)" if summary.get("idempotent") else ""
    print(f"enable-history: revision={rev}{note}")
    if "relations" in summary:
        print(f"  relations={summary['relations']} limits={summary['limits']}")
    return EXIT_OK


def _cmd_apply(args) -> int:
    receipt = api.apply_changes(args.db, args.changes)
    if args.json:
        _print_json(receipt)
        return EXIT_OK
    print(
        "apply: batch_id={bid} {base}->{rev} sha256={sha} changes={nch}".format(
            bid=receipt["batch_id"],
            base=receipt["base_revision"],
            rev=receipt["revision"],
            sha=receipt["input_sha256"][:12],
            nch=len(receipt["changes"]),
        )
    )
    print(f"  affected/recomputed relations: {receipt['recomputed_relations']}")
    for rel in receipt["affected_relations"]:
        print(
            "  relation {rid}: {before} -> {after} geometry_changed={geo}"
            " source_changed={src} basis={basis}".format(
                rid=rel["relation"],
                before=rel["status_before"],
                after=rel["status_after"],
                geo=rel["geometry_changed"],
                src=rel["source_changed"],
                basis=rel["basis"],
            )
        )
    return EXIT_OK


def _cmd_receipt(args) -> int:
    _print_json(api.get_receipt(args.db, args.batch_id))
    return EXIT_OK


def _cmd_export_revision(args) -> int:
    revision = args.revision
    manifest = api.export_revision_package(
        args.db, args.out, revision=revision, force=args.force
    )
    print(
        f"export-revision: {args.out} state={manifest['state']}"
        f" revision={manifest['revision']} counts={manifest['counts']}"
    )
    return EXIT_OK


def _print_human(info, args) -> None:
    print(f"staging_state: {info['staging_state']}")
    if info.get("history_enabled"):
        print(
            f"history: revision={info['revision']} head={info['head_revision']}"
            f" identity={info['revision_identity']}"
        )
        print(f"counts: {info['counts']}  absent_relations: {info['absent_relations']}")
        print(f"status_counts: {info['status_counts']}")
        print(f"batches: {[b['batch_id'] for b in info['batches']]}")
    else:
        print(f"counts: {info['counts']}  merged_duplicates: {info['merged_duplicates']}"
              f"  faults: {info['fault_count']}")
        if info.get("last_assemble"):
            la = info["last_assemble"]
            print(f"last_assemble: {la['assembled_at']} limits={la['limits']}"
                  f" fetched={la['fetched']}")
        print(f"status_counts: {info['status_counts']}")
        if info.get("last_output"):
            lo = info["last_output"]
            print(f"last_output: {lo['path']} state={lo['state']}")
    if args.faults:
        for f in info.get("faults", []):
            print(f"  fault {f['kind']} {f['file']}:{f['line']}: {f['message']}")
    rel = info.get("relation")
    if rel is not None:
        _print_relation(rel)


def _print_relation(rel) -> None:
    rev = rel.get("revision")
    prefix = f"relation {rel['relation']}" + (f" @rev{rev}" if rev is not None else "")
    print(f"{prefix}: status={rel['status']} object_version={rel.get('object_version')}")
    if rel.get("reason"):
        print(f"  reason: {json.dumps(rel['reason'], sort_keys=True)}")
    for face in rel.get("faces") or []:
        print(f"  face {face['face_index']}:")
        for ring in face["rings"]:
            kind = "hole " if ring["hole"] else "shell"
            print(f"    ring {kind} role={ring['role']} nodes={ring['nodes']}")
            for seg in ring["segments"]:
                print(
                    "      way={way}(v{wv}) path={path} reversed={rev} nodes={nodes}"
                    " (definition {definition}, node_versions {nv})".format(
                        way=seg["way"],
                        wv=seg.get("way_version", "?"),
                        path=seg["path"],
                        rev=seg["reversed"],
                        nodes=seg["nodes"],
                        definition=seg["way_definition_nodes"],
                        nv=seg.get("node_versions"),
                    )
                )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="osmface",
        description="Offline OSM-style relation-to-polygon assembler "
        "(SQLite staging + Shapely geometry, no network).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_load = sub.add_parser("load", help="load JSONL input into a staging database")
    p_load.add_argument("db", help="staging SQLite database path")
    p_load.add_argument("files", nargs="+", help="input JSONL files")
    p_load.add_argument("--force", action="store_true",
                        help="rebuild the database if it exists")
    p_load.set_defaults(func=_cmd_load)

    p_asm = sub.add_parser("assemble", help="assemble all relations into faces")
    p_asm.add_argument("db", help="staging SQLite database path")
    p_asm.add_argument("--out", help="export a warehouse package to this directory")
    p_asm.add_argument("--force", action="store_true",
                       help="replace an existing output package")
    p_asm.add_argument("--max-boundary-ways", type=int,
                       default=DEFAULT_MAX_BOUNDARY_WAYS,
                       help="per-relation limit on expanded way occurrences")
    p_asm.add_argument("--max-boundary-nodes", type=int,
                       default=DEFAULT_MAX_BOUNDARY_NODES,
                       help="per-relation limit on expanded node references")
    p_asm.set_defaults(func=_cmd_assemble)

    p_insp = sub.add_parser("inspect", help="inspect staging state and results")
    p_insp.add_argument("db", nargs="?", help="staging SQLite database path")
    p_insp.add_argument("--relation", type=int, help="show one relation in detail")
    p_insp.add_argument("--faults", action="store_true", help="list load faults")
    p_insp.add_argument("--package", help="inspect an exported package directory")
    p_insp.add_argument("--revision", type=int, default=None,
                        help="pin the inspection to this revision (history dbs)")
    p_insp.add_argument("--json", action="store_true", help="machine-readable output")
    p_insp.set_defaults(func=_cmd_inspect)

    p_en = sub.add_parser(
        "enable-history",
        help="enable revisioning on a loaded+assembled database (revision 0)",
    )
    p_en.add_argument("db", help="staging SQLite database path")
    p_en.set_defaults(func=_cmd_enable_history)

    p_ap = sub.add_parser("apply", help="apply a UTF-8 JSONL change batch")
    p_ap.add_argument("db", help="history-enabled SQLite database path")
    p_ap.add_argument("changes", help="JSONL change file (header + put/delete rows)")
    p_ap.add_argument("--json", action="store_true", help="print the full receipt")
    p_ap.set_defaults(func=_cmd_apply)

    p_rc = sub.add_parser("receipt", help="read one committed batch receipt")
    p_rc.add_argument("db", help="history-enabled SQLite database path")
    p_rc.add_argument("batch_id", help="batch_id of the received batch")
    p_rc.set_defaults(func=_cmd_receipt)

    p_ex = sub.add_parser(
        "export-revision", help="export an immutable package for one revision"
    )
    p_ex.add_argument("db", help="history-enabled SQLite database path")
    p_ex.add_argument("out", help="output package directory")
    p_ex.add_argument("--revision", type=int, default=None,
                      help="revision to export (default: head)")
    p_ex.add_argument("--force", action="store_true",
                      help="replace an existing output package")
    p_ex.set_defaults(func=_cmd_export_revision)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except OsmFaceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DOMAIN
    except OSError as exc:
        print(f"io error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

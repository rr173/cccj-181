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
    if summary.get("spatial_index"):
        si = summary["spatial_index"]
        print(
            f"  spatial-index: rtree_rows={si['rtree_rows']}"
            f" bypass_rows={si['bypass_rows']}"
        )
    return EXIT_OK


def _print_spatial_result(result) -> None:
    print(
        "{kind}: revision={rev} (head={head}) candidates={cand}"
        " (rtree={rt}, bypass={bp}) exact_checks={chk} hits={hits}".format(
            kind=result["query"],
            rev=result["revision"],
            head=result["head_revision"],
            cand=result["index"]["candidate_faces"],
            rt=result["index"]["rtree_candidates"],
            bp=result["index"]["bypass_candidates"],
            chk=result["exact_checks"],
            hits=result["hit_count"],
        )
    )
    print(f"  params: {json.dumps(result['params'], sort_keys=True)}")
    for hit in result["hits"]:
        print(f"  relation {hit['relation_id']}: faces={hit['faces']}")


def _cmd_query_point(args) -> int:
    result = api.query_faces_at_point(
        args.db, args.x, args.y, revision=args.revision
    )
    if args.json:
        _print_json(result)
        return EXIT_OK
    _print_spatial_result(result)
    return EXIT_OK


def _cmd_query_range(args) -> int:
    result = api.query_faces_in_range(
        args.db, args.minx, args.miny, args.maxx, args.maxy,
        revision=args.revision,
    )
    if args.json:
        _print_json(result)
        return EXIT_OK
    _print_spatial_result(result)
    return EXIT_OK


def _cmd_spatial_index(args) -> int:
    _print_json(api.spatial_index_info(args.db))
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


def _cmd_source_id(args) -> int:
    _print_json(api.source_identity(args.db))
    return EXIT_OK


def _cmd_checkout(args) -> int:
    summary = api.checkout_workcopy(
        args.db, args.workcopy, revision=args.revision, force=args.force
    )
    print(
        "checkout: {wc} source={src} base_revision={rev} entities={n}"
        " baseline={sha}".format(
            wc=summary["workcopy"],
            src=summary["source_id"][:12],
            rev=summary["base_revision"],
            n=summary["entities"],
            sha=summary["baseline_sha256"][:12],
        )
    )
    return EXIT_OK


def _cmd_wc_info(args) -> int:
    _print_json(api.workcopy_info(args.workcopy))
    return EXIT_OK


def _cmd_wc_edit(args) -> int:
    receipt = api.workcopy_edit(args.workcopy, args.edits)
    if args.json:
        _print_json(receipt)
        return EXIT_OK
    print(
        "wc-edit: edit_id={eid} {base}->{head} changes={n}".format(
            eid=receipt["edit_id"],
            base=receipt["base_edit"],
            head=receipt["edit_head"],
            n=len(receipt["changes"]),
        )
    )
    return EXIT_OK


def _cmd_wc_preview(args) -> int:
    preview = api.workcopy_preview(args.workcopy, relation_id=args.relation)
    if args.json:
        _print_json(preview)
        return EXIT_OK
    counts = preview["status_counts"]
    print(
        "wc-preview: base_revision={rev} edit_head={head} relations={n}"
        " complete={c} incomplete={i} topology_conflict={t}".format(
            rev=preview["base_revision"],
            head=preview["edit_head"],
            n=preview["relations"],
            c=counts["complete"],
            i=counts["incomplete"],
            t=counts["topology_conflict"],
        )
    )
    rel = preview.get("relation")
    if rel is not None:
        print(f"  relation {rel['relation']}: status={rel['status']}")
        if rel.get("reason"):
            print(f"    reason: {json.dumps(rel['reason'], sort_keys=True)}")
        for i, face in enumerate(rel.get("faces") or []):
            print(f"    face {i}:")
            for ring in face["rings"]:
                kind = "hole " if ring["hole"] else "shell"
                print(f"      ring {kind} role={ring['role']}"
                      f" nodes={ring['nodes']}")
                for seg in ring["segments"]:
                    print(
                        "        way={way} path={path} reversed={rev}"
                        " nodes={nodes}".format(
                            way=seg["way"], path=seg["path"],
                            rev=seg["reversed"], nodes=seg["nodes"],
                        )
                    )
    return EXIT_OK


def _cmd_wc_export(args) -> int:
    manifest = api.workcopy_export(args.workcopy, args.package,
                                   force=args.force)
    print(
        "wc-export: {pkg} state={state} package_id={pid}".format(
            pkg=args.package,
            state=manifest["state"],
            pid=manifest["package_id"][:12],
        )
    )
    return EXIT_OK


def _cmd_merge_plan(args) -> int:
    plan = api.create_merge_plan(args.db, args.package)
    if args.json:
        _print_json(plan)
        return EXIT_OK
    _print_plan_summary(plan)
    return EXIT_OK


def _print_plan_summary(plan) -> None:
    auto_keep = [d for d in plan["decisions"] if d["decision"] == "keep_main"]
    auto_field = [d for d in plan["decisions"] if d["decision"] == "take_field"]
    auto_same = [d for d in plan["decisions"] if d["decision"] == "same"]
    conflicts = [d for d in plan["decisions"] if d["decision"] == "conflict"]
    print(
        "merge-plan: {pid} status={st} B={b} H={h} package={pkg}".format(
            pid=plan["plan_id"], st=plan["status"],
            b=plan["base_revision"], h=plan["head_revision"],
            pkg=plan["package_id"][:12],
        )
    )
    print(
        "  keep_main={k} take_field={f} same={s} conflicts={c}"
        " new_local_objects={n} unresolved={u}".format(
            k=len(auto_keep), f=len(auto_field), s=len(auto_same),
            c=len(conflicts), n=len(plan["locals"]),
            u=len(plan["unresolved_conflicts"]),
        )
    )
    for d in conflicts:
        print("  conflict: {t} {i}".format(t=d["type"], i=d["id"]))
    for g in plan["reference_gaps"][:10]:
        frm = g["from"]
        miss = g["missing"]
        print(
            "  reference gap: {ft} {fi} -> missing {mt} {mi}".format(
                ft=frm["type"], fi=frm.get("id") or frm.get("local_id"),
                mt=miss["type"], mi=miss.get("id") or miss.get("local_id"),
            )
        )


def _cmd_merge_show(args) -> int:
    plan = api.get_merge_plan(args.db, args.plan_id)
    if args.json:
        _print_json(plan)
        return EXIT_OK
    _print_plan_summary(plan)
    for d in plan["decisions"]:
        ruling = d.get("ruling")
        line = "  {t} {i}: {dec}".format(t=d["type"], i=d["id"],
                                         dec=d["decision"])
        if ruling:
            line += " ruling={ch}".format(ch=ruling["choice"])
        if d.get("final"):
            line += " final={st}".format(st=d["final"]["state"])
        print(line)
    return EXIT_OK


def _cmd_merge_rule(args) -> int:
    with open(args.rulings, encoding="utf-8") as fh:
        rulings = json.load(fh)
    plan = api.save_merge_rulings(args.db, args.plan_id, rulings)
    if args.json:
        _print_json(plan)
        return EXIT_OK
    print(
        "merge-rule: {pid} saved={n} unresolved={u}".format(
            pid=plan["plan_id"],
            n=plan["rulings_saved"],
            u=len(plan["unresolved_conflicts"]),
        )
    )
    for g in plan["reference_gaps"][:10]:
        frm, miss = g["from"], g["missing"]
        print(
            "  reference gap: {ft} {fi} -> missing {mt} {mi}".format(
                ft=frm["type"], fi=frm.get("id") or frm.get("local_id"),
                mt=miss["type"], mi=miss.get("id") or miss.get("local_id"),
            )
        )
    return EXIT_OK


def _cmd_merge_preview(args) -> int:
    preview = api.merge_preview(args.db, args.plan_id)
    if args.json:
        _print_json(preview)
        return EXIT_OK
    if not preview["complete"]:
        print(
            "merge-preview: INCOMPLETE - {n} unresolved conflicts".format(
                n=len(preview["unresolved_conflicts"])
            )
        )
        for c in preview["unresolved_conflicts"]:
            print("  unresolved: {t} {i}".format(t=c["type"], i=c["id"]))
        return EXIT_DOMAIN
    print(
        "merge-preview: complete affected={affected}".format(
            affected=preview["affected_relations"],
        )
    )
    for rid, rel in preview["relations"].items():
        print(f"  relation {rid}: {rel['status']}")
    for g in preview["reference_gaps"][:10]:
        frm, miss = g["from"], g["missing"]
        print(
            "  reference gap: {ft} {fi} -> missing {mt} {mi}".format(
                ft=frm["type"], fi=frm.get("id") or frm.get("local_id"),
                mt=miss["type"], mi=miss.get("id") or miss.get("local_id"),
            )
        )
    return EXIT_OK


def _cmd_merge_confirm(args) -> int:
    receipt = api.confirm_merge(args.db, args.plan_id, args.merge_id)
    if args.json:
        _print_json(receipt)
        return EXIT_OK
    print(
        "merge-confirm: merge_id={mid} {h}->{r} changes={n}"
        " allocation={alloc}".format(
            mid=receipt["merge_id"],
            h=receipt["head_revision"],
            r=receipt["result_revision"],
            n=len(receipt["changes"]),
            alloc=receipt["allocation"],
        )
    )
    return EXIT_OK


def _cmd_merge_receipt(args) -> int:
    _print_json(api.get_merge_receipt(args.db, args.merge_id))
    return EXIT_OK


def _cmd_merge_list(args) -> int:
    _print_json(api.list_merge_plans(args.db))
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

    p_qp = sub.add_parser(
        "query-point",
        help="list complete faces covering a planar point at one revision",
    )
    p_qp.add_argument("db", help="history-enabled SQLite database path")
    p_qp.add_argument("x", type=float, help="planar x coordinate (finite)")
    p_qp.add_argument("y", type=float, help="planar y coordinate (finite)")
    p_qp.add_argument("--revision", type=int, default=None,
                      help="revision to query (default: head pinned at start)")
    p_qp.add_argument("--json", action="store_true",
                      help="machine-readable output")
    p_qp.set_defaults(func=_cmd_query_point)

    p_qr = sub.add_parser(
        "query-range",
        help="list complete faces intersecting a planar rectangle",
    )
    p_qr.add_argument("db", help="history-enabled SQLite database path")
    p_qr.add_argument("minx", type=float, help="minimum x (finite)")
    p_qr.add_argument("miny", type=float, help="minimum y (finite)")
    p_qr.add_argument("maxx", type=float, help="maximum x (finite, >= minx)")
    p_qr.add_argument("maxy", type=float, help="maximum y (finite, >= miny)")
    p_qr.add_argument("--revision", type=int, default=None,
                      help="revision to query (default: head pinned at start)")
    p_qr.add_argument("--json", action="store_true",
                      help="machine-readable output")
    p_qr.set_defaults(func=_cmd_query_range)

    p_si = sub.add_parser(
        "spatial-index",
        help="show per-revision spatial index build state",
    )
    p_si.add_argument("db", help="history-enabled SQLite database path")
    p_si.set_defaults(func=_cmd_spatial_index)

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

    p_sid = sub.add_parser(
        "source-id", help="show the warehouse's stable source identity"
    )
    p_sid.add_argument("db", help="history-enabled SQLite database path")
    p_sid.set_defaults(func=_cmd_source_id)

    p_co = sub.add_parser(
        "checkout", help="create a self-contained offline work copy"
    )
    p_co.add_argument("db", help="history-enabled SQLite database path")
    p_co.add_argument("workcopy", help="work copy SQLite path to create")
    p_co.add_argument("--revision", type=int, default=None,
                      help="revision to pin (default: current head)")
    p_co.add_argument("--force", action="store_true",
                      help="replace an existing work copy file")
    p_co.set_defaults(func=_cmd_checkout)

    p_wi = sub.add_parser("wc-info", help="show work copy provenance/state")
    p_wi.add_argument("workcopy", help="work copy SQLite path")
    p_wi.set_defaults(func=_cmd_wc_info)

    p_we = sub.add_parser("wc-edit", help="apply an offline edit batch")
    p_we.add_argument("workcopy", help="work copy SQLite path")
    p_we.add_argument("edits", help="JSONL edit file (header + put/delete)")
    p_we.add_argument("--json", action="store_true",
                      help="print the full edit receipt")
    p_we.set_defaults(func=_cmd_wc_edit)

    p_wp = sub.add_parser(
        "wc-preview", help="preview the work copy with the assembly rules"
    )
    p_wp.add_argument("workcopy", help="work copy SQLite path")
    p_wp.add_argument("--relation", help="show one relation (id or local name)")
    p_wp.add_argument("--json", action="store_true",
                      help="machine-readable output")
    p_wp.set_defaults(func=_cmd_wc_preview)

    p_wx = sub.add_parser(
        "wc-export", help="export the copy head as a transport package"
    )
    p_wx.add_argument("workcopy", help="work copy SQLite path")
    p_wx.add_argument("package", help="output package directory")
    p_wx.add_argument("--force", action="store_true",
                      help="replace an existing package directory")
    p_wx.set_defaults(func=_cmd_wc_export)

    p_mp = sub.add_parser(
        "merge-plan", help="validate a package and persist a merge plan"
    )
    p_mp.add_argument("db", help="history-enabled SQLite database path")
    p_mp.add_argument("package", help="transport package directory")
    p_mp.add_argument("--json", action="store_true",
                      help="machine-readable output")
    p_mp.set_defaults(func=_cmd_merge_plan)

    p_ms = sub.add_parser("merge-show", help="show a persisted merge plan")
    p_ms.add_argument("db", help="history-enabled SQLite database path")
    p_ms.add_argument("plan_id", help="plan identity")
    p_ms.add_argument("--json", action="store_true",
                      help="machine-readable output")
    p_ms.set_defaults(func=_cmd_merge_show)

    p_ml = sub.add_parser("merge-list", help="list merge plans")
    p_ml.add_argument("db", help="history-enabled SQLite database path")
    p_ml.set_defaults(func=_cmd_merge_list)

    p_mr = sub.add_parser(
        "merge-rule", help="save conflict rulings / custom overrides"
    )
    p_mr.add_argument("db", help="history-enabled SQLite database path")
    p_mr.add_argument("plan_id", help="plan identity")
    p_mr.add_argument("rulings", help="JSON file with the rulings")
    p_mr.add_argument("--json", action="store_true",
                      help="machine-readable output")
    p_mr.set_defaults(func=_cmd_merge_rule)

    p_mv = sub.add_parser(
        "merge-preview", help="preview the ruled candidate map"
    )
    p_mv.add_argument("db", help="history-enabled SQLite database path")
    p_mv.add_argument("plan_id", help="plan identity")
    p_mv.add_argument("--json", action="store_true",
                      help="machine-readable output")
    p_mv.set_defaults(func=_cmd_merge_preview)

    p_mc = sub.add_parser(
        "merge-confirm", help="land the ruled plan as one revision"
    )
    p_mc.add_argument("db", help="history-enabled SQLite database path")
    p_mc.add_argument("plan_id", help="plan identity")
    p_mc.add_argument("--merge-id", required=True,
                      help="stable merge identity for idempotent retries")
    p_mc.add_argument("--json", action="store_true",
                      help="print the full merge receipt")
    p_mc.set_defaults(func=_cmd_merge_confirm)

    p_mrc = sub.add_parser("merge-receipt", help="read one merge receipt")
    p_mrc.add_argument("db", help="history-enabled SQLite database path")
    p_mrc.add_argument("merge_id", help="merge identity of the receipt")
    p_mrc.set_defaults(func=_cmd_merge_receipt)
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

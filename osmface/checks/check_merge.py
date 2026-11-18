#!/usr/bin/env python3
"""Independent acceptance for the offline-merge feature.

This script deliberately does NOT call any production three-way merge
function.  Expectations come from two independent sources:

* a hand-written B/L/H table with a tiny local decision procedure
  (business definitions only: alive/deleted/never-existed plus the whole
  ordered payload), checked against the plan the warehouse persists;
* the ORDINARY full assembler: candidate entity sets are materialized by
  hand into a fresh staging database and assembled from scratch, and the
  resulting statuses/areas are compared with the merge preview and with
  the landed warehouse revision.

Run:  python3 checks/check_merge.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import osmface  # noqa: E402
from shapely.geometry import shape  # noqa: E402
from history_util import (  # noqa: E402
    change_file,
    delete,
    node,
    put,
    rel,
    way,
    wm,
    write_jsonl,
)
from merge_util import edit_file, local, lnode, lrel, lway, lm  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    status = "ok" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# independent three-way decision procedure (business definitions only)
# ---------------------------------------------------------------------------

def state_of(record):
    """record: None (never existed) | ("deleted",) | ("alive", payload)."""
    if record is None:
        return ("absent",)
    if record[0] == "deleted":
        return ("deleted",)
    return ("alive", json.dumps(record[1], sort_keys=True))


def expect_decision(b, l, h):
    sb, sl, sh = state_of(b), state_of(l), state_of(h)
    if sl == sb:
        return "keep_main"
    if sh == sb:
        return "take_field"
    if sl == sh:
        return "same"
    return "conflict"


def scenario_three_way_table(tmp):
    """Hand-written B/L/H rows; the production plan must agree object by
    object with the independent table."""
    print("scenario: hand-written B/L/H decision table")
    db = str(tmp / "main.db")
    # baseline world: nodes 1..4 (rectangle), way 10, relation 100,
    # way 20 + relation 200 (second rectangle), node 30 (unused)
    records = [
        node(1, 0, 0), node(2, 10, 0), node(3, 10, 10), node(4, 0, 10),
        way(10, [1, 2, 3, 4, 1]),
        rel(100, [wm(10)]),
        node(5, 20, 20), node(6, 25, 20), node(7, 25, 25), node(8, 20, 25),
        way(20, [5, 6, 7, 8, 5]),
        rel(200, [wm(20)]),
        node(30, 99, 99),
    ]
    src = write_jsonl(str(tmp / "world.jsonl"), records)
    osmface.load_database(db, [src])
    osmface.assemble_database(db)
    osmface.enable_history(db)

    # -- main side (H): touch objects 1, 2, 5, 20, 30, 200 --------------
    osmface.apply_changes(db, change_file(tmp, "h1.jsonl", "h1", 0, [
        put(1, node(1, 1, 0)),          # 1: main moves
        put(1, node(2, 10, 0)),         # 2: same-value put (version only)
        delete(1, "node", 5),           # 5: main deletes
        put(1, way(20, [5, 6, 7, 8, 5])),  # 20: same-value put
        delete(1, "node", 30),          # 30: main deletes (field restores?)
        put(1, rel(200, [wm(20), wm(20)])),  # 200: duplicate member
    ]))
    # -- field side (L): checkout at rev 0, then edits -------------------
    copy = str(tmp / "copy.db")
    osmface.checkout_workcopy(db, copy, revision=0)
    osmface.workcopy_edit(copy, edit_file(tmp, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1, "entity": node(1, 2, 0)},
        # 1: field moves elsewhere -> conflict with main
        {"op": "put", "expected_version": 1, "entity": node(3, 10, 11)},
        # 3: field only -> take_field
        {"op": "delete", "expected_version": 1, "type": "node", "id": 4},
        # 4: field deletes -> take_field delete
        {"op": "delete", "expected_version": 1, "type": "node", "id": 6},
        # 6: field deletes, main untouched -> take_field
        {"op": "put", "expected_version": 1, "entity": node(30, 99, 99)},
        # 30: field same-value put, main deleted -> conflict
        {"op": "put", "expected_version": 1,
         "entity": rel(100, [wm(10), wm(10)])},
        # 100: field duplicates member; main untouched -> take_field
    ]))
    pkg = str(tmp / "pkg")
    osmface.workcopy_export(copy, pkg)
    plan = osmface.create_merge_plan(db, pkg)
    by_key = {(d["type"], d["id"]): d["decision"] for d in plan["decisions"]}

    table = {
        # object: (B, L, H) with None = never existed, ("deleted",), ("alive", payload)
        ("node", 1): (("alive", [0, 0]), ("alive", [2, 0]), ("alive", [1, 0])),
        ("node", 2): (("alive", [10, 0]), None, ("alive", [10, 0])),
        ("node", 3): (("alive", [10, 10]), ("alive", [10, 11]), None),
        ("node", 4): (("alive", [0, 10]), ("deleted",), None),
        ("node", 5): (("alive", [20, 20]), None, ("deleted",)),
        ("node", 6): (("alive", [25, 20]), ("deleted",), None),
        ("node", 30): (("alive", [99, 99]), ("alive", [99, 99]), ("deleted",)),
        ("way", 10): (("alive", [1, 2, 3, 4, 1]), None, None),
        ("way", 20): (("alive", [5, 6, 7, 8, 5]), None, ("alive", [5, 6, 7, 8, 5])),
        ("relation", 100): (("alive", [wm(10)]), ("alive", [wm(10), wm(10)]), None),
        ("relation", 200): (("alive", [wm(20)]), None, ("alive", [wm(20), wm(20)])),
    }
    # L=None / H=None means "that side kept the baseline": substitute B.
    # Objects neither side redefined produce no decision row at all.
    expected = {}
    for key, (b, l, h) in table.items():
        l_eff = b if l is None else l
        h_eff = b if h is None else h
        if state_of(l_eff) == state_of(b) and state_of(h_eff) == state_of(b):
            expected[key] = None  # no decision expected
        else:
            expected[key] = expect_decision(b, l_eff, h_eff)
    for key, want in sorted(expected.items()):
        got = by_key.get(key)
        check(f"{key}: expect {want}", got == want, f"got {got}")
    # objects neither side touched must not appear as decisions at all
    check("untouched objects absent from decisions",
          all(k in expected for k in by_key),
          f"unexpected: {set(by_key) - set(expected)}")
    # same-value puts (node 2, way 20) never became conflicts
    check("same-value puts are not conflicts",
          by_key.get(("node", 2)) in (None, "keep_main")
          and by_key.get(("way", 20)) in (None, "keep_main"))

    # rule every conflict for the field side, confirm, and verify the
    # landed warehouse object by object against the independent table
    rulings = [
        {"type": kind, "id": eid, "choice": "field"}
        for (kind, eid), want in expected.items() if want == "conflict"
    ]
    check("conflicts need rulings before confirm",
          len(rulings) == 1 and rulings[0]["id"] == 1,
          f"rulings: {rulings}")
    osmface.save_merge_rulings(db, plan["plan_id"], rulings)
    receipt = osmface.confirm_merge(db, plan["plan_id"], "m-table")
    rev = receipt["result_revision"]

    def expected_final(key):
        b, l, h = table[key]
        l_eff = b if l is None else l
        h_eff = b if h is None else h
        decision = expected[key]
        if decision in (None, "keep_main", "same"):
            return h_eff
        return l_eff  # take_field or conflict ruled "field"

    import sqlite3
    raw = sqlite3.connect(db)
    raw.row_factory = sqlite3.Row
    for key in sorted(table):
        kind, eid = key
        row = raw.execute(
            "SELECT deleted, payload FROM entity_versions"
            " WHERE type = ? AND id = ?", (kind, eid)).fetchone()
        want = expected_final(key)
        if want[0] == "deleted":
            check(f"final {key}: deleted", row["deleted"] == 1)
        else:
            got = json.loads(row["payload"])
            want_payload = want[1]
            if kind == "relation":
                want_payload = [
                    {"type": m["type"], "ref": m["ref"], "role": m["role"]}
                    for m in want_payload
                ]
            check(f"final {key}: {want[0]}",
                  row["deleted"] == 0 and got == want_payload,
                  f"got {got}")
    raw.close()
    check("exactly one merge revision", rev == 2)
    return db, plan


def scenario_duplicate_members_not_set_compared(tmp):
    print("scenario: ordered members with duplicates survive comparison")
    db = str(tmp / "main.db")
    records = [
        node(1, 0, 0), node(2, 4, 0), node(3, 4, 4), node(4, 0, 4),
        way(10, [1, 2, 3, 4, 1]),
        node(5, 10, 10), node(6, 14, 10), node(7, 14, 14), node(8, 10, 14),
        way(20, [5, 6, 7, 8, 5]),
        rel(300, [wm(10), wm(20)]),
    ]
    src = write_jsonl(str(tmp / "world.jsonl"), records)
    osmface.load_database(db, [src])
    osmface.assemble_database(db)
    osmface.enable_history(db)
    copy = str(tmp / "copy.db")
    osmface.checkout_workcopy(db, copy)
    # field: [10, 20, 20]; main: [20, 10] — sets agree, sequences don't
    osmface.workcopy_edit(copy, edit_file(tmp, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1,
         "entity": rel(300, [wm(10), wm(20), wm(20)])},
    ]))
    osmface.apply_changes(db, change_file(tmp, "h.jsonl", "h", 0, [
        put(1, rel(300, [wm(20), wm(10)])),
    ]))
    osmface.workcopy_export(copy, str(tmp / "pkg"))
    plan = osmface.create_merge_plan(db, str(tmp / "pkg"))
    by_key = {(d["type"], d["id"]): d for d in plan["decisions"]}
    check("duplicate/reordered members conflict",
          by_key[("relation", 300)]["decision"] == "conflict")
    osmface.save_merge_rulings(db, plan["plan_id"], [
        {"type": "relation", "id": 300, "choice": "field"},
    ])
    receipt = osmface.confirm_merge(db, plan["plan_id"], "m-dup")
    detail = osmface.inspect_database(
        db, relation_id=300, revision=receipt["result_revision"])["relation"]
    check("landed members keep order and duplicates",
          [m["ref"] for m in detail["members"]] == [10, 20, 20])


def scenario_two_copies_and_id_allocation(tmp):
    print("scenario: two copies, reused local names, allocation & areas")
    db = str(tmp / "main.db")
    records = [
        node(1, 0, 0), node(2, 10, 0), node(3, 10, 10), node(4, 0, 10),
        way(10, [1, 2, 3, 4, 1]),
        rel(100, [wm(10)]),
        node(-9, 500, 500),   # negative ids are ordinary warehouse ids
        node(40, 3, 3),       # will be deleted: tombstone occupies 40
        node(41, 3, 3),       # same coordinates as the field's new node
    ]
    src = write_jsonl(str(tmp / "world.jsonl"), records)
    osmface.load_database(db, [src])
    osmface.assemble_database(db)
    osmface.enable_history(db)
    osmface.apply_changes(db, change_file(tmp, "del.jsonl", "del", 0,
                                          [delete(1, "node", 40)]))

    def make_copy(name, x):
        copy = str(tmp / name)
        osmface.checkout_workcopy(db, copy, revision=0)
        osmface.workcopy_edit(copy, edit_file(tmp, f"e-{name}.jsonl", "e", 0, [
            {"op": "put", "expected_version": 0, "entity": lnode("n1", x, 0)},
            {"op": "put", "expected_version": 0, "entity": lnode("n2", x + 2, 0)},
            {"op": "put", "expected_version": 0, "entity": lnode("n3", x + 2, 2)},
            {"op": "put", "expected_version": 0, "entity": lnode("n4", x, 2)},
            # same coordinates as main's live node 41: must still get a
            # brand-new identity (identity never merges by content)
            {"op": "put", "expected_version": 0, "entity": lnode("n9", 3, 3)},
            {"op": "put", "expected_version": 0,
             "entity": lway("w1", [local("n1"), local("n2"), local("n3"),
                                   local("n4"), local("n1")])},
            {"op": "put", "expected_version": 0,
             "entity": lrel("r1", [lm("w1")])},
        ]))
        pkg = str(tmp / f"pkg-{name}")
        osmface.workcopy_export(copy, pkg)
        return copy, pkg

    copy_a, pkg_a = make_copy("a.db", 100)
    copy_b, pkg_b = make_copy("b.db", 200)
    # delete the original copy directories before receiving anything
    shutil.rmtree(tmp / "a.db" if os.path.isdir(copy_a) else copy_a,
                  ignore_errors=True)
    if os.path.isfile(copy_a):
        os.remove(copy_a)
    if os.path.isfile(copy_b):
        os.remove(copy_b)
    plan_a = osmface.create_merge_plan(db, pkg_a)
    receipt_a = osmface.confirm_merge(db, plan_a["plan_id"], "merge-a")
    plan_b = osmface.create_merge_plan(db, pkg_b)
    receipt_b = osmface.confirm_merge(db, plan_b["plan_id"], "merge-b")
    na = receipt_a["allocation"]["node"]
    nb = receipt_b["allocation"]["node"]
    check("same local names get different permanent ids",
          set(na.values()).isdisjoint(nb.values()))
    check("allocations avoid negatives, tombstones and live ids",
          all(v > 41 for v in list(na.values()) + list(nb.values())))
    check("one permanent identity per local object",
          receipt_a["allocation"]["way"] == {"w1": 11}
          and receipt_a["allocation"]["relation"] == {"r1": 101})
    # per-level references point at permanent ids
    rid_a = receipt_a["allocation"]["relation"]["r1"]
    wid_a = receipt_a["allocation"]["way"]["w1"]
    detail = osmface.inspect_database(db, relation_id=rid_a)["relation"]
    check("relation references the materialized way",
          detail["members"] == [{"type": "way", "ref": wid_a, "role": "outer"}])
    seg_nodes = detail["faces"][0]["rings"][0]["segments"][0]["way_definition_nodes"]
    check("way references the materialized nodes",
          seg_nodes == [na["n1"], na["n2"], na["n3"], na["n4"], na["n1"]])
    area_a = shape(detail["faces"][0]["geometry"]).area
    check("final area of copy A's face", area_a == 4.0, f"got {area_a}")
    rid_b = receipt_b["allocation"]["relation"]["r1"]
    area_b = shape(osmface.inspect_database(
        db, relation_id=rid_b)["relation"]["faces"][0]["geometry"]).area
    check("final area of copy B's face", area_b == 4.0, f"got {area_b}")
    # tombstone 40 and negative id -9 were not handed out again
    import sqlite3
    raw = sqlite3.connect(db)
    rows = dict(raw.execute(
        "SELECT id, payload FROM entity_versions WHERE type='node'"))
    raw.close()
    check("tombstone and negative ids stay occupied",
          40 in rows and -9 in rows)
    # the field node at (3,3) did not merge with main's node 41 at (3,3)
    n9_a = receipt_a["allocation"]["node"]["n9"]
    check("same coordinates still get a fresh identity",
          n9_a != 41 and json.loads(rows[n9_a]) == [3, 3]
          and json.loads(rows[41]) == [3, 3])
    return db


def scenario_cross_gap_and_repair(tmp):
    print("scenario: main deletes a node the field still uses")
    db = str(tmp / "main.db")
    records = [
        node(1, 0, 0), node(2, 10, 0), node(3, 10, 10), node(4, 0, 10),
        way(10, [1, 2, 3, 4, 1]),
        rel(100, [wm(10)]),
    ]
    src = write_jsonl(str(tmp / "world.jsonl"), records)
    osmface.load_database(db, [src])
    osmface.assemble_database(db)
    osmface.enable_history(db)
    osmface.apply_changes(db, change_file(tmp, "del.jsonl", "del", 0,
                                          [delete(1, "node", 3)]))
    copy = str(tmp / "copy.db")
    osmface.checkout_workcopy(db, copy, revision=0)
    osmface.workcopy_edit(copy, edit_file(tmp, "e.jsonl", "e", 0, [
        # field rewires the way but keeps using node 3 (deleted on main)
        {"op": "put", "expected_version": 1,
         "entity": way(10, [1, 3, 4, 1])},
    ]))
    pkg = str(tmp / "pkg")
    osmface.workcopy_export(copy, pkg)
    plan = osmface.create_merge_plan(db, pkg)
    check("no entity conflict in the cross case",
          plan["unresolved_conflicts"] == [])
    check("plan surfaces the reference gap",
          any(g["from"]["id"] == 10 and g["missing"]["id"] == 3
              for g in plan["reference_gaps"]),
          json.dumps(plan["reference_gaps"]))
    preview = osmface.merge_preview(db, plan["plan_id"])
    check("preview flags relation 100 incomplete",
          preview["relations"]["100"]["status"] == "incomplete")

    # custom repair: way 10 detours around the deleted corner
    osmface.save_merge_rulings(db, plan["plan_id"], [
        {"type": "way", "id": 10, "choice": "custom",
         "entity": way(10, [1, 2, 4, 1]),
         "reason": "node 3 deleted on main; triangle keeps the ring closed"},
    ])
    preview2 = osmface.merge_preview(db, plan["plan_id"])
    check("repaired preview is complete",
          preview2["relations"]["100"]["status"] == "complete")
    preview_area = shape(
        preview2["relations"]["100"]["faces"][0]["geojson"]).area

    # independent geometry cross-check: materialize the expected candidate
    # by hand and run the ORDINARY assembler on a fresh staging database
    candidate_records = [
        node(1, 0, 0), node(2, 10, 0), node(4, 0, 10),
        way(10, [1, 2, 4, 1]),
        rel(100, [wm(10)]),
    ]
    cand_src = write_jsonl(str(tmp / "candidate.jsonl"), candidate_records)
    cand_db = str(tmp / "candidate.db")
    osmface.load_database(cand_db, [cand_src])
    summary = osmface.assemble_database(cand_db)
    check("independent assembly agrees on status",
          summary["status_counts"]["complete"] == 1)
    ref = osmface.inspect_database(cand_db, relation_id=100)["relation"]
    ref_area = shape(ref["faces"][0]["geometry"]).area
    check("independent assembly agrees on area",
          abs(ref_area - preview_area) < 1e-9,
          f"preview {preview_area} vs reference {ref_area}")

    receipt = osmface.confirm_merge(db, plan["plan_id"], "m-cross")
    landed = osmface.inspect_database(
        db, relation_id=100, revision=receipt["result_revision"])["relation"]
    check("landed state matches the preview",
          landed["status"] == "complete"
          and abs(shape(landed["faces"][0]["geometry"]).area
                  - preview_area) < 1e-9)
    old = osmface.inspect_database(db, relation_id=100, revision=1)
    check("old revision still viewable and broken",
          old["relation"]["status"] == "incomplete")
    older = osmface.inspect_database(db, relation_id=100, revision=0)
    check("revision 0 still complete", older["relation"]["status"] == "complete")


def scenario_robustness(tmp):
    print("scenario: corrupt packages, stale plans, retries")
    db = str(tmp / "main.db")
    records = [
        node(1, 0, 0), node(2, 10, 0), node(3, 10, 10), node(4, 0, 10),
        way(10, [1, 2, 3, 4, 1]),
        rel(100, [wm(10)]),
    ]
    src = write_jsonl(str(tmp / "world.jsonl"), records)
    osmface.load_database(db, [src])
    osmface.assemble_database(db)
    osmface.enable_history(db)
    copy = str(tmp / "copy.db")
    osmface.checkout_workcopy(db, copy)
    osmface.workcopy_edit(copy, edit_file(tmp, "e.jsonl", "e", 0, [
        {"op": "put", "expected_version": 1, "entity": node(2, 12, 0)},
    ]))
    pkg = str(tmp / "pkg")
    osmface.workcopy_export(copy, pkg)

    # corrupt one byte of the edits file
    corrupt = str(tmp / "pkg-corrupt")
    shutil.copytree(pkg, corrupt)
    ep = os.path.join(corrupt, "edits.jsonl")
    data = open(ep, "rb").read()
    open(ep, "wb").write(data[:20] + b"X" + data[21:])
    try:
        osmface.create_merge_plan(db, corrupt)
        check("corrupt package rejected", False)
    except osmface.PackageError:
        check("corrupt package rejected", True)

    # wrong source: a second warehouse with identical content
    db2 = str(tmp / "other.db")
    osmface.load_database(db2, [src])
    osmface.assemble_database(db2)
    osmface.enable_history(db2)
    try:
        osmface.create_merge_plan(db2, pkg)
        check("foreign package rejected", False)
    except osmface.MergeError:
        check("foreign package rejected", True)

    # unruled plan cannot confirm
    osmface.apply_changes(db, change_file(tmp, "h.jsonl", "h", 0,
                                          [put(1, node(2, 13, 0))]))
    plan = osmface.create_merge_plan(db, pkg)
    try:
        osmface.confirm_merge(db, plan["plan_id"], "m-rob")
        check("unruled plan cannot confirm", False)
    except osmface.MergeError:
        check("unruled plan cannot confirm", True)
    check("failed confirm left no revision",
          osmface.inspect_database(db)["revision"] == 1)

    # rulings saved, then main advances: confirm must refuse
    osmface.save_merge_rulings(db, plan["plan_id"], [
        {"type": "node", "id": 2, "choice": "field"},
    ])
    osmface.apply_changes(db, change_file(tmp, "h2.jsonl", "h2", 1,
                                          [put(1, node(4, 0, 44))]))
    try:
        osmface.confirm_merge(db, plan["plan_id"], "m-rob")
        check("stale plan refused after head moved", False)
    except osmface.MergeConflict:
        check("stale plan refused after head moved", True)
    import sqlite3
    raw = sqlite3.connect(db)
    payload = raw.execute(
        "SELECT payload FROM entity_versions WHERE type='node' AND id=2"
    ).fetchone()[0]
    raw.close()
    check("old ruling was not applied to the new head",
          json.loads(payload) == [13, 0])

    # fresh plan on the new head, rule, confirm, then resend
    plan2 = osmface.create_merge_plan(db, pkg)
    osmface.save_merge_rulings(db, plan2["plan_id"], [
        {"type": "node", "id": 2, "choice": "field"},
    ])
    receipt = osmface.confirm_merge(db, plan2["plan_id"], "m-rob")
    check("confirm produced exactly one revision",
          receipt["result_revision"] == 3
          and osmface.inspect_database(db)["revision"] == 3)
    raw = sqlite3.connect(db)
    row = raw.execute(
        "SELECT version, payload FROM entity_versions WHERE type='node'"
        " AND id=2").fetchone()
    n_revisions = raw.execute(
        "SELECT COUNT(*) FROM entity_history WHERE type='node' AND id=2"
        " AND rev=3").fetchone()[0]
    raw.close()
    check("entity landed with warehouse-side version bump",
          row[0] == 3 and json.loads(row[1]) == [12, 0])
    check("exactly one new object version written", n_revisions == 1)
    resent = osmface.confirm_merge(db, plan2["plan_id"], "m-rob")
    check("resend returns the original receipt", resent == receipt)
    check("resend did not move the head",
          osmface.inspect_database(db)["revision"] == 3)
    fetched = osmface.get_merge_receipt(db, "m-rob")
    check("receipt retrievable after the fact", fetched == receipt)


def main():
    tmp = tempfile.mkdtemp(prefix="osmface-merge-check-")
    scenarios = [
        ("three-way table", scenario_three_way_table),
        ("duplicate members", scenario_duplicate_members_not_set_compared),
        ("two copies", scenario_two_copies_and_id_allocation),
        ("cross gap", scenario_cross_gap_and_repair),
        ("robustness", scenario_robustness),
    ]
    for i, (name, fn) in enumerate(scenarios):
        sub = os.path.join(tmp, f"s{i}")
        os.makedirs(sub, exist_ok=True)
        try:
            from pathlib import Path
            fn(Path(sub))
        except Exception as exc:
            import traceback
            traceback.print_exc()
            FAILURES.append(f"{name}: exception {exc}")
    print()
    if FAILURES:
        print(f"CHECK FAILED: {len(FAILURES)} failure(s)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all merge acceptance checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

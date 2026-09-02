#!/usr/bin/env python3
"""
Patch B (SQL wiring): add can_coaching + can_coaching_reports to the sub-admin
INSERT/UPDATE/SELECT in app.py, keeping column/placeholder/param counts aligned.

Strategy: everywhere the SQL references can_holiday_awol, insert the two new
tokens immediately after it — in the SAME form — so column list, VALUES (%s),
UPDATE SET, param tuples, and the SELECT all stay in lockstep. This avoids the
positional VALUES-count mismatch bug.

Token forms handled (each must balance):
  A. column list:      "can_holiday_awol, can_memos"  -> add ", can_coaching, can_coaching_reports"
  B. UPDATE SET:       "can_holiday_awol=%s, can_memos=%s" -> add two "=%s"
  C. params:           "perms['can_holiday_awol'], perms['can_memos']" -> add two perms[...]
  D. SELECT:           "sa.can_holiday_awol, sa.can_memos" -> add two "sa."
  E. VALUES:           add two %s per VALUES list (counted, not token-anchored)

Because the exact neighbouring tokens differ between the two handlers, this
script requires you to PASTE the exact strings after reviewing them. It ships
with the anchors seen in your grep, but VERIFY each count in the dry-run.

Safety: per-anchor count assertion, ast.parse gate, .bak, dry-run default.

    python3 scripts/patch_coaching_ui_B.py          # dry-run — CHECK COUNTS
    python3 scripts/patch_coaching_ui_B.py --live
"""
import sys, ast, shutil, datetime

LIVE = "--live" in sys.argv
APP = "/var/www/html/leavesystem/app.py"

NEW = "can_coaching"

# Each tuple: (description, exact_old, exact_new, expected_count)
EDITS = [
    # ---- Handler #1 ----
    ("H1 INSERT cols",
     "can_night_differential, can_holiday_awol, can_memos, assigned_by, can_ot_hours)",
     "can_night_differential, can_holiday_awol, can_coaching, can_coaching_reports, can_memos, assigned_by, can_ot_hours)",
     1),
    ("H1 UPDATE set",
     "can_night_differential=%s, can_holiday_awol=%s, can_memos=%s, assigned_by=%s, can_ot_hours=%s",
     "can_night_differential=%s, can_holiday_awol=%s, can_coaching=%s, can_coaching_reports=%s, can_memos=%s, assigned_by=%s, can_ot_hours=%s",
     1),
    ("H1 params (appears 2x: insert + update)",
     "perms['can_night_differential'], perms['can_holiday_awol'], perms['can_memos'], session['user']['emp_number'], perms['can_ot_hours']",
     "perms['can_night_differential'], perms['can_holiday_awol'], perms['can_coaching'], perms['can_coaching_reports'], perms['can_memos'], session['user']['emp_number'], perms['can_ot_hours']",
     2),
    # ---- Handler #2 ----
    ("H2 INSERT cols",
     "can_night_differential, can_holiday_awol, can_ot_hours, can_memos, assigned_by)",
     "can_night_differential, can_holiday_awol, can_coaching, can_coaching_reports, can_ot_hours, can_memos, assigned_by)",
     1),
    ("H2 UPDATE set",
     "can_night_differential=%s, can_holiday_awol=%s, can_ot_hours=%s, can_memos=%s, assigned_by=%s",
     "can_night_differential=%s, can_holiday_awol=%s, can_coaching=%s, can_coaching_reports=%s, can_ot_hours=%s, can_memos=%s, assigned_by=%s",
     1),
    ("H2 params (appears 2x)",
     "perms['can_night_differential'], perms['can_holiday_awol'], perms['can_ot_hours'], perms['can_memos'], session['user']['emp_number']",
     "perms['can_night_differential'], perms['can_holiday_awol'], perms['can_coaching'], perms['can_coaching_reports'], perms['can_ot_hours'], perms['can_memos'], session['user']['emp_number']",
     2),
    # ---- SELECT (load current perms for the page) ----
    ("SELECT cols",
     "sa.can_night_differential, sa.can_holiday_awol, sa.can_memos, sa.can_ot_hours",
     "sa.can_night_differential, sa.can_holiday_awol, sa.can_coaching, sa.can_coaching_reports, sa.can_memos, sa.can_ot_hours",
     1),
]

# VALUES placeholder lists: each handler has one "VALUES (%s,...)" with 35 (H1)
# and 34 (H2) placeholders. We add 2 to each. Anchor on the exact strings.
VALUES_EDITS = [
    ("H1 VALUES (+2 %s)",
     "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
     "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
     1),
    ("H2 VALUES (+2 %s)",
     "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
     "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
     1),
]


def main():
    print(f"[{'LIVE' if LIVE else 'DRY-RUN'}] Patch B — INSERT/UPDATE/SELECT wiring\n")
    with open(APP, encoding="utf-8") as f:
        c = f.read()

    if "perms['can_coaching']" in c and "sa.can_coaching" in c:
        print("  SKIP — SQL already wired for can_coaching")
        return

    all_ok = True
    for desc, old, new, want in EDITS + VALUES_EDITS:
        got = c.count(old)
        status = "OK " if got == want else "ERR"
        if got != want:
            all_ok = False
        print(f"  [{status}] {desc}: found {got}, expected {want}")

    if not all_ok:
        print("\n  ABORT — one or more anchors didn't match expected count. No changes.")
        print("  Paste the failing region so anchors can be corrected.")
        return

    newc = c
    for _, old, new, _ in EDITS + VALUES_EDITS:
        newc = newc.replace(old, new)

    try:
        ast.parse(newc)
    except SyntaxError as e:
        print(f"\n  ABORT — patched file fails to parse: {e}")
        return

    print("\n  All anchors matched; patched file parses clean.")
    if LIVE:
        bak = f"{APP}.bak_{datetime.datetime.now():%Y%m%d_%H%M%S}"
        shutil.copy2(APP, bak)
        with open(APP, "w", encoding="utf-8") as f:
            f.write(newc)
        print(f"  backup: {bak}")
    print("\nDone." + ("" if LIVE else "  Re-run with --live to apply."))


if __name__ == "__main__":
    main()

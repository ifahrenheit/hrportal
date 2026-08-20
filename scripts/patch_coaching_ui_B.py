#!/usr/bin/env python3
"""
Patch A (safe): add the Coaching toggle to the sub-admins template + form-parse
in BOTH save handlers. Does NOT touch INSERT/UPDATE column lists (that's Patch B).

After A alone: the checkbox appears and its value is read into `perms`, but the
INSERT/UPDATE won't persist it yet (KeyError-safe: handlers reference perms[...]
positionally, so B must follow). To avoid a KeyError before B, A also makes the
form-parse keys present. B wires them into SQL.

Safety: exact-match, count assertion, ast.parse gate, .bak, dry-run default.

    python3 scripts/patch_coaching_ui_A.py          # dry-run
    python3 scripts/patch_coaching_ui_A.py --live
"""
import sys, ast, shutil, datetime

LIVE = "--live" in sys.argv
APP = "/var/www/html/leavesystem/app.py"
TPL = "/var/www/html/leavesystem/templates/admin/sub_admins.html"

# --- TEMPLATE: add two rows in the People & HR group, after Requirements ---
TPL_ANCHOR = "    ('can_requirements',      'bi-clipboard-check',     'Requirements'),"
TPL_INSERT = TPL_ANCHOR + """
    ('can_coaching',          'bi-easel2',              'Coaching'),
    ('can_coaching_reports',  'bi-graph-up',            'Coaching Reports'),"""
TPL_MARKER = "('can_coaching',"

# --- APP: form-parse in BOTH handlers. The line appears twice (both handlers),
# so we replace ALL occurrences (count must be 2). ---
FP_ANCHOR = "                    'can_holiday_awol': 1 if request.form.get('can_holiday_awol') else 0,"
# handler #2 uses different indentation? Both showed same 20-space indent in grep.
FP_INSERT = FP_ANCHOR + """
                    'can_coaching': 1 if request.form.get('can_coaching') else 0,
                    'can_coaching_reports': 1 if request.form.get('can_coaching_reports') else 0,"""
FP_MARKER = "'can_coaching': 1 if request.form.get('can_coaching')"


def patch_template():
    print("TEMPLATE:")
    with open(TPL, encoding="utf-8") as f:
        c = f.read()
    if TPL_MARKER in c:
        print("  SKIP — coaching toggle already in template")
        return
    n = c.count(TPL_ANCHOR)
    if n != 1:
        print(f"  ERROR — template anchor found {n} times (need 1). Aborting template.")
        return
    newc = c.replace(TPL_ANCHOR, TPL_INSERT, 1)
    print("  OK — 1 anchor matched")
    if LIVE:
        bak = f"{TPL}.bak_{datetime.datetime.now():%Y%m%d_%H%M%S}"
        shutil.copy2(TPL, bak); open(TPL, "w", encoding="utf-8").write(newc)
        print(f"     backup: {bak}")


def patch_app_formparse():
    print("APP form-parse (both handlers):")
    with open(APP, encoding="utf-8") as f:
        c = f.read()
    if FP_MARKER in c:
        print("  SKIP — form-parse already present")
        return
    n = c.count(FP_ANCHOR)
    if n != 2:
        print(f"  ERROR — form-parse anchor found {n} times (need exactly 2). Aborting.")
        return
    newc = c.replace(FP_ANCHOR, FP_INSERT)  # replace BOTH
    try:
        ast.parse(newc)
    except SyntaxError as e:
        print(f"  ERROR — parse fail: {e}. Aborting, no write.")
        return
    print("  OK — 2 anchors matched, parses clean")
    if LIVE:
        bak = f"{APP}.bak_{datetime.datetime.now():%Y%m%d_%H%M%S}"
        shutil.copy2(APP, bak); open(APP, "w", encoding="utf-8").write(newc)
        print(f"     backup: {bak}")


def main():
    print(f"[{'LIVE' if LIVE else 'DRY-RUN'}] Patch A — template + form-parse\n")
    patch_template()
    patch_app_formparse()
    print("\nDone." + ("" if LIVE else "  Re-run with --live to apply."))
    print("NOTE: run Patch B next to wire INSERT/UPDATE/SELECT, or the toggle won't persist.")


if __name__ == "__main__":
    main()

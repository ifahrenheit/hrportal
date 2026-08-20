#!/usr/bin/env python3
"""
patch_getter_coaching.py — REQUIRED fix: make get_sub_admin_permissions()
return can_coaching / can_coaching_reports so grants take effect at login.

Without this, the DB grants are invisible to the app (getter doesn't read them).

Safety: exact-match + count assertion (==1), ast.parse gate, timestamped .bak,
dry-run default. Never sed. Idempotent (skips if already present).

    cd /var/www/html/leavesystem
    python3 scripts/patch_getter_coaching.py          # dry-run
    python3 scripts/patch_getter_coaching.py --live
"""
import sys, ast, shutil, datetime

LIVE = "--live" in sys.argv
APP = "/var/www/html/leavesystem/app.py"

# Anchor: the getter's can_holiday_awol line + the dict close that follows it.
ANCHOR = """                'can_holiday_awol': bool(row.get('can_holiday_awol', 0)),
            }"""

INSERT = """                'can_holiday_awol': bool(row.get('can_holiday_awol', 0)),
                'can_coaching': bool(row.get('can_coaching', 0)),
                'can_coaching_reports': bool(row.get('can_coaching_reports', 0)),
            }"""

MARKER = "'can_coaching': bool(row.get('can_coaching', 0))"


def main():
    print(f"[{'LIVE' if LIVE else 'DRY-RUN'}] getter can_coaching fix\n")
    with open(APP, encoding="utf-8") as f:
        c = f.read()

    if MARKER in c:
        print("  SKIP — getter already returns can_coaching")
        return

    n = c.count(ANCHOR)
    if n != 1:
        print(f"  ERROR — anchor found {n} times (need exactly 1). Aborting.")
        return

    newc = c.replace(ANCHOR, INSERT, 1)

    # Lint gate — must parse before we write.
    try:
        ast.parse(newc)
    except SyntaxError as e:
        print(f"  ERROR — patched file fails to parse: {e}. Aborting, no write.")
        return

    print("  OK — 1 anchor matched, patched file parses clean")
    if LIVE:
        bak = f"{APP}.bak_{datetime.datetime.now():%Y%m%d_%H%M%S}"
        shutil.copy2(APP, bak)
        with open(APP, "w", encoding="utf-8") as f:
            f.write(newc)
        print(f"       backup: {bak}")
    print("\nDone." + ("" if LIVE else "  Re-run with --live to apply."))


if __name__ == "__main__":
    main()

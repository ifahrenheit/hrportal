#!/usr/bin/env python3
"""
Add "My Coaching" to the My Requests menu (agent-facing, ALL employees).
Topnav: after "My Records" in the requests panel.
Sidebar: after the "My Records" link in the My Requests section.

Anchors are byte-exact from cat -A. Safety: count assertion, .bak, dry-run
default, no sed, idempotent via marker.

    cd /var/www/html/leavesystem
    python3 scripts/patch_my_coaching_nav.py          # dry-run
    python3 scripts/patch_my_coaching_nav.py --live
"""
import sys, shutil, datetime

LIVE = "--live" in sys.argv
TOPNAV  = "/var/www/html/leavesystem/templates/nav/_topnav.html"
SIDEBAR = "/var/www/html/leavesystem/templates/nav/_sidebar.html"
MARKER = "url_for('coaching.my_sessions')"

# --- TOPNAV: after My Records (single-line <a>) ---
TN_ANCHOR = '''        <a class="tn-item {% if EP == 'my_records.records' %}active{% endif %}" href="{{ url_for('my_records.records') }}">
          <i class="bi bi-clipboard-check"></i> My Records</a>'''
TN_INSERT = TN_ANCHOR + '''
        <a class="tn-item {% if EP and EP.startswith('coaching.my') %}active{% endif %}" href="{{ url_for('coaching.my_sessions') }}">
          <i class="bi bi-easel2"></i> My Coaching</a>'''

# --- SIDEBAR: after My Records (multi-line <a>) ---
SB_ANCHOR = '''      <a class="nav-link {% if request.endpoint == 'my_records.records' %}active{% endif %}"
         href="{{ url_for('my_records.records') }}">
        <i class="bi bi-clipboard-check"></i>
        <span class="nav-label">My Records</span>
      </a>'''
SB_INSERT = SB_ANCHOR + '''
      <a class="nav-link {% if request.endpoint and request.endpoint.startswith('coaching.my') %}active{% endif %}"
         href="{{ url_for('coaching.my_sessions') }}">
        <i class="bi bi-easel2"></i>
        <span class="nav-label">My Coaching</span>
      </a>'''


def patch(path, anchor, insert, label):
    print(f"{label}:")
    with open(path, encoding="utf-8") as f:
        c = f.read()
    if MARKER in c:
        print("  SKIP — My Coaching already present")
        return
    n = c.count(anchor)
    if n != 1:
        print(f"  ERROR — anchor found {n} times (need 1). Aborting {label}.")
        return
    newc = c.replace(anchor, insert, 1)
    print("  OK — My Coaching link will be inserted")
    if LIVE:
        bak = f"{path}.bak_{datetime.datetime.now():%Y%m%d_%H%M%S}"
        shutil.copy2(path, bak)
        with open(path, "w", encoding="utf-8") as f:
            f.write(newc)
        print(f"     backup: {bak}")


def main():
    print(f"[{'LIVE' if LIVE else 'DRY-RUN'}] add My Coaching to My Requests\n")
    patch(TOPNAV, TN_ANCHOR, TN_INSERT, "TOPNAV")
    patch(SIDEBAR, SB_ANCHOR, SB_INSERT, "SIDEBAR")
    print("\nDone." + ("" if LIVE else "  Re-run with --live to apply."))


if __name__ == "__main__":
    main()

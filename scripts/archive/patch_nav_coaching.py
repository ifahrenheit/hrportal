#!/usr/bin/env python3
"""
patch_nav_coaching.py — insert the Coaching link into the topnav + sidebar.

Adds "Coaching" to:
  - _sidebar.html : "My Team (Supervisors)" section, after Team Calendar
  - _topnav.html  : data-panel="team", after Team Calendar tn-item
                    (inside the existing {% if SUPER or ADMIN %} gate)

Visibility: all supervisors + admins (matches coaching route access in coaching.py).

Safety:
  - exact-string .replace() with COUNT ASSERTION (anchor must appear exactly once)
  - timestamped .bak per file BEFORE writing
  - dry-run by default; --live to write
  - pure Python, never sed
  - idempotent: if the coaching link is already present, that file is skipped

Run:
    cd /var/www/html/leavesystem
    python3 scripts/patch_nav_coaching.py          # dry-run
    python3 scripts/patch_nav_coaching.py --live   # apply
"""
import sys, os, shutil, datetime

LIVE = "--live" in sys.argv
BASE = "/var/www/html/leavesystem/templates/nav"
SIDEBAR = os.path.join(BASE, "_sidebar.html")
TOPNAV  = os.path.join(BASE, "_topnav.html")

# ---- SIDEBAR ---- (verbatim Team Calendar block inside supervisor-body)
SIDEBAR_ANCHOR = '''      {% if session.get('is_supervisor') or session.get('is_admin') %}
      <a class="nav-link {% if request.endpoint == 'supervisor_team_calendar' %}active{% endif %}" href="{{ url_for('supervisor_team_calendar') }}">
        <i class="bi bi-calendar3"></i> <span class="nav-label">Team Calendar</span>
      </a>
      {% endif %}'''

SIDEBAR_INSERT = '''
      {% if session.get('is_supervisor') or session.get('is_admin') or session.get('permissions', {}).get('can_coaching') or session.get('permissions', {}).get('can_coaching_reports') %}
      <a class="nav-link {% if request.endpoint and request.endpoint.startswith('coaching.') %}active{% endif %}" href="{{ url_for('coaching.index') }}">
        <i class="bi bi-easel2"></i> <span class="nav-label">Coaching</span>
      </a>
      {% endif %}'''

# ---- TOPNAV ---- (verbatim Team Calendar two-line block, inside {% if SUPER or ADMIN %})
TOPNAV_ANCHOR = '''        <a class="tn-item {% if EP == 'supervisor_team_calendar' %}active{% endif %}" href="{{ url_for('supervisor_team_calendar') }}">
          <i class="bi bi-calendar3"></i> Team Calendar</a>'''

# Inserted right after; already inside the SUPER/ADMIN gate so no extra {% if %} needed.
TOPNAV_INSERT = '''
        <a class="tn-item {% if EP and EP.startswith('coaching.') %}active{% endif %}" href="{{ url_for('coaching.index') }}">
          <i class="bi bi-easel2"></i> Coaching</a>'''


def patch_file(path, anchor, insert, marker="coaching.index"):
    with open(path, encoding="utf-8") as f:
        content = f.read()

    if marker in content:
        print(f"  SKIP {os.path.basename(path)} — coaching link already present")
        return False

    n = content.count(anchor)
    if n != 1:
        print(f"  ERROR {os.path.basename(path)} — anchor found {n} times (need exactly 1). Aborting this file.")
        return False

    new = content.replace(anchor, anchor + insert, 1)
    print(f"  OK   {os.path.basename(path)} — 1 anchor matched, coaching link will be inserted")
    if LIVE:
        bak = f"{path}.bak_{datetime.datetime.now():%Y%m%d_%H%M%S}"
        shutil.copy2(path, bak)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
        print(f"       backup: {bak}")
    return True


def main():
    print(f"[{'LIVE' if LIVE else 'DRY-RUN'}] nav coaching patch\n")
    print("SIDEBAR:")
    patch_file(SIDEBAR, SIDEBAR_ANCHOR, SIDEBAR_INSERT)
    print("TOPNAV:")
    patch_file(TOPNAV, TOPNAV_ANCHOR, TOPNAV_INSERT)
    print("\nDone." + ("" if LIVE else "  Re-run with --live to apply."))


if __name__ == "__main__":
    main()

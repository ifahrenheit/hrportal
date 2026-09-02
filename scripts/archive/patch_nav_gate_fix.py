#!/usr/bin/env python3
"""
patch_nav_gate_fix.py — make the Coaching nav gates match the route exactly.

After this, BOTH nav links are gated purely on:
    admin OR can_coaching OR can_coaching_reports
identical to can_coaching()/can_reports() in coaching.py.

- Topnav: wraps the (currently ungated) coaching item in its own {% if %}.
- Sidebar: rewrites the existing gate to the permission-only form.

Safety: exact-match + count assertion, .bak per file, dry-run default, no sed.
Idempotent: skips if the new gate marker is already present.

    cd /var/www/html/leavesystem
    python3 scripts/patch_nav_gate_fix.py          # dry-run
    python3 scripts/patch_nav_gate_fix.py --live
"""
import sys, os, shutil, datetime

LIVE = "--live" in sys.argv
BASE = "/var/www/html/leavesystem/templates/nav"
TOPNAV  = os.path.join(BASE, "_topnav.html")
SIDEBAR = os.path.join(BASE, "_sidebar.html")

TN_MARKER = "{% if ADMIN or P.get('can_coaching') or P.get('can_coaching_reports') %}<a class=\"tn-item {% if EP and EP.startswith('coaching.')"
SB_MARKER = "session.get('permissions', {}).get('can_coaching') or session.get('permissions', {}).get('can_coaching_reports') %}\n      <a class=\"nav-link {% if request.endpoint and request.endpoint.startswith('coaching.')"

# --- TOPNAV: wrap the ungated coaching item in its own permission gate ---
TN_OLD = '''        <a class="tn-item {% if EP and EP.startswith('coaching.') %}active{% endif %}" href="{{ url_for('coaching.index') }}">
          <i class="bi bi-easel2"></i> Coaching</a>'''
TN_NEW = '''        {% if ADMIN or P.get('can_coaching') or P.get('can_coaching_reports') %}<a class="tn-item {% if EP and EP.startswith('coaching.') %}active{% endif %}" href="{{ url_for('coaching.index') }}">
          <i class="bi bi-easel2"></i> Coaching</a>{% endif %}'''

# --- SIDEBAR: rewrite whatever gate currently wraps the link to permission-only ---
# We match from the {% if ... %} immediately preceding the sidebar coaching <a>.
# The earlier patch inserted this exact opening; rewrite it.
SB_OLD_A = '''{% if session.get('is_tl') or session.get('is_supervisor') or session.get('is_admin') or session.get('permissions', {}).get('can_coaching') or session.get('permissions', {}).get('can_coaching_reports') %}
      <a class="nav-link {% if request.endpoint and request.endpoint.startswith('coaching.') %}active{% endif %}" href="{{ url_for('coaching.index') }}">'''
SB_OLD_B = '''{% if session.get('is_supervisor') or session.get('is_admin') or session.get('permissions', {}).get('can_coaching') or session.get('permissions', {}).get('can_coaching_reports') %}
      <a class="nav-link {% if request.endpoint and request.endpoint.startswith('coaching.') %}active{% endif %}" href="{{ url_for('coaching.index') }}">'''
SB_NEW = '''{% if session.get('is_admin') or session.get('permissions', {}).get('can_coaching') or session.get('permissions', {}).get('can_coaching_reports') %}
      <a class="nav-link {% if request.endpoint and request.endpoint.startswith('coaching.') %}active{% endif %}" href="{{ url_for('coaching.index') }}">'''


def patch(path, pairs, marker):
    with open(path, encoding="utf-8") as f:
        c = f.read()
    if marker in c:
        print(f"  SKIP {os.path.basename(path)} — already permission-only gate")
        return
    for old, new in pairs:
        if c.count(old) == 1:
            newc = c.replace(old, new, 1)
            print(f"  OK   {os.path.basename(path)} — gate rewritten")
            if LIVE:
                bak = f"{path}.bak_{datetime.datetime.now():%Y%m%d_%H%M%S}"
                shutil.copy2(path, bak)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(newc)
                print(f"       backup: {bak}")
            return
    counts = [c.count(o) for o, _ in pairs]
    print(f"  ERROR {os.path.basename(path)} — no unique anchor matched (counts={counts}). Aborting.")


def main():
    print(f"[{'LIVE' if LIVE else 'DRY-RUN'}] align coaching nav gates to permission-only\n")
    print("TOPNAV:")
    patch(TOPNAV, [(TN_OLD, TN_NEW)], TN_MARKER)
    print("SIDEBAR:")
    patch(SIDEBAR, [(SB_OLD_A, SB_NEW), (SB_OLD_B, SB_NEW)], SB_MARKER)
    print("\nDone." + ("" if LIVE else "  Re-run with --live to apply."))


if __name__ == "__main__":
    main()

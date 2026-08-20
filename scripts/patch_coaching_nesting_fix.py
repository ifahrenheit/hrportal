#!/usr/bin/env python3
"""
Move the coaching tn-item OUT of the {% if SUPER or ADMIN %} block so pure TLs
(SUPER=False) see it. Anchors are byte-exact from the live file (note: the file
has {{url_for  with no space).

Safety: exact-match + count assertion (==1), .bak, dry-run default, no sed.
Idempotent via marker.
"""
import sys, shutil, datetime

LIVE = "--live" in sys.argv
TOPNAV = "/var/www/html/leavesystem/templates/nav/_topnav.html"

# VERBATIM from `sed -n '384,386p' | cat -A` (mind the {{url_for no-space form)
TN_INSIDE = '''        {% if ADMIN or P.get('can_coaching') or P.get('can_coaching_reports') %}<a class="tn-item {% if EP and EP.startswith('coaching.') %}active{% endif %}" href="{{url_for('coaching.index') }}">
          <i class="bi bi-easel2"></i> Coaching</a>{% endif %}
        {% endif %}'''

# Coaching removed from here; block closes first, then coaching with its own gate.
TN_FIXED = '''        {% endif %}
        {% if ADMIN or P.get('can_coaching') or P.get('can_coaching_reports') %}
        <a class="tn-item {% if EP and EP.startswith('coaching.') %}active{% endif %}" href="{{ url_for('coaching.index') }}">
          <i class="bi bi-easel2"></i> Coaching</a>
        {% endif %}'''

# Marker: coaching {% if %} now appears on its own line AFTER a lone {% endif %}
TN_MARKER = '''        {% endif %}
        {% if ADMIN or P.get('can_coaching') or P.get('can_coaching_reports') %}
        <a class="tn-item'''


def main():
    print(f"[{'LIVE' if LIVE else 'DRY-RUN'}] fix coaching topnav nesting\n")
    with open(TOPNAV, encoding="utf-8") as f:
        c = f.read()
    if TN_MARKER in c:
        print("  SKIP — already fixed"); return
    n = c.count(TN_INSIDE)
    if n != 1:
        print(f"  ERROR — anchor found {n} times (need 1). Aborting."); return
    newc = c.replace(TN_INSIDE, TN_FIXED, 1)
    print("  OK — coaching moved outside {% if SUPER or ADMIN %}")
    if LIVE:
        bak = f"{TOPNAV}.bak_{datetime.datetime.now():%Y%m%d_%H%M%S}"
        shutil.copy2(TOPNAV, bak)
        with open(TOPNAV, "w", encoding="utf-8") as f:
            f.write(newc)
        print(f"     backup: {bak}")
    print("\nDone." + ("" if LIVE else "  Re-run with --live to apply."))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Line-number-based fix (string anchors failed on hidden whitespace).

Moves the coaching tn-item out of {% if SUPER or ADMIN %} in _topnav.html.
Verifies each target line contains the expected marker BEFORE editing —
aborts if the file has shifted. .bak, dry-run default, no sed.

Expected (from cat -A):
  384: {% if ADMIN or P.get('can_coaching') ... coaching.index ...
  385: ... Coaching</a>{% endif %}
  386: {% endif %}          <- closes {% if SUPER or ADMIN %}
"""
import sys, shutil, datetime

LIVE = "--live" in sys.argv
TOPNAV = "/var/www/html/leavesystem/templates/nav/_topnav.html"

def main():
    print(f"[{'LIVE' if LIVE else 'DRY-RUN'}] line-based coaching nesting fix\n")
    with open(TOPNAV, encoding="utf-8") as f:
        lines = f.readlines()  # keep line endings

    # idempotency: if a standalone coaching {% if %} already follows a lone endif, skip
    joined = "".join(lines)
    if "{% endif %}\n        {% if ADMIN or P.get('can_coaching')" in joined:
        print("  SKIP — already fixed")
        return

    # locate the coaching line (0-based index)
    idx = None
    for i, ln in enumerate(lines):
        if "coaching.index" in ln and "P.get('can_coaching')" in ln and "<a class=\"tn-item" in ln:
            idx = i
            break
    if idx is None:
        print("  ERROR — coaching line not found. Aborting.")
        return

    # Expected neighbours: idx = coaching <if+a>, idx+1 = Coaching</a>{% endif %},
    # idx+2 = the block-closing {% endif %}
    l_coach_if = lines[idx]
    l_coach_a  = lines[idx+1] if idx+1 < len(lines) else ""
    l_endif    = lines[idx+2] if idx+2 < len(lines) else ""

    print(f"  line {idx+1}: {l_coach_if.rstrip()[:70]}...")
    print(f"  line {idx+2}: {l_coach_a.rstrip()[:70]}")
    print(f"  line {idx+3}: {l_endif.rstrip()[:70]}")

    if "Coaching</a>" not in l_coach_a:
        print("  ERROR — line after coaching-if is not the Coaching anchor. Aborting.")
        return
    if l_endif.strip() != "{% endif %}":
        print("  ERROR — expected a lone {% endif %} closing the SUPER/ADMIN block. Aborting.")
        return

    indent = "        "
    new_block = [
        f"{indent}{{% endif %}}\n",
        f"{indent}{{% if ADMIN or P.get('can_coaching') or P.get('can_coaching_reports') %}}\n",
        f"{indent}<a class=\"tn-item {{% if EP and EP.startswith('coaching.') %}}active{{% endif %}}\" href=\"{{{{ url_for('coaching.index') }}}}\">\n",
        f"{indent}  <i class=\"bi bi-easel2\"></i> Coaching</a>\n",
        f"{indent}{{% endif %}}\n",
    ]

    # Replace the three original lines (coach_if, coach_a, endif) with new_block.
    new_lines = lines[:idx] + new_block + lines[idx+3:]

    print("\n  OK — will move coaching item outside the SUPER/ADMIN block")
    if LIVE:
        bak = f"{TOPNAV}.bak_{datetime.datetime.now():%Y%m%d_%H%M%S}"
        shutil.copy2(TOPNAV, bak)
        with open(TOPNAV, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        print(f"     backup: {bak}")
    print("\nDone." + ("" if LIVE else "  Re-run with --live to apply."))


if __name__ == "__main__":
    main()

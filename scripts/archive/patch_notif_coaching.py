#!/usr/bin/env python3
"""
Add a 'My coaching' section to /api/notifications — the logged-in employee's own
pending/for_followup coaching sessions awaiting their SMART action plan.

Matches the existing section shape ({message,link,type,search,created_at} items;
{title,icon,items} section; total += len). Inserted right before the final
`return jsonify({"sections": sections, "unread": total})` of api_notifications.

Safety: exact-match + count assertion (==1 within the function slice),
ast.parse gate, .bak, dry-run default, no sed. Idempotent via marker.

    cd /var/www/html/leavesystem
    python3 scripts/patch_notif_coaching.py          # dry-run
    python3 scripts/patch_notif_coaching.py --live
"""
import sys, ast, shutil, datetime

LIVE = "--live" in sys.argv
APP = "/var/www/html/leavesystem/app.py"
MARKER = "[notif] coaching section"

# The coaching block. Uses eid (agent employee_id) already defined at top of the
# function. Own sessions only; pending statuses; links to the agent detail page.
BLOCK = '''    # --- My coaching action plans ([notif] coaching section) ---
    if eid:
        try:
            conn = get_central_db()
            try:
                with conn.cursor(pymysql.cursors.DictCursor) as cur:
                    cur.execute("""
                        SELECT cs.id, cs.topic, cs.session_date,
                               s.schedule_name AS sup
                        FROM coaching_sessions cs
                        LEFT JOIN gsheet_employees s
                          ON s.employee_id = cs.supervisor_id COLLATE utf8mb4_unicode_ci
                        WHERE cs.agent_id = %s COLLATE utf8mb4_unicode_ci
                          AND cs.status IN ('pending','for_followup','pending_followup')
                        ORDER BY cs.session_date DESC LIMIT 100
                    """, (eid,))
                    cch = cur.fetchall()
            finally:
                conn.close()
            items = [{"message": f"Coaching: complete your action plan · {r['topic'] or 'Session'}",
                      "link": f"/coaching/my/{r['id']}",
                      "type": "Coaching",
                      "search": f"coaching action plan {r['topic'] or ''}".lower(),
                      "created_at": str(r['session_date']) if r['session_date'] else ''}
                     for r in cch]
            if items:
                sections.append({"title": "My coaching", "icon": "bi-easel2", "items": items})
                total += len(items)
        except Exception as e:
            app.logger.warning(f"[notif] coaching section failed: {e}")

    return jsonify({"sections": sections, "unread": total})'''

ANCHOR = '''    return jsonify({"sections": sections, "unread": total})'''


def main():
    print(f"[{'LIVE' if LIVE else 'DRY-RUN'}] add coaching section to /api/notifications\n")
    with open(APP, encoding="utf-8") as f:
        c = f.read()

    if MARKER in c:
        print("  SKIP — coaching notif section already present")
        return

    # The anchor return line appears in multiple endpoints; we need the one that
    # ends api_notifications. Slice from 'def api_notifications' to the next
    # '@app.route' after it, and operate only within that slice.
    start = c.find("def api_notifications():")
    if start == -1:
        print("  ERROR — api_notifications not found. Aborting.")
        return
    after = c.find("@app.route('/api/notifications/read'", start)
    if after == -1:
        print("  ERROR — could not bound the function. Aborting.")
        return

    seg = c[start:after]
    n = seg.count(ANCHOR)
    if n != 1:
        print(f"  ERROR — return anchor found {n}x in function (need 1). Aborting.")
        return

    new_seg = seg.replace(ANCHOR, BLOCK, 1)
    newc = c[:start] + new_seg + c[after:]

    try:
        ast.parse(newc)
    except SyntaxError as e:
        print(f"  ERROR — patched file fails to parse: {e}. Aborting, no write.")
        return

    print("  OK — coaching section will be inserted before the function's return")
    if LIVE:
        bak = f"{APP}.bak_{datetime.datetime.now():%Y%m%d_%H%M%S}"
        shutil.copy2(APP, bak)
        with open(APP, "w", encoding="utf-8") as f:
            f.write(newc)
        print(f"     backup: {bak}")
    print("\nDone." + ("" if LIVE else "  Re-run with --live to apply."))


if __name__ == "__main__":
    main()

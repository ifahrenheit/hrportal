"""
Mailing list sync from the employee sheet (cron).

The Cohere Employee List (MasterList tab) has a "Mailing List" column, e.g.
"gygcc3@cohere.ph, gygccall@cohere.ph". The Apps Script push copies it into
central_db.gsheet_employees.mailing_list. After each push this script makes
the cPanel groups match it, through the same code the portal's Mailing Lists
pages use (every change verified by reading the list back, every attempt a
row in mailing_list_audit, actor "Sheet sync").

Rules, per group address named in the column:
  add     Active/Training employees the sheet puts on it who aren't members.
  remove  members who are in the employee sheet and either are Separated or
          have a filled-in Mailing List cell that no longer names this group.
  never   blank cells (or only "#N/A"): that employee is left alone;
          addresses not in the employee sheet (managers, outside addresses,
          role inboxes) are never touched.
A group that is both a Mailman list and a forwarder (gygccall@) is synced as
the Mailman list; its forwarder targets are other groups, not people.

Safety: a group losing more than MAX_REMOVE_SHARE of its members, or more
than MAX_REMOVE members, in one run is skipped and reported (a bad push,
e.g. the column empty, would otherwise empty the lists).

    venv/bin/python scripts/mailing_list_sheet_sync.py          # dry run: print the plan, change nothing
    venv/bin/python scripts/mailing_list_sheet_sync.py --live   # apply, only if the sheet was pushed since the last live run
    venv/bin/python scripts/mailing_list_sheet_sync.py --live --force   # apply even if no new push
    --allow-big   skip the removal guard (one-off catch-up after a reviewed dry run)

Add-only: employees in ADD_ONLY_GROUPS (Finest, i.e. managers) are never
removed because their cell names other groups; only when Separated.

"Pushed since" = MAX(gsheet_employees.last_synced_at) differs from the value
stored in mailing_list_sheet_sync.state after the last live run.

Cron (root):
    */30 * * * *  cd /var/www/html/leavesystem && venv/bin/python scripts/mailing_list_sheet_sync.py --live >> mailing_list_sheet_sync.log 2>&1
"""

import os
import re
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from db_core import get_db_connection  # noqa: E402
from modules import mailing_lists as ml  # noqa: E402

STATE_FILE = os.path.join(ROOT, "mailing_list_sheet_sync.state")

# Sheet value -> real group address (renamed groups the sheet still uses).
ALIASES = {
    "gygl2@cohere.ph": "l2-agents@cohere.ph",
    "gygbo1@cohere.ph": "bo-cohere@cohere.ph",  # old forwarder, deleted 2026-10-28
}
# Named in the sheet but not managed here: soms@ is forwarded elsewhere (not a
# cPanel group); hr@ forwards HR's mailbox, so its recipients are set by hand.
IGNORED = {
    "soms@cohere.ph",
    "hr@cohere.ph",
}
# Groups whose members are only ever added (managers sit on several lists one
# sheet cell can't express). They are still removed everywhere when Separated.
ADD_ONLY_GROUPS = {"finest"}
MAX_REMOVE = 20
MAX_REMOVE_SHARE = 0.25

EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [mailing_list_sheet_sync] {msg}", flush=True)


def parse_cell(value):
    """Mailing List cell -> (set of group addresses, [unparseable bits]).
    Aliases applied, ignored groups dropped. "#N/A" and blanks give nothing."""
    groups, bad = set(), []
    for part in re.split(r"[,;\s]+", (value or "").strip()):
        p = part.strip().lower()
        if not p or p.startswith("#"):
            continue
        if not EMAIL_RE.match(p):
            bad.append(part)
            continue
        p = ALIASES.get(p, p)
        if p not in IGNORED:
            groups.add(p)
    return groups, bad


def build_people(rows):
    """{email: {"status", "groups", "filled", "name"}} from gsheet rows; when an
    email repeats (rehires), the Active/Training row wins. Also returns the
    unparseable cell bits as [(name, bit)]."""
    people, problems = {}, []
    ordered = sorted(rows, key=lambda r: (r.get("status") or "").strip() == "Separated")
    for r in ordered:
        email = (r.get("email") or "").strip().lower()
        if not email or email in people:
            continue
        groups, bad = parse_cell(r.get("mailing_list"))
        problems += [(r.get("schedule_name") or email, b) for b in bad]
        people[email] = {
            "status": (r.get("status") or "").strip(),
            "groups": groups,
            "filled": bool(groups),
            "add_only": (r.get("group_name") or "").strip().lower() in ADD_ONLY_GROUPS,
            "name": r.get("schedule_name") or email,
        }
    return people, problems


def plan_group(address, people, current):
    """(to_add, to_remove) for one group, per the rules in the docstring."""
    current = {c.lower() for c in current}
    to_add, to_remove = [], []
    for email, p in sorted(people.items()):
        active = p["status"] != "Separated"
        if active and address in p["groups"] and email not in current:
            to_add.append(email)
        elif email in current and (not active or (p["filled"] and not p.get("add_only")
                                                  and address not in p["groups"])):
            to_remove.append(email)
    return to_add, to_remove


def removal_blocked(n_remove, n_current):
    return n_remove > MAX_REMOVE or (n_current and n_remove / n_current > MAX_REMOVE_SHARE)


def load_rows():
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            c.execute("SHOW COLUMNS FROM gsheet_employees LIKE 'mailing_list'")
            if not c.fetchone():
                return None, None
            c.execute("""
                SELECT employee_id, schedule_name, email, status, group_name, mailing_list
                FROM gsheet_employees
                WHERE email IS NOT NULL AND email <> ''
            """)
            rows = c.fetchall()
            c.execute("SELECT MAX(last_synced_at) AS m FROM gsheet_employees")
            pushed = c.fetchone()["m"]
    finally:
        conn.close()
    return rows, str(pushed)


def read_state():
    try:
        with open(STATE_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def group_type(address, forwarders, mailman):
    if address in mailman:
        return "mailman"
    if address in forwarders:
        return "forwarder"
    return None


def main(argv):
    live = "--live" in argv
    force = "--force" in argv
    allow_big = "--allow-big" in argv
    mode = "LIVE" if live else "DRY RUN"

    rows, pushed = load_rows()
    if rows is None:
        log("gsheet_employees has no mailing_list column yet (Apps Script push not updated): nothing to do")
        return 1
    if live and not force and pushed == read_state():
        return 0  # no new push since the last live run; stay quiet

    people, problems = build_people(rows)
    for name, bit in problems:
        log(f"skipped unreadable value {bit!r} for {name}")

    named = sorted({g for p in people.values() if p["status"] != "Separated" for g in p["groups"]})
    forwarders, _ = ml.fetch_forwarders(refresh=True)
    mailman, _ = ml.fetch_mailman_lists(refresh=True)
    log(f"[{mode}] push {pushed}: {len(people)} employees, groups named: {', '.join(named) or 'none'}")

    failed = False
    for address in named:
        kind = group_type(address, forwarders, mailman)
        if not kind:
            log(f"{address}: not a forwarder or Mailman list on cPanel, skipped")
            continue
        try:
            current, meta = ml._current_emails(kind, address, refresh=True)
        except Exception as ex:
            log(f"{address}: could not read members ({ex}), skipped")
            failed = True
            continue
        if kind == "mailman" and not meta.get("complete"):
            log(f"{address}: member list read incomplete, skipped")
            failed = True
            continue
        to_add, to_remove = plan_group(address, people, current)
        if not to_add and not to_remove:
            log(f"{address} ({kind}, {len(current)} members): in sync")
            continue
        blocked = not allow_big and removal_blocked(len(to_remove), len(current))
        log(f"{address} ({kind}, {len(current)} members): "
            f"add {len(to_add)} {to_add}  remove {len(to_remove)} {to_remove}"
            + ("  REMOVALS BLOCKED (too many)" if blocked else ""))
        if not live:
            continue
        if to_add:
            for e, res, detail in ml.change_members(kind, address, "add", to_add):
                if res == "failed":
                    failed = True
                    log(f"  add {e}: FAILED {detail}")
        if to_remove and not blocked:
            for e, res, detail in ml.change_members(kind, address, "remove", to_remove):
                if res == "failed":
                    failed = True
                    log(f"  remove {e}: FAILED {detail}")
        if blocked:
            failed = True

    if live and not failed:
        with open(STATE_FILE, "w") as f:
            f.write(pushed)
    elif live:
        log("some changes failed or were blocked: will retry on the next run")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

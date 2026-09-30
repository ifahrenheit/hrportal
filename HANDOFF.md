# Handoff Log — leavesystem

## Project overview
- **App:** Cohere HR portal ("Leave4Day" / hrportal), Flask monolith [app.py](app.py) plus blueprints in `modules/`, `blueprints/`, `break_log/`, `coaching.py`, `memos.py`. Frontend: Jinja + Bootstrap 5 + vanilla JS, Lucide icons.
- **DBs:** `orangehrm2` via `get_db()` (OrangeHRM tables, leave balances) and `central_db` via `get_db_connection()` / `get_central_db()` (portal data, `gsheet_employees`). Raw PyMySQL with DictCursor.
- **Deploy:** this checkout *is* production (`hrportal.cohere.ph`). systemd `leavesystem` runs `venv/bin/python app.py` on 127.0.0.1:5001 behind Apache. Restart with `sudo systemctl restart leavesystem`.
- **Cron:** tardiness/overbreak/undertime notify scripts and others under root's crontab (see CLAUDE.md).

---

## 2026-10-01 — Uncommitted live work: PIM approve/reject, tardiness period buttons, requirements
Commit: `b20e821` (changes below are **uncommitted** on top of it) · Branch: `main`

### What was built / changed
- **PIM profile: Leaves + Requests tabs with approver actions.** `app.py` (+273 lines), `templates/pim/profile.html` (+509 lines)
  - New helpers `_fetch_pim_requests`, `_request_scope_data`, `_leave_scope_data`, `_is_pim_approver_of`.
  - New route `POST /pim/<employee_id>/request-action` (`pim_request_action`) approves or rejects one OT / RDW / CWS / FTS / Magic CWS request.
  - Tabs show pending badges, status/type filter pills and summary cards. A confirm modal is `#pimActionModal`.
  - Leaves go through the existing `/api/leave/action`.
- **Tardiness page buttons.** `templates/tardiness.html`
  - "Current period" now shows the period in progress from its start to today. It used to show the last completed period.
  - "Today" now shows the actual current date. The old 1 PM cutoff logic that jumped back 1–2 days is gone.
- **requirements.txt** now lists deps that were installed but missing: bcrypt, reportlab, openpyxl, python-keycloak, mysql-connector-python, and chromadb, python-docx and pypdf for `rag/rag_api.py`.

### Why / decisions
- `pim_request_action` is deliberately narrower than `/api/approvals/action`. The caller must be this employee's approver, the request must belong to the employee, and the `UPDATE ... AND status='Pending'` guard means a double click or a second approver gets a 409.
- Magic CWS approval creates the balance row if it's missing (`_init_magic_cws_balance`) and then adds `deduction_hours` to `magic_cws_balance.used_hrs`.
- Tardiness "Today" can show incomplete counts for the day in progress. Only people who have already punched in late appear.

### Config / environment
- No new .env keys, no DB schema changes.
- Backup: `app.py.bak.20260928231815`.

### Deploy / run notes
- `app.py` was last edited 2026-09-30 21:53 and the service was restarted 2026-09-30 22:04. **The changes are live but not committed.**

### How to verify
- Open `/pim/<employee_id>` as that employee's approver. Check the Leaves and Requests tabs, then approve a pending request and confirm the badge count drops.
- Open `/tardiness` and click Current period and Today. The date range should end today.

### Open items / next steps
- [ ] Review and commit the four modified files (`app.py`, `requirements.txt`, `templates/pim/profile.html`, `templates/tardiness.html`).
- [ ] Check that non-approvers get a 403 on `/pim/<id>/request-action` and that Magic CWS `used_hrs` goes up by exactly one deduction per approval.
- [ ] From memory: recheck inventory ledger reconciliation at the end of Sept 2026 (due now), and confirm the material release sub-unit (Bond Paper) stock-deduction fix with real-world use.

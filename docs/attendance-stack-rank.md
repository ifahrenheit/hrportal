# Attendance Stack Rank

## 1. Access & Navigation

| | |
|---|---|
| Route | `GET /admin/attendance-stack-rank` — `admin_attendance_stack_rank()` in `app.py` (~line 14693) |
| Template | `templates/admin/attendance_stack_rank.html` |
| Permission required | `can_absences` session flag, or `is_admin` |
| Auth check | Inline in the route body (no decorator): `session.get('is_admin')` or `session['permissions']['can_absences']`, else redirect to `dashboard` |
| Entry point | Sidebar → **Attendance** (lands on Absence Report) → **Attendance Stack Rank** tab |

This page is part of a five-tab "Attendance" report family — Absence Report,
Attendance Grid, AWOL Report, Undertime Report, Attendance Stack Rank — sharing an
identical Bootstrap tab bar. Each tab is a plain link (full page reload, no AJAX), so
users can switch between all five without returning to the sidebar.

## 2. What it shows

A single flat table ranking every employee by attendance percentage over the selected
date range — highest attendance first. Unlike AWOL/Undertime, this isn't about
flagging problems; it's a leaderboard-style view for comparing attendance across the
whole org (or a filtered slice of it).

### Columns

Rank, Employee, Employee ID, Group, Team Lead, Days Scheduled, Days Present,
Attendance % — color-coded green (≥90%), amber (≥75%), red (<75%). The same
color coding is applied to the "Overall Attendance Rate" summary stat tile.

### Filters & UI

- Date range (`date_from` / `date_to`) with prev/next "payroll period" navigation
  buttons, a **Current Period** button, and a **Today** button.
- **Group**, **Team Lead**, and **Employee** (name/ID) filters.
- **Include inactive** toggle — includes employees outside
  `gsheet_employees.status = 'Active'`.
- **Clear** button to reset filters.
- **Copy Table** — copies the visible table as tab-separated text to the clipboard.
- Client-side column sorting (server sorts by rank by default; click any
  `<th class="sortable">` to re-sort in the browser).

## 3. How the score is computed

For each employee, iterate every scheduled (non-rest-day, not past `exit_date`) row
in the date range:

- `scheduled_count += 1` by default.
- If the attendance map shows `Present`, `FTS IN`, or `FTS OUT` → `present_count += 1`.
- Else, if a leave record exists for that date:
  - Leave code `PL` or `ML` (via `get_leave_code()`) → `scheduled_count -= 1`
    (fully excluded — doesn't count against attendance at all).
  - Any other leave type → counts as scheduled-but-absent (stays in the
    denominator, no present increment).
- Else (no punch, no leave) → counted as absent (denominator increments, no present
  increment).

```
attendance_pct = round(present_count / scheduled_count * 100, 1)
```
(`None` if `scheduled_count == 0`.)

### Ranking

Employees with `scheduled_count > 0` are sorted by
`(-attendance_pct, -scheduled_count, name)` — highest % first, ties broken by more
scheduled days, then name — and assigned `rank = 1, 2, 3, …`. Employees with
`scheduled_count == 0` (e.g., no shifts scheduled in range) are listed unranked at the
bottom, sorted by name.

The team-wide **average** shown in the summary is
`sum(present_count) / sum(scheduled_count)` across ranked employees — a weighted
average, not a mean of individual percentages.

## 4. Config / thresholds

- Only `PL` and `ML` leave codes get full denominator exclusion; every other leave
  type (VL, SL, LWOP, COL, etc.) still counts as a scheduled day that wasn't attended.
- Leave-status exclusion list: `rejected`, `cancelled`, `deleted` (case-insensitive) —
  any other status counts as covering the date, per the leave query below.
- `TODAY_CUTOFF_HOUR = 13` (1 PM) — the "Today" button and default date range treat a
  day as incomplete until 1 PM.
- Payroll period boundaries (8th–22nd, 23rd–end of month) come from
  `helpers/payroll_period.py`; the report defaults to the most recently **completed**
  period, not the in-progress one.

## 5. Shared helpers used

- **`build_attendance_map(personids, window_start, window_end, shift_starts=None)`**
  (`app.py`) — pairs `in`/`out` punches from `dailytimerecordsfiltered` into
  per-employee/date status (`Present`, `FTS IN`, `FTS OUT`). Never returns "Absent" —
  absence is inferred from a missing entry. Handles overnight shifts crossing
  midnight and caps punch pairing at 18 hours apart.
- **`parse_shift_time(shift_str)`** — parses shift strings like `"10pm-7am"` into
  start/end hours.
- **`get_leave_code(leave_type_name)`** — maps a leave type name to a short code
  (VL, SL, LWOP, PL, COL, ML, else "LV"); used here to detect PL/ML for exclusion.
- **`get_default_payroll_period()` / `get_payroll_period_for_date()`**
  (`helpers/payroll_period.py`).

Note: this logic is intentionally duplicated rather than shared with AWOL Report /
Undertime Report, to avoid one report's changes risking the others. Stack Rank's
scheduled-employee query differs from the others by also accepting **Group**, **Team
Lead**, and **Employee** filters, and an `include_inactive` toggle that drops the
`g.status = 'Active'` clause when set.

## 6. Data sources

| Table | DB | Used for |
|---|---|---|
| `employee_schedules`, `gsheet_employees` | central | scheduled shifts, group/team lead, status/exit_date |
| `userdata` | central | resolving `personid` for punch lookups |
| `dailytimerecordsfiltered` | central | raw clock in/out punches |
| `leave4day_requests`, `ohrm_leave_type`, `hs_hr_employee` | app | filed leave, used to detect PL/ML exclusion |

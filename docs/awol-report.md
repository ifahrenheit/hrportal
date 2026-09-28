# AWOL Report

## 1. Access & Navigation

| | |
|---|---|
| Route | `GET /admin/awol-report` — `admin_awol_report()` in `app.py` (~line 10809) |
| Template | `templates/admin/awol_report.html` |
| Permission required | `can_absences` session flag, or `is_admin` |
| Auth check | Inline in the route body (no decorator): `session.get('is_admin')` or `session['permissions']['can_absences']`, else redirect to `dashboard` |
| Entry point | Sidebar → **Attendance** (lands on Absence Report) → **AWOL Report** tab |

This page is part of a five-tab "Attendance" report family — Absence Report,
Attendance Grid, AWOL Report, Undertime Report, Attendance Stack Rank — sharing an
identical Bootstrap tab bar. Each tab is a plain link (full page reload, no AJAX), so
users can switch between all five without returning to the sidebar.

> **Not to be confused with:** the separate **Holiday AWOL** admin page
> (`/admin/holiday-awol`, permission `can_holiday_awol`). That page is about the
> synthetic "AWOL" leave-type entitlement applied on holidays — an unrelated feature.

## 2. What it shows

Employees who were scheduled to work, did not clock in, and have **no explanation on
file** (no approved OT, no suspension, no filed leave) for that date. Rows are grouped
into a card per `group_name` (department/group), each with a header showing the count
for that group, plus a summary pill with the total AWOL count and the date range
covered.

A separate warning panel lists **"Unverified"** scheduled absences — scheduled rows
that couldn't be matched to a `personid` in `userdata`. These are still checked
against leave records but are never flagged AWOL, since there's no confirmed identity
behind the schedule row.

### Columns

Employee, Employee ID, Team Lead, Absent Date, Day of Week, Shift (badge), Status
(always an "AWOL" red badge), IR (linked badge to a matching Incident Report if one
exists for that employee/date, else `—`).

### Filters & UI

- Date range (`date_from` / `date_to`) with prev/next "payroll period" navigation
  buttons, a **Current Period** button, and a **Today** button.
- **Copy Table** — copies the visible table as tab-separated text to the clipboard.
- Client-side column sorting — click any `<th class="sortable">` to re-sort in the
  browser (no server round-trip).

## 3. How AWOL is determined

The route pulls all scheduled, active, non-rest-day shifts in the date range, then
works through a sequence of exclusions ("Steps 1–6" per inline comments in `app.py`):

1. **Scheduled** — join `employee_schedules` + `gsheet_employees` (+ `userdata` for
   `personid`) for active employees, non-rest-day, with a shift time, in the date
   range.
2. **Punched** — build an attendance map from raw punches
   (`dailytimerecordsfiltered`, via the shared `build_attendance_map()` helper). Any
   day with a punch record (`Present`, `FTS IN`, `FTS OUT`) is excluded — not AWOL.
3. **Approved OT** — a day covered by an `ot_requests` row with `status = 'Approved'`
   (±1 day window) is excluded.
4. **Suspension (SUS)** — a day with a `SUS` code in `absence_records` is excluded;
   it's a fully-explained absence.
5. What's left ("no punch, no OT, no suspension") is a **candidate** AWOL day.
6. **Leave coverage** — a candidate covered by any filed leave in
   `leave4day_requests` (any type, any status except rejected/cancelled/deleted) is
   excluded.
7. Whatever survives is flagged **AWOL**. Employees with no `personid` mapping are
   never accused — they land in the "Unverified" list instead.
8. Matching **Incident Reports** (`incident_reports`, same employee + exact date,
   most-recently-created wins) are attached and linked from the IR column.

## 4. Config / thresholds

- `TODAY_CUTOFF_HOUR = 13` (1 PM) — the "Today" button and default date range treat a
  day as incomplete until 1 PM, matching the existing absence-report cron cutoff.
- Leave-status exclusion list: `rejected`, `cancelled`, `deleted` (case-insensitive) —
  any other status counts as covering the date.
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
- **`get_default_payroll_period()` / `get_payroll_period_for_date()`**
  (`helpers/payroll_period.py`).

Note: this logic is intentionally duplicated rather than shared with Undertime
Report / Attendance Stack Rank, to avoid one report's changes risking the others.

## 6. Data sources

| Table | DB | Used for |
|---|---|---|
| `employee_schedules`, `gsheet_employees` | central | scheduled shifts, group/team lead |
| `userdata` | central | resolving `personid` for punch lookups |
| `dailytimerecordsfiltered` | central | raw clock in/out punches |
| `ot_requests` | central | approved OT (AWOL exclusion) |
| `absence_records` | central | `SUS` suspension codes (AWOL exclusion) |
| `incident_reports` | central | IR links on the AWOL Report |
| `leave4day_requests`, `ohrm_leave_type` | app | filed leave (AWOL exclusion) |

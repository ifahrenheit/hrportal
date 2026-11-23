# Payroll Readiness

**Analytics › Payroll Readiness**, `/admin/payroll-readiness`. Open it before running payroll for
a cutoff to see what in that payroll period is still open, and who it's waiting on.

Code: [modules/payroll_readiness.py](../modules/payroll_readiness.py),
[templates/payroll_readiness/index.html](../templates/payroll_readiness/index.html).
Tests: `tests/test_payroll_readiness.py`.

## Who can open it

Admins, and sub-admins with **Reports** (`can_reports`) or **OT Hours** (`can_ot_hours`). No new
permission was added; change `ACCESS_PERMS` in the module (and the two nav links) to adjust.

## Periods

Payroll periods are 8th–22nd and 23rd–7th (`helpers/payroll_period.py`). The page opens on the
**most recently closed** period, the one payroll is being run for. Use the arrows to move
between periods, or "Current period" to see the one in progress. `?start=YYYY-MM-DD` opens the
period containing that date.

## What it lists

| Section | Source | "Waiting on" |
|---|---|---|
| File requests still pending | `ot_requests`, `fts_requests`, `cws_requests`, `rd_requests` (RDW), `magic_cws_requests` with status Pending, not deleted, work date in the period (CWS/Magic CWS: original or new date) | The approver, from `get_approver_names()`, the same routing as File Requests › Approvals |
| Missing punches with no FTS filed | `fts_pathb_incidents`, category `fts`, review pending, `fts_filed` empty | The employee (file an FTS), or HR dismisses it in FTS Review |
| Leave not settled | `leave4day_requests` in the period: status `pending`, SL with `sl_hr_stage = 'pending_hr'`, or `credit_pending = 1` | Approver / HR / the next year's balance |

- **Missing punches already covered by an FTS are left out.** The FTS engine fills `fts_filed` only on
  its daily run, so a just-filed FTS would otherwise count twice. Matching follows the engine: IN is
  filed on the shift date; OUT on the shift date or the next day (overnight shifts). A pending FTS
  still shows under File requests.
- **Earlier periods:** a note counts requests still pending whose work date is before the period.
  If they're approved now, they need a payroll adjustment.
- "Days open" is counted from when the item was filed (or flagged by the FTS engine). It turns red at
  3+ days.

## Export

**Export CSV** downloads every open item for the period (one file, with a Section column).
Each table also has **Copy table**, which pastes into Excel or Google Sheets as columns.

## History

Built 2026-11-24. It replaces the cutoff-day emails to payroll@cohere.ph
(`cohere_dashboard/send_{ot,rdw,cws,fts}_email.php`). Their cron lines had a typo'd log path, so
they never ran after May 2025, and they were removed on 2026-11-23.

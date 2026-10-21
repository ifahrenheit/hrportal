---
name: year-end-rollover
description: Prepares leave entitlements per company policy - the December rollover (12 VL after the 1st deployment anniversary, 12 SL after regularization, Carry-Over = unused VL up to 6, LWOP for all) and the pro-rated VL/SL added mid-year at anniversary or regularization - as reviewed dry runs, then charges leave already filed. Use for "load 2027 balances", "year-end leave rollover", "who is due VL/SL this month", or checking entitlements are complete. Jan 1 rollover and daily pro-rated grants run from cron; anything else needs an explicit go-ahead.
tools: Read, Grep, Glob, Bash
---

# Year-End Rollover

## Role
Once a year (in December, before Jan 1) you prepare the next year's leave entitlements in
`orangehrm2.ohrm_leave_entitlement`, the table both the portal and OrangeHRM read. You produce a
reviewable plan, then a one-time script that dry-runs by default; the user runs it with `--live`
after HR signs off. This is the live production database; there's no staging copy.

## Rules (from HR, Oct 2026; ask if anything looks different this year)
Balances are in 8-hour days (12 days = 96 hours).

Company policy (Paid leaves 3.1 / Sick Leave 3.2): at most 12 VL and 12 SL per calendar year,
earned at 1 VL and 1 SL per month. **VL starts at the 1st anniversary of deployment**, **SL at
regularization**. In the year it starts it's pro-rated: 1 day per month from that month to December,
where a start date on or before the 15th counts that month and a date after the 15th starts the
next month. Example: anniversary Mar 3 → 10 (Mar–Dec); Mar 24 → 9 (Apr–Dec).

| Leave type | Who | Jan 1 entitlement for the new year |
|---|---|---|
| Vacation Leave | 1st deployment anniversary is **before** the new year | **12** |
| Vacation Leave | anniversary falls **in** the new year | **none on Jan 1**; pro-rated VL is added on the anniversary (see "During the year") |
| Sick Leave | regularized **before** the new year | **12** |
| Sick Leave | regularizes **in** the new year | **none on Jan 1**; pro-rated SL is added at regularization |
| Carry-Over Leave | everyone with VL this year | **min(unused VL, 6)**: 8 unused → 6 (the other 2 go to cash conversion), 4 unused → 4, 0 unused → 0 |
| Leave Without Pay (LWOP) | everyone active | the usual default row (99); LWOP has no limit when filing, the row only tracks usage |
| Magic Leave, Parental leave | | **don't create**. They're granted case by case during the year |

- Deployment date = `central_db.gsheet_employees.operational_date`. **Regularization =
  operational date + 6 months** (confirmed by the user; 2026 data matches: deployed Aug 4 → SL
  from Feb; Sep 15 → Mar 15; Oct 27 → from May). List anyone without an `operational_date`.
- "Unused VL" = this year's VL `no_of_days − days_used`, measured after the user confirms every
  current-year filing is final (pending leave still counts as used, as it does everywhere else).
- Carry-Over from the *previous* year doesn't roll again; only unused VL feeds next year's CO.
- 2026 balances follow this rule (103 of 137 employees: CO = min(unused 2025 VL, 6)).
- Only employees with `central_db.gsheet_employees.status = 'Active'` (or Training, if HR says so).
- In 2026 a few pro-rated VL amounts were one day off the rule (likely set by hand). Report
  differences you find instead of "correcting" past years.

## Cash conversion and Carry-Over expiry (policy 3.3.1 / 3.3.2)
- Carry-Over Leave can't roll past the succeeding year: next year's CO comes only from this
  year's unused VL, never from this year's unused CO.
- **By June 15:** unused VL above 6 from the previous year is convertible to cash.
  **On September 15:** unused Carry-Over from the prior year is eligible for conversion.
- Both require: no AWOL during the year (AWOL leave type in `leave4day_requests`), actively
  employed at conversion (`gsheet_employees.status = 'Active'`), and not resigning or serving a
  notice period (ask HR; it isn't recorded in the portal). Before each date, list who qualifies,
  how many days, and who's excluded and why. HR/payroll does the conversion; you don't change
  balances for it.

## During the year (pro-rated grants)
Automated: `scripts/prorated_leave_grants.py --live` runs daily at 1:15 AM (log
`prorated_leave_grants.log`). It creates the pro-rated VL/SL row once the credit date is reached,
never changes an existing row, and lists grants that were already overdue when it started
(2026-10-22), rows that differ from the policy amount, and what's due in the next 31 days.
The user cross-checks each grant against HR's email; report, don't adjust, unless asked.
When someone leaves, policy says leave used beyond what they'd earned (1 per month) is
refunded from final pay; you can compute earned vs used for HR, but HR handles the refund.

## Steps
1. **Health check of the current year first.** Unused VL is only right if `days_used` is right.
   Run the leave-auditor checks (balances vs filed leave), or ask the user to, and resolve gaps
   before computing Carry-Over.
2. **Plan, read-only.** For every active employee list: tenured yes/no and why, this year's VL
   allowance/used/unused, next year's VL, CO and LWOP, and whether a next-year row already exists
   (never create a second row for the same employee/type/year; report existing ones and how they differ).
   Summarise counts per leave type and the exceptions (no joined date, separated after Dec 1,
   negative or over-allowance balances, people on parental leave).
3. **Script.** `scripts/manual_fixes/rollover_<year>.py`, dry run by default, `--live` to write,
   one transaction, `INSERT` only (`entitlement_type = 1`, `deleted = 0`, `from_date` Jan 1,
   `to_date` Dec 31, `credited_date = NOW()`, `note = 'Year-end rollover <year>'`,
   `created_by_id` = the admin's `ohrm_user.id`), skip rows that already exist, abort on any
   surprise. Log every row to `leave_balance_sync.log` (gitignored) and to `leave4day_entitlement_log`
   (`action = 'add'`, `old_days` NULL). The script names employees, so add it to `.gitignore`.
4. **After the live run.** Run `venv/bin/python scripts/apply_pending_leave_credits.py` (dry run)
   so leave already filed for the new year (`leave4day_requests.credit_pending = 1`) shows what it
   will charge; the 1:30 AM cron charges it, or the user runs it with `--live`. Flag any
   `OVER ALLOWANCE` lines: those filings were checked against the old year's balance.
5. **Verify.** Every active employee has next-year VL/CO (if tenured) and LWOP; no duplicates; spot
   check a few people on Admin › Entitlements (year = new year) and in OrangeHRM.

## Privacy
The repo is public: plans and lists with names stay in chat or gitignored files. Never commit them.

## Query gotchas
- `hs_hr_employee` is utf8mb3; join `gsheet_employees` with
  `CONVERT(h.employee_id USING utf8mb4) COLLATE utf8mb4_unicode_ci = g.employee_id COLLATE utf8mb4_unicode_ci`.
- Leave type IDs come from `ohrm_leave_type` (VL 1, SL 2, LWOP 3, Carry-Over 4, Magic 5,
  Parental 6, AWOL 7 as of 2026); look them up by name, don't hard-code. Never use
  `leave4day_entitlements` / `leave4day_leave_types` (retired, IDs don't match).
- Connect with `helpers/orangehrm_db.get_orangehrm_connection()` after
  `load_dotenv('/var/www/html/leavesystem/.env')`.

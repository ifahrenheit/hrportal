---
name: year-end-rollover
description: Prepares next year's leave balances in December for the HR portal - 12 VL for tenured employees, Carry-Over from unused VL above 6, LWOP for everyone, nothing for new hires - as a reviewed dry run, then charges leave already filed for the new year. Use for "load 2027 balances", "year-end leave rollover", or checking that next year's entitlements are complete. Never writes without an explicit go-ahead.
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

| Leave type | Who | Next year's entitlement |
|---|---|---|
| Vacation Leave | Tenured (employed before the new year starts) | **12** |
| Carry-Over Leave | Tenured | **max(0, unused VL − 6)**: 8 unused → 2, 5 unused → 0, 12 unused → 6 |
| Leave Without Pay (LWOP) | Everyone active | the usual default row (99); LWOP has no limit when filing, the row only tracks usage |
| Sick Leave | Tenured | 12 in past years. Confirm with HR before creating it |
| VL / SL | Newly hired (not yet tenured) | **none**. They may only file LWOP. In 2026, 2025 hires got prorated VL (6–11 days) added mid-year, apparently at regularization. That's a separate, later step, so ask HR rather than guessing |
| Magic Leave, Parental leave | | **don't create**. They're granted case by case during the year |

- "Unused VL" = this year's VL `no_of_days − days_used`, measured after the user confirms every
  current-year filing is final (pending leave still counts as used, as it does everywhere else).
- Carry-Over from the *previous* year doesn't roll again; only unused VL feeds next year's CO.
- **Ask before running** who counts as tenured if it isn't simply "hired before Jan 1 of the new
  year" (`hs_hr_employee.joined_date`, cross-checked with `gsheet_employees.training_date`/`operational_date`).
  Some rows have no `joined_date`; list them for HR instead of deciding.
- Only employees with `central_db.gsheet_employees.status = 'Active'` (or Training, if HR says so).

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

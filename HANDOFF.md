# Handoff Log — leavesystem

## Project overview
- **App:** Cohere HR portal ("Leave4Day" / hrportal), Flask monolith [app.py](app.py) plus blueprints in `modules/`, `blueprints/`, `break_log/`, `coaching.py`, `memos.py`. Frontend: Jinja + Bootstrap 5 + vanilla JS, Lucide icons.
- **DBs:** `orangehrm2` via `get_db()` (OrangeHRM tables, leave balances) and `central_db` via `get_db_connection()` / `get_central_db()` (portal data, `gsheet_employees`). Raw PyMySQL with DictCursor.
- **Deploy:** this checkout *is* production (`hrportal.cohere.ph`). systemd `leavesystem` runs `venv/bin/python app.py` on 127.0.0.1:5001 behind Apache. Restart with `sudo systemctl restart leavesystem`.
- **Cron:** tardiness/overbreak/undertime notify scripts and others under root's crontab (see CLAUDE.md).

---

## 2026-10-22 (3) — Timestamp Records page (raw punches from Google Drive)
Commit: `c5141e3` · Branch: `main`

### What was built / changed
- Read-only page `/admin/timestamp-records` + JSON `/admin/timestamp-records/data?from=YYYY-MM-DD&to=YYYY-MM-DD[&refresh=1]` — `modules/timestamp_records.py`, `templates/timestamp_records/index.html`.
- Drive reader `helpers/drive_timestamps.py`: Cohere Raw Logs / YYYY / YYYYMM / YYYYMMDD-.csv; paginated listing; range loading across month/year folders; duplicate file names → newest upload only (yellow notice); bad rows/files reported, never dropped silently; memory + disk cache (`instance/drive_timestamps/`, gitignored) keyed on file id + modifiedTime/md5/size.
- UI: payroll-period dropdown (portal cut-offs 8–22 / 23–7, defaults to last completed) + custom From/To (max 62 days), search, IN/OUT filter, pagination, Copy table (TSV of all matching rows), loading/empty/error states.
- New sub-admin permission `can_timestamp_records` (Analytics & Reports): `permission.py`, `templates/admin/sub_admins.html`, app.py session dict + both sub-admin saves + SELECT; nav links in `nav/_attendance_tabs.html`, `_sidebar.html`, `_topnav.html`.
- Tests: `tests/test_timestamp_records.py` (51, incl. one live Drive read).

### Why / decisions
- CSV header is `companyid,Day,Time,Type` — no names; First/Last Name come from OrangeHRM `hs_hr_employee` by employee_id (active row wins; unmatched → blank + notice).
- Each daily file = one shift date; overnight clock-outs carry the next Day. Ranges select by **file (shift) date**, so the last shift's overnight out is included.
- Auth switched from API key to a service account: Google throttled API-key downloads of link-shared files (403 "Sorry…" HTML page) after ~150 test downloads. Auth mode is chosen by config only, never auto-fallback.
- Year folders reached via hardcoded `YEAR_FOLDERS` (2026, 2025) because the service account is shared on the 2026 folder, not the root. Folder access is checked first (files.list on an unshared folder returns empty, not an error).
- Dedicated permission (user's choice) instead of reusing can_absences/can_tardiness/can_reports.

### Database changes
```sql
-- orangehrm2 (36 rows, all 0 after)
ALTER TABLE leave4day_sub_admins ADD COLUMN can_timestamp_records TINYINT(1) NOT NULL DEFAULT 0;
```

### Config / environment
- `.env`: `GOOGLE_DRIVE_SA_KEY_FILE` (path to service-account JSON in `/etc/leavesystem/`, root-only 600), `GOOGLE_DRIVE_API_KEY` (fallback only, unused while SA is set).
- Service account `timestamp-drive-reader@wpforms-smtp-501221.iam.gserviceaccount.com`, scope drive.readonly; Drive API enabled in that project.
- app.py backup: `app.py.bak.20261022231254`. Service restarted by the user at 23:28.

### How to verify
- `https://hrportal.cohere.ph/admin/timestamp-records` (admin, or sub-admin with the new checkbox) → Sep 23 – Oct 7 2026 = 15 files, 3,913 punches.
- `venv/bin/python -m unittest tests.test_timestamp_records -v`

### Open items / next steps
- [x] Service-account key rotated 2026-10-22: new key `898730ea…` in `/etc/leavesystem/`, `.env` points at it, service restarted 23:53; old key `fd194495…` deleted in Google (sign-in now rejected) and its file removed. Old `GOOGLE_DRIVE_API_KEY` deleted in Google and removed from `.env`.
- [x] 2025 dropped (owner has left, can't be shared): page covers 2026-01-01 onward (`FIRST_DATE`), 2025 removed from `YEAR_FOLDERS`.
- [ ] Continuity risk: root "Cohere Raw Logs" is owned by the departed gmail.com account and is reached only via anyone-with-link; 2026 folder is owned by a getyourguide.com account. Before 2027: create a company-owned home (ideally a Shared Drive), share it with the service account, add the 2027 folder id to `YEAR_FOLDERS` in `helpers/drive_timestamps.py`.
- [ ] Tick "Timestamp Records" on the Sub-Admins page for whoever needs it.
- [ ] Confirm payroll period start (portal says 23rd; user mentioned "Sept 22").
- [ ] Pre-existing failing tests (fail on HEAD too): test_awol_report / test_undertime_report empty-state + leave-covered cases.

---

## 2026-10-22 (2) — 2027 balances loaded; leave cycle automated per policy
Commit: `ae19f18` · Branch: `main` (commits `f92d855`..`ae19f18`)

### What was built / changed
- `helpers/leave_policy.py`: policy 3.1–3.3 rules (VL from 1st anniversary of `operational_date`, SL from operational date + 6 months, pro-rated 1/month with a 15th-of-month cutoff, Carry-Over = min(unused VL, 6)).
- `scripts/year_end_rollover.py <year>`: VL/SL/LWOP + Carry-Over for a year; `--skip-co`, `--only-co`, `--reg-file`, `--csv`; dry run by default; skips existing rows.
- `scripts/prorated_leave_grants.py`: daily pro-rated VL/SL grants; never changes existing rows; lists overdue (pre-2026-10-22) grants, rows that differ from policy, next 31 days.
- Ran 2027 rollover with `--skip-co --live`: 544 rows (VL/SL/LWOP); then charged the pending 2027 VL.
- Dashboard: LWOP card removed. VL/COL notice is 7 **calendar** days (HR's choice; working-day version reverted).
- Agents: `year-end-rollover.md` follows the policy; leave-auditor / employee-sync-auditor added earlier.
- `ae19f18`: a move to Numa restarts the VL anniversary (`gsheet_employees.moved_to_numa`, `vl_deployment()`); Arctic moves don't; SL unchanged. Column is empty — only two Numa staff were moved (HR counts from 2025-07-08), everyone else was hired directly.
- First HR email cross-check: the two Numa staff's 6 VL was already credited (2026-07-28); nothing to do.

### Config / environment (root crontab)
- `15 1 * * *` prorated_leave_grants.py --live >> prorated_leave_grants.log
- `30 1 * * *` apply_pending_leave_credits.py --live (existing)
- `30 0 1 1 *` year_end_rollover.py $(date +%Y) --live >> year_end_rollover.log (full rollover every Jan 1; on Jan 1, 2027 only Carry-Over is left: 136 rows in the dry run)

### Why / decisions
- Entitlement corrections wait for HR's email; automated grants are cross-checked against those emails.
- `promotion_date` in the sheet is promotions, not regularization.

### How to verify
- `venv/bin/python scripts/prorated_leave_grants.py` (dry run) · `/admin/entitlements?year=2027`
- `venv/bin/python -m unittest tests.test_leave_policy tests.test_advance_notice tests.test_work_mode_lwop tests.test_leave_credits`

### Open items / next steps
- [ ] Check the first nightly logs: `prorated_leave_grants.log`, `apply_pending_leave_credits.log`.
- [ ] HR cross-check list (names in chat/logs, not here): 3 SL grants overdue before automation, 4 allowances below policy, first automatic SL grants on Nov 1 and Nov 4.
- [ ] New next-year filings are still balance-checked against the current year's balance.
- [ ] 2025 balances not reviewed; 2025 report still in git history (`e072dfe`).
- [ ] Dashboard redesign on hold (`lovable_preview/`).

---

## 2026-10-22 — Leave balances: one table, 2026 reconciled, work-mode/LWOP rules, new agents
Commit: `6c3af9b` · Branch: `main` (commits `bea2379`..`6c3af9b`)

### What was built / changed
- **Dashboard:** "Coming up" birthdays card in the side column (`_dashboard_extras`, `templates/dashboard.html`); recent-request dates show the year (`_fmt_range(year=True)`); LWOP balance card removed, LWOP-only staff see "No paid leave balance yet. You can file LWOP."
- **Incident Reports / Memos:** Group filter (whole account e.g. "All Numa", or single group) — `modules/incident_reports.py`, `memos.py` (memos match by employee_id, else email).
- **Leave History** (`/history`): one line per row, notes expand on click; paging 25/50/100 with filters kept; employee search also matches full name / employee ID.
- **One entitlement table:** Admin › Entitlements reads/writes `ohrm_leave_entitlement` directly (current-total hint, confirm before replace, Edit buttons, delete only if unused, change log). `leave4day_entitlements` ("Portal DB") retired — its type IDs (`leave4day_leave_types`) didn't match `ohrm_leave_type`. Entitlement Source toggle and File Leave "[Portal]" options removed.
- **Future-year leave:** filing into a year with no balance marks days `credit_pending`; `helpers/leave_credits.py` + `scripts/apply_pending_leave_credits.py` charge them once the row exists (also on saving a balance). Reject/cancel skip refunds for never-charged days.
- **Work mode** (`get_work_mode()`): individual setting > Numa/SGA/TL are 5-day (`is_five_day_group`) > global default. Magic CWS uses the same rule.
- **LWOP unlimited:** always offered on File Leave / File for Employee ("no limit"), no balance check (`with_unlimited_lwop`).
- **VL/COL notice:** 7 calendar days; error names the earliest date (`advance_notice_min_date`).
- **Agents:** `.claude/agents/leave-auditor.md`, `year-end-rollover.md`, `employee-sync-auditor.md`.
- `lovable_preview/` saved (on-hold top-nav redesign, mock data, README).

### Why / decisions
- Leave day = hours ÷ 8; balances in 8-hour days. Some agents moved to 4×10 h around 2026-03-23; Numa, SGA, TL stay 5-day; parental leave reverts to 5-day. "Use remaining balance" caps at 0 (an 8 h day on a 10 h shift can be correct).
- Policy 3.1–3.3 (VL from 1st deployment anniversary, SL from regularization, pro-rated with a 15th-of-month cutoff; Carry-Over = unused VL up to 6). HR chose **calendar** days for the 7-day notice and leaves the ">3 consecutive days" rule to approvers.
- Repo is public: no employee names/IDs in committed files. 2025 reconciliation report and two name-listing fix scripts are gitignored (report is still in git history from `e072dfe`).

### Database changes
```sql
-- orangehrm2 (run by the user)
ALTER TABLE leave4day_requests
  ADD COLUMN credit_pending TINYINT(1) NOT NULL DEFAULT 0,
  ADD INDEX idx_credit_pending (credit_pending);
CREATE TABLE leave4day_entitlement_log (id, entitlement_id, emp_number, leave_type_id, year,
  action, old_days, new_days, changed_by, changed_at) -- see app.py _log_entitlement_change
```
- Data (2026 only): migrated portal entitlement rows into OrangeHRM (`scripts/manual_fixes/retire_portal_entitlements.py`); reconciled `days_used` to filed leave for all 2026 balances; 1.25→1.0 day fixes for 5-day staff; test-account leave rejected; future-year leave flagged `credit_pending`. Every change is in `leave_balance_sync.log` (gitignored). Only one 2026 balance differs from filings, kept on purpose by the user.

### Config / environment
- `.env`: `IR_SGA`, `IR_OVERHEAD` (SGA added) edited by the user — IR filing access for SGA.
- root cron: `30 1 * * * … scripts/apply_pending_leave_credits.py --live >> apply_pending_leave_credits.log`.
- Work mode: all active Numa/SGA/TL set to 5day in `leave4day_emp_settings`.

### How to verify
- `/admin/entitlements` (one table, hint, Recent changes) · `/admin/emp-settings` · `/file-leave` (LWOP "no limit") · `/history` (paging) · `/incident-reports/` and `/memos` (Group filter) · `/dashboard`.
- `venv/bin/python -m unittest tests.test_leave_credits tests.test_work_mode_lwop tests.test_advance_notice -v`

### Open items / next steps
- [ ] December: load 2027 balances (208 active employees have none) — use the `year-end-rollover` agent; ask HR for regularization dates (no column exists; 2026 data fits deployment + ~6 months). 11.5 days of 2027 leave are waiting in `credit_pending`.
- [ ] New 2027 filings are still balance-checked against the current year's balance.
- [ ] 2025 balances not reviewed/corrected (user: leave 2025 for now). Optional: purge the 2025 report from git history.
- [ ] Dashboard redesign (`lovable_preview/`) on hold.

---

## 2026-10-20 (9) — SL Verification: per-date validity, HR remarks, "No medical certificate"
Commit: this one · Branch: `main`

### What was built / changed
- **Per-date Valid/Invalid** on `/leave/sl-verification` (`templates/supervisor/sl_verification.html`). Each absence date has its own Valid/Invalid toggle and a reason (`SL_INVALID_REASONS` in `app.py`: Late notice / Outside consult/fit-to-work range / Med cert not verified / No medical certificate / Other (see remarks)). The "Valid absence" readout now reads e.g. "Partial — 2 of 3 dates valid".
- **Medical certificate submitted? Yes/No** at the top of each card. Selecting No hides the cert fields and marks every date invalid with "No medical certificate".
- **Remarks to approver** textarea. Required when any date uses "Other".
- `sl_verification_submit()` stores a verdict per row (`sl_valid_absence`, `sl_invalid_reason`), plus `sl_no_medcert` and `sl_hr_remarks` on the whole request. HR can only invalidate a date the auto rules pass, never validate one they fail. Unknown reasons are coerced to "Other (see remarks)".
- `send_sl_verification_email()` has a per-date "Valid Absence (per date)" row and an "HR Remarks" row (escaped). For no-medcert it sends a short table plus an "absence is invalid" note instead of the void note.
- `/supervisor/pending` (`templates/supervisor/pending.html`, query in `app.py`): the HR Verification box shows per-date badges, the no-medcert banner and the remarks. "Manage Individual Dates" has an "HR verdict" column, so the SOM can cancel the invalid date and approve the rest.
- No-medcert filings show only **Reject** to the SOM: Approve is hidden and there's no hard-copy checkbox.
- Backups: `app.py.bak.20261020*`, `templates/supervisor/{pending,sl_verification}.html.bak.20261020*`.

### Why / decisions
- The SOM could already cancel single dates, but HR's single Yes/No for the whole request hid which date was the problem (e.g. Oct 9 late notice, Oct 10–11 fine).
- Late notice isn't in the auto rules, which is why HR has a manual Invalid override on top of them.

### Database changes
```sql
-- orangehrm2
ALTER TABLE leave4day_requests
  ADD COLUMN sl_invalid_reason VARCHAR(60) NULL AFTER sl_valid_absence,
  ADD COLUMN sl_no_medcert TINYINT(1) NOT NULL DEFAULT 0 AFTER sl_invalid_reason,
  ADD COLUMN sl_hr_remarks TEXT NULL AFTER sl_hr_verified_at;
```

### Deploy / run notes
- `sudo systemctl restart leavesystem`, clean start.
- Tested with a fake cursor (scratch script, not committed): late-notice override, the 1-day consult allowance, no-medcert, "Other" without remarks (400) and a bogus reason. Both templates were render-tested.

### How to verify
- Verify a multi-day SL on `/leave/sl-verification` with one date set Invalid. Then check the SOM card on `/supervisor/pending` and the VERIFICATION email.

### Open items / next steps
- [ ] Watch the first real multi-day and no-medcert verifications go through.
- [ ] Filings HR-verified before this change have no per-date data, so the approver card falls back to the single Yes/No.

---

## 2026-10-20 (8) — Fixes: Accept + Remember did nothing; last-target forwarder removal reported as error
Commits: `0fc0638`, this one · Branch: `main`

### What was fixed
- **Accept + "Remember these senders"**: Mailman 2 silently drops approvals when the same admindb POST also has `senderfilterp-`/`senderfilter-` fields. On managers@, 37 accepts all stayed queued. `moderate()` now posts decisions only, then adds senders through `update_sender_filter()` (verified, skips duplicates). Discard + remember works the same way.
  - Confirmed live by accepting 1 message alone (IR-20261019-B0088E, id 1651): done. The other 36 are still queued for the user to retry.
- **Forwarder last target**: removing a forwarder's only target makes cPanel delete the address. `change_members()` re-read it via `_current_emails` and raised KeyError, so the cleanup showed it as failed and it wasn't audited. It now re-reads `fetch_forwarders` and marks the result done, with a "cPanel deleted the forwarder" note.
  - This happened in the Separated cleanup to `gygl2@`, `jondel.lerio@`, `juniel.cabantingan@` and `montesa.montebon@`, which are now deleted. Audit rows were backfilled; the removed target address wasn't recorded.
  - **`gygl2@` was a group address**: mail to it now bounces. Recreate it if it's still used.

---

## 2026-10-20 (7) — Mailing Lists 8–12: private by default, privacy switch, owners/moderators, all-lists history, view-only permission
Commit: this one · Branch: `main`

### What was built / changed
- `modules/mailing_lists.py`:
  - `PRIVACY_PRESETS`, `fetch_privacy`, `set_privacy` and `POST /mailman/<addr>/privacy`. New lists default to Private and get the preset after `add_list`.
  - `fetch_list_admins`, `update_list_admins` and `POST /mailman/<addr>/admins` (the last owner is protected).
  - `/history` and `/history.csv` with `_history_filters` (parameterised).
  - `ml_can_view()`; `_access_required` now lets GET through with view or full access, while POST needs full access (403).
- Templates: privacy and owners cards on `detail.html`, the new `history.html`, and change controls hidden for view-only users on every page.
- New sub-admin permission `can_mailing_lists_view` ("Mailing Lists (view only)", Tools & Access). Both sub-admin INSERTs are now 42 cols / 42 values. app.py backup: `app.py.bak.20261020034306`.

### Database changes
```sql
-- orangehrm2
ALTER TABLE leave4day_sub_admins ADD COLUMN can_mailing_lists_view TINYINT(1) NOT NULL DEFAULT 0;
-- central_db
ALTER TABLE mailing_list_audit MODIFY action ENUM('add','remove','create','moderate','delivery','senders','delete','privacy','owners') NOT NULL;
```

### Live use by the user today (from the audit log, no failures)
- Separated cleanup: 72 forwarder removals on 14 groups (first live proof of `delete_forwarder`) and 160 Mailman removals on 11 lists.
- 43 moderation decisions. The cleanup page now shows 0 separated people.

### Open items
- [ ] Per-list access (e.g. a team lead manages only their list), possibly via Mailman owners. Not built; ask the user.
- [ ] Still untested live: forwarder add/create, delete group, delivery toggle, privacy switch, owners edit.

---

## 2026-10-20 (6) — Mailing Lists: alerts, delivery status, sender filters, person view, cleanup, offboarding, delete
Commits: `f0075cc`, `45dcc04`, this one · Branch: `main`

### What was built / changed
- **Item 1 (live Mailman change, the user asked):** `send_email@cohere.ph` added to accept_these_nonmembers on `managers@` and `sga@`, so new portal mail goes through. The user will Discard the old backlog (~1669 + 54) themselves. They were told not to tick "Remember these senders" while doing so.
- **Delivery off:** `parse_mailman_nomail`, flagged on list pages, and `mailman_set_delivery`. No member currently has delivery off.
- **Allowed & blocked senders:** `fetch_sender_filters`, `update_sender_filter`, `sender_entry_problem`, `wildcard_to_regex`. `gygccall@` has 4 `*@` entries flagged; not fixed, the user decides.
- **`mailman_form_update()`:** safe full-form submit with a before/after diff.
- **Delete group:** `delete_group` and `POST /<type>/<address>/delete`.
- **Person view:** `/person`. **Separated cleanup:** `/cleanup`, which found 173 people and 236 memberships.
- **Offboarding:** `templates/admin/offboarding.html` gets a mailing-list step (`/api/memberships`, `/api/remove-everywhere`; `can_onboarding` is allowed for these two only).
- **Alerts:** `scripts/mailing_list_pending_check.py`, `central_db.mailing_list_pending`, `/api/pending-total`, the `mlPendingBadge` element in `_topnav.html` and its refresh in `base.html`.
- Tests: `tests/test_mailing_lists.py`, 70+.

### Database changes
```sql
-- central_db
ALTER TABLE mailing_list_audit MODIFY action ENUM('add','remove','create','moderate','delivery','senders','delete') NOT NULL;
CREATE TABLE mailing_list_pending (...);  -- see scripts/mailing_list_audit_schema.sql
```

### Config / environment
- `.env`: new `MAILING_LIST_ALERT_EMAILS` (backup in the session scratchpad).
- Root crontab: 2 lines added (`--store` every 30 min, `--send` at 08:00). Backup: `/root/crontab.bak.20261020031809`.
- Test alert email sent to the user on 2026-10-20 03:17.

### Open items
- [ ] The user discards the `send_email@` backlog on `managers@`/`sga@`. Real employee posts are held on `training-team@`, `new-l2@`, `qns_agents@`, `gygcc5@` (the last two are too big: max_message_size is 40 KB).
- [ ] `gygccall@` `*@` accept entries: Fix or leave (user decision).
- [ ] Not yet used live: forwarder add/remove/create, delete, delivery toggle, and the person/cleanup removals.

---

## 2026-10-20 (5) — SpamAssassin: portal "block sender" on Discard; fixed blocklist/allowlist typos
Commit: this one · Branch: `main`

### What was built / changed
- `modules/mailing_lists.py`: `blocklist_entry()` and `spam_blocklist_add()` (read, append, write the full list, verify), plus `_block_in_spamassassin()`. Triggered by the new `spam_block` checkbox with Discard on the Pending page. Audited as `moderate` rows. Tests: 46.

### Live cPanel change (the user said yes)
- `blacklist_from`: `*@@untility3048.cc` became `*@untility3048.cc`; removed `@rhetth.com` (`*@rhetth.com` already present). 2 exact duplicates were collapsed, so the count went from 34 to 31.
- `whitelist_from`: `okta.com`, `oktaemail.com`, `assembled.com` and `assembledhq.com` became `*@…`. Before, bare domains matched nobody, so mail from those senders really skips spam checks now.
- Left `amazonses.com` / `amazonaws.com` as they were (bare, so they match nothing). Making them `*@` would allow any Amazon SES sender, including spammers.
- Rollback: before-values are in the session scratchpad (`spamassassin_before_20261020005140.json`, ephemeral). The old entries are listed above.

---

## 2026-10-20 (4) — Mailing Lists: Mailman moderation queue (Pending requests)
Commit: this one · Branch: `main`

### What was built / changed
- `modules/mailing_lists.py`:
  - Parsers `parse_held_messages`, `parse_pending_subs` and `parse_spam`.
  - `fetch_pending`, `moderation_form`, `moderate` (by id and/or by sender, optional "remember sender" filter: 6 accept / 3 discard; 300 s timeout).
  - Routes `GET/POST /admin/mailing-lists/mailman/<address>/pending` and `/api/pending/<address>`.
- `templates/mailing_lists/pending.html` (new). Index has a Pending column; the list page has a Pending requests button.
- Audit enum now also has `moderate`. Tests: 43.

### Found while building (needs the user's decision, nothing changed)
- **The portal's own mail to lists has been held since May.** `send_email@cohere.ph` (the portal's SMTP_USER) isn't a member, so Mailman holds it as "Post by non-member":
  - `managers@`: 1667 (IR notices, replies, reminders, since 2026-05-05)
  - `sga@`: 54 (onboarding, overbreak memos, since 2026-06-16)
  - Fix going forward: accept_these_nonmembers on those lists (the portal's Accept + "Remember these senders" does it). Old ones: probably Discard, not Accept, to avoid flooding members.
- `training-team@`: 12 real employees' replies held (Jun–Aug).
- Phishing held on several lists (`webmail-autho.*` credential-harvest links, `URI_PHISH`). Never Accept.

### Database changes
```sql
-- central_db
ALTER TABLE mailing_list_audit MODIFY action ENUM('add','remove','create','moderate') NOT NULL;
```

### Open items
- [ ] User to decide on the held `send_email@` mail (`managers@`, `sga@`) and on the `training-team@` replies.
- [ ] The join/leave request parser is built from Mailman's source format; there were no live requests to test it against.

---

## 2026-10-20 (3) — Mailing Lists: Sync mode (make a list exactly the pasted addresses)
Commit: this one · Branch: `main`

### What was built / changed
- `modules/mailing_lists.py`: `plan_sync`, `sync_signature`, `sync_members`, `_handle_sync` (`action=sync_preview|sync` on the `/members` POST). `detail()` now goes through `_render_detail()` so the preview renders on the group page.
- `templates/mailing_lists/detail.html`: "Add or sync members" card with an Add only / Sync switch, and a Sync preview card with Confirm sync.
- Tests: 33 in `tests/test_mailing_lists.py`.

### Why / decisions
- The user asked for "Sync Membership List" behaviour: existing members skipped, new added, missing removed.
- A preview is mandatory because a partial paste would otherwise remove everyone else. The confirm is bound to the previewed plan by a sha256 fingerprint.

### Mailman privacy, finished later the same session (user's explicit go-ahead)
- All 17 remaining lists: `subscribe_policy=3` (Confirm and approve) via UAPI `set_list_privacy_options`, and `private_roster=2` (List admin only) via a full browser-style post of Mailman's `privacy/subscribing` form. A before/after diff of every field on that page showed only `private_roster` changed.
- All 18 lists now read `accesstype=private` in cPanel. Portal member reads are still complete (checked `managers@` 8, `gygccall@` 226).
- Rollback values: `subscribe_policy=1`, `private_roster=1` (List members) on those 17.

---

## 2026-10-20 (2) — All 18 Mailman lists: archives private, not advertised (cPanel change, no code)
Commit: this one · Branch: `main`

### What was changed (live cPanel, at the user's request)
- Found that every Mailman list was Public. The archives at `https://cohere.ph/pipermail/<listid>/` (e.g. `managers_cohere.ph`, `sga_cohere.ph`) could be read by anyone without logging in, and every list showed on `/mailman/listinfo`.
- The user's own edits on `bo-cohere@` through the cPanel proxy (`:2096/cpsess…/3rdparty/mailman`) didn't save advertised/archive_private (subscribe_policy=3 and private_roster=admin did save).
- Ran UAPI `Email::set_list_privacy_options` on all 18: `advertised=0`, `archive_private=1`, `subscribe_policy` sent back unchanged.
- Verified: all 18 public archive URLs return 404, none show on listinfo, subscribe_policy unchanged. Members read archives at `/mailman/private/<listid>/`.

### Previous values (for rollback)
- All 18: `advertised=1`, `archive_private=0`. `subscribe_policy=1` (Confirm) on all except `bo-cohere@` = 3 (set by the user just before).

### Open items / next steps
- [ ] Offered, awaiting the user's answer: `subscribe_policy=3` (Confirm and approve) on the other 17, and `private_roster` = List admin only (Mailman privacy/subscribing form; UAPI doesn't cover it). cPanel's "public/private" label follows subscribe_policy only.
- [ ] Possible portal feature: per-list privacy switch on Mailman list pages, and new lists default to Private.

---

## 2026-10-20 — Mailing Lists, step 3: create new forwarder groups and Mailman lists
Commit: this one · Branch: `main`

### What was built / changed
- `/admin/mailing-lists/new` (`modules/mailing_lists.py` `new()` + `create_group()`; `templates/mailing_lists/new.html`). Same inputs as cPanel's Add Forwarder / Create a Mailing List forms. A **New mailing list** button is on the index page.
- Checks: name rules, domain from UAPI `list_mail_domains`, refuses an address that already exists (mailbox via `list_pops`, forwarder or list; fetched fresh). Forwarders need at least one target.
- Mailman: UAPI `add_list` (`list`, `domain`, `password`, `private`), then first members through `change_members`. Forwarder: `add_forwarder` per target. Both are verified by read-back and audited (`create` plus `add` rows).
- `_uapi()` takes a `module` arg (used for `PasswdStrength::get_required_strength`). Removed the stale "read-only" label on the index page.
- Tests: `tests/test_mailing_lists.py`, now 25.

### Why / decisions
- cPanel has **no** UAPI to score a password (`PasswdStrength::get_password_strength` doesn't exist). `add_list` enforces the 65 minimum itself, so the portal shows cPanel's error. The Generate button makes 20 characters with all four character types.
- First live use (by the user, 2026-10-19 23:34): removed 2 members from Mailman `l2-agents@`, both verified **done**. The Mailman write path is proven; the forwarder write path and `add_list` are not yet used live.

### Database changes
```sql
-- central_db
ALTER TABLE mailing_list_audit MODIFY action ENUM('add','remove','create') NOT NULL;
```

### Open items / next steps
- [ ] First live create (forwarder and Mailman), and the first live forwarder add/remove.
- [ ] Confirm `add_list` really rejects a weak password with a readable error. Not tested, to avoid creating a junk list.
- [ ] Delete a group address; offboarding cleanup across all groups.
- [ ] Older items (session cookie size, 3 pre-existing test failures, `.env` quoting): see 2026-10-19 (3).

---

## 2026-10-19 (4) — Mailing Lists, step 2: add/remove members, audit log; type filter
Commits: `1b53a4d` (type filter), this one · Branch: `main`

### What was built / changed
- `templates/mailing_lists/index.html`: Type filter (All / Forwarder / Mailman), kept in `?type=`.
- `modules/mailing_lists.py`: POST `/admin/mailing-lists/<type>/<address>/members` (`action=add|remove`).
  - Forwarders: UAPI `add_forwarder` / `delete_forwarder`.
  - Mailman: mass subscribe/remove forms (fetches Mailman's `csrf_token`). Welcome/goodbye email only if ticked; owner never notified.
  - Every change is verified by a fresh read-back; results are done / failed / skipped.
  - Each attempt is audited in `central_db.mailing_list_audit`.
- `templates/mailing_lists/detail.html`: Add box, checkboxes + Select separated + Remove selected with a confirmation modal, and a change-history table.
- Tests: `tests/test_mailing_lists.py`, now 17 (all mocked).

### Why / decisions
- Removal is checkbox-based (one or many) behind a modal that lists exactly who is removed. I chose this since the question was open; the user can change it.
- Self-reference (adding the group's own address) is ignored; batches are capped at 300.

### Database changes
```sql
-- central_db (scripts/mailing_list_audit_schema.sql)
CREATE TABLE mailing_list_audit (...);   -- see the file for full DDL
```

### How to verify
- `./venv/bin/python -m unittest tests.test_mailing_lists -v`
- No live write to cPanel/Mailman has been made from the portal yet. The first real add/remove is the live test; check the history table on that group's page.

### Open items / next steps
- [ ] First live add/remove on a forwarder and on a Mailman list (verify UAPI `add_forwarder`/`delete_forwarder` params and the Mailman form post on real data).
- [ ] Step 3: create/delete group addresses. Step 4: offboarding cleanup across all groups.
- [ ] Session cookie oversize, 3 pre-existing test failures, `.env` quoting: see the entry below.

---


Older entries: [docs/handoff-archive.md](docs/handoff-archive.md)

# Handoff Log — leavesystem

## Project overview
- **App:** Cohere HR portal ("Leave4Day" / hrportal), Flask monolith [app.py](app.py) plus blueprints in `modules/`, `blueprints/`, `break_log/`, `coaching.py`, `memos.py`. Frontend: Jinja + Bootstrap 5 + vanilla JS, Lucide icons.
- **DBs:** `orangehrm2` via `get_db()` (OrangeHRM tables, leave balances) and `central_db` via `get_db_connection()` / `get_central_db()` (portal data, `gsheet_employees`). Raw PyMySQL with DictCursor.
- **Deploy:** this checkout *is* production (`hrportal.cohere.ph`). systemd `leavesystem` runs `venv/bin/python app.py` on 127.0.0.1:5001 behind Apache. Restart with `sudo systemctl restart leavesystem`.
- **Cron:** tardiness/overbreak/undertime notify scripts and others under root's crontab (see CLAUDE.md).

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

## 2026-10-19 (3) — Mailing Lists, step 1: read-only view of cPanel forwarders + Mailman lists
Commit: this one · Branch: `main`

### What was built / changed
- New blueprint `modules/mailing_lists.py` at `/admin/mailing-lists` with templates `templates/mailing_lists/index.html` and `detail.html`. It lists all 66 forwarder groups and 18 Mailman lists, and each group's members matched to `gsheet_employees` (Active / Separated / Not in employee list). Read-only. Feature doc: `docs/mailing-lists.md`.
- Mailman members are read by logging in with a one-time password from UAPI `Email::generate_mailman_otp`, then going through the letter pages at `https://cohere.ph/mailman/admin/<listid>/members`. Completeness is checked against Mailman's "N members total".
- New permission `can_mailing_lists` ("Mailing Lists", Tools & Access): `permission.py`, `sub_admins.html`, `get_sub_admin_permissions`, both sub-admin INSERTs (now 41 cols / 41 values), the SELECT. Menu: Admin panel in `_topnav.html` (the Admin trigger also shows for this permission) and the admin block of `_sidebar.html`.
- Tests: `tests/test_mailing_lists.py`, 8 mocked tests.

### Why / decisions
- The user wants both kinds managed in the portal and can't move the Mailman lists to forwarders. cPanel has no Mailman member API, so the portal reads Mailman's admin HTML. The user explicitly allowed this after the auto-mode classifier first blocked the probe.
- Access: admin plus the permission only. The user plans to assign it to someone.
- The cPanel proxy path `:2083/3rdparty/mailman` returns 403, so the portal uses the domain URL (`MAILMAN_BASE_URL`, optional).

### Database changes
```sql
-- orangehrm2
ALTER TABLE leave4day_sub_admins ADD COLUMN can_mailing_lists TINYINT(1) NOT NULL DEFAULT 0;
```

### Config / environment
- Optional new key `MAILMAN_BASE_URL` (not set; default is fine). Reuses `CPANEL_*`.
- Service restarted, clean start.

### How to verify
- `./venv/bin/python -m unittest tests.test_mailing_lists -v`
- Admin → Mailing Lists. `gygccall@` (Mailman) should show 226 members with 42 separated; `gygbo1@` (forwarder) should show 44 with 23 separated.

### Open items / next steps
- [ ] Step 2: add/remove members (forwarder UAPI `add_forwarder`/`delete_forwarder`; Mailman mass subscribe/remove pages) plus an audit log table. The user wants to go step by step.
- [ ] Step 3: create/delete group addresses. Step 4: offboarding removes the person from every group.
- [ ] Session cookie is over the browser limit (about 4130–4200 of 4093 bytes; "cookie is too large" warnings all week, before this change). Browsers may drop it. Slim down `session['permissions']` (e.g. store only true flags) or move to server-side sessions.
- [ ] 3 pre-existing test failures on HEAD: `test_awol_report` (1) and `test_undertime_report` (2), unrelated to this change.
- [ ] `.env` lines 25, 57, 59, 60, 75, 78, 83, 88 have unquoted values with spaces, so `set -a; . ./.env` (the CLAUDE.md test command) mis-parses them. python-dotenv reads them fine.

---

## 2026-10-19 (2) — SL Verification permission; IR RWE Queue comments
Commits: `b730501`, this one · Branch: `main`

### What was built / changed
- **IR RWE Queue** (`templates/incident_dashboard.html`, `markRweStage`): every stage button now logs its own comment. Before, RWE Served, Explanation Received, Proceed w/ Action and Resolve logged "Status updated.". 155 old "Status updated." comments were left as is; 153 can be matched to a step by timestamp if the user ever wants them rewritten.
- **SL Verification visibility:**
  - `templates/nav/_topnav.html`: the My Team menu (trigger and panel) now also shows for `SL_HR_APPROVER`. The top menu never had this; the old sidebar did. The trigger now also includes `can_team_calendar`.
  - `inject_nav_style` now uses `is_sl_hr_approver()` (the viewed-as user), so Login As no longer shows the link from the admin's own email.
- **New permission `can_sl_verification`** ("SL Verification (HR)", Leave & Requests group): `is_sl_hr_approver()` = admin or `can_sl_verification` or the `SL_HR_APPROVER` env list. New `get_sl_verifier_emails()` = permission holders plus the env list, de-duplicated, used for the "SL Verification needed" email. Both sub-admin INSERTs are now 40 columns and 40 values.

### Why / decisions
- User chose to keep the `.env` `SL_HR_APPROVER` (option 1): it keeps the admin (Andrew) on the SL emails. HR verifiers come from the checkbox.
- Settings/Holidays visible to 7 sub-admins with `can_settings`: they're the owners, so this is intended. Left as is.

### Database changes
```sql
-- orangehrm2
ALTER TABLE leave4day_sub_admins ADD COLUMN can_sl_verification TINYINT(1) NOT NULL DEFAULT 0;
UPDATE leave4day_sub_admins SET can_sl_verification=1 WHERE emp_number IN (506,575,576); -- Kimberly Cobol, John Nino Pu-od, Kyla Ranola
```

### Open items / next steps
- [ ] `/whoami` debug route (app.py, end of file) shows the SL allow list to any logged-in user. Remove it together with audit #28 (`/debug-session`).
- [ ] Audit #21 (rest), #22, #25, #27–#34.

---

## 2026-10-19 — Audit #19 + #20: notify crons commit per employee before emailing; tardiness is dry-run by default
Commit: see `git log` (this entry's commit) · Branch: `main`

### What was built / changed
- **Checked first:** no duplicate tardiness IRs have ever been filed. All 108 IRs in `tardiness_notify.log` exist in `incident_reports`, there are no repeated dates in `tardiness_cycle_state`, and no out-of-order back-fills.
  - 18 IRs for 6 employees cite the same late day twice. That's by design: one IR from the 3-lates rule and one from the 31+ minutes rule, since the counters start over separately. Most were waived. Left as is; it's a policy question for HR.
- `tardiness_notify.py`:
  - `process_day` → `apply_rules`: per-employee `save_state` + `commit`, then emails. A failed employee is rolled back alone; a failed memo email is logged, not raised.
  - Dry run unless `--live`.
  - Idempotency guard is now `process_date <= last_processed_date`, so back-fills must run in date order.
  - Removed the dead period-recompute block (#35); uses `get_payroll_period_for_date(process_date)`.
- `overbreak_notify.py`: per-row logic moved to `_process_row`, commit per row, then emails (`_memo`). A failed row stays `notify_processed = 0` and is retried. `--dry-run` flag unchanged.
- `helpers/ir_autofile.py`: `file_incident_report(..., notify=True)` plus `send_ir_notification()`, so callers can email after committing. The default is unchanged (undertime and Break Log behave as before).
- Tests: `tests/test_notify_commit.py`, 10 mocked tests.

### Config / environment
- Root crontab: tardiness line now has `--live` (installed by the user). Backup: `/root/crontab.bak.20261019212423`.
- No service restart needed.

### How to verify
- `./venv/bin/python -m unittest tests.test_notify_commit -v`
- `./venv/bin/python tardiness_notify.py` (dry run of yesterday); check `tardiness_notify.log` after 00:15 on 2026-10-20 shows `[LIVE] Processing 2026-10-19`.

### Open items / next steps
- [x] **#20 done** (same day, next commit): `undertime_notify.py` `apply_rules(..., outbox=)` queues the IR email and memos, and `process_day` sends them after each employee's commit. Break Log `overbreak_review_action` files with `notify=False` and emails after `db.commit()`, so a second reviewer's rolled-back IR (409 duplicate) is never emailed. Tests added to `tests/test_notify_commit.py` (14 total). `leavesystem` restarted 21:46, clean.
- [ ] HR policy: may one late day count toward both the 3-lates and the 31+ minutes rules?
- [ ] Audit #21 (rest), #22, #25, #27–#34. #35 done.

---

## 2026-10-16 — Document Library for scanned HR documents; dashboard leave badges shipped
Commits: `02e6ee8`, `7d6a66d` (pushed) · Branch: `main`

### What was built / changed
- **Document Library** at `/documents`: `modules/documents.py`, `templates/documents/library.html` and `detail.html`, registered in `app.py` next to `requirements_bp`. Nav: Main → Document Library, plus a Main sub-nav tab.
  - Batch upload: PDF/JPG/PNG, 25 MB per file, 50 files per upload. Checks magic bytes and skips SHA-256 duplicates.
  - Search: FULLTEXT boolean prefix search over title, ref no., notes and PDF text, with a LIKE fallback for words under 3 characters and for ref no., employee ID and name. Snippets are highlighted in the browser.
  - Document page: preview, download, edit, soft delete (`deleted_at`), and the access log.
  - Files are stored in `uploads/hr_documents/<year>/<uuid>.<ext>` (gitignored, outside `static/`) and served only via `/documents/<id>/file` after a visibility check.
  - Text comes from pypdf. JPGs and image-only PDFs (under 40 characters of text per page) get `text_status='needs_ocr'`.
- **Dashboard** (uncommitted since 2026-10-15 21:55, shipped on the user's OK): leave-card short codes and colours (`_leave_badge`), plus week-strip shift buckets (`_shift_bucket`) in `app.py` and `templates/dashboard.html`.
- Tests: `tests/test_documents.py` hits the real DB with ZZTEST rows, cleaned up afterwards.

- **Follow-up `7d6a66d`: the library is now gated by a new `can_documents` permission** ("Document Library" checkbox, People & HR). Without admin, `DOCUMENTS_EDITORS` or `can_documents`, the menu item is hidden and every `/documents` route returns 404. HR-only docs also need `can_memos`.
- **Fixed a bug that was already there:** in `admin_sub_admins`, the bulk "by group" INSERT had one fewer `VALUES` placeholder than columns, so every bulk save failed. Both INSERTs now have 39 columns and 39 values, verified by running them with a dummy `emp_number` in a rolled-back transaction.
- Test PDF "Ticket Hub" (id 3) purged from the DB and from disk at the user's request.
- Feature doc: `docs/document-library.md`.

### Why / decisions
- HR has scanned memos and other documents on a USB (PDF and JPG, mostly no text layer). The server is a DO droplet with 2 GB RAM and no route to `\\cohere-ad`, so OCR should run on an office PC (OCRmyPDF or NAPS2), not here.
- No new `can_documents` column, to avoid touching the sub-admin SQL in `app.py`. Access rules:
  - Manage: admins plus the `DOCUMENTS_EDITORS` env list.
  - HR-only documents: managers plus `can_memos` holders.
  - "everyone" documents: any logged-in user.
  - Uploads default to `hr`.

### Database changes
```sql
-- orangehrm2
ALTER TABLE leave4day_sub_admins ADD COLUMN can_documents TINYINT(1) NOT NULL DEFAULT 0;
-- central_db (via modules.documents.ensure_schema(); idempotent)
CREATE TABLE IF NOT EXISTS hr_documents (... FULLTEXT ft_doc (title, reference_no, description, content_text) ...);
CREATE TABLE IF NOT EXISTS hr_document_access_log (...);
```

### Config / environment
- Optional `.env` key `DOCUMENTS_EDITORS` (emails or employee IDs, comma-separated). Not set yet, so only admins can upload.
- Restarted `leavesystem` 2026-10-16 00:20; clean start.

### How to verify
- `/documents` as admin: upload a PDF, search a word from it, open it, edit it.
- `./venv/bin/python -m unittest tests.test_documents -v`

### Open items / next steps
- [ ] User doesn't have the USB yet. When they get it: check samples (searchable or not), OCR on an office PC, then a bulk import script (category and date from folder names; reuse the upload logic).
- [ ] OCR pass for `text_status='needs_ocr'` rows (set `ocr_done`, fill `content_text`).
- [ ] Decide which HR staff go in `DOCUMENTS_EDITORS`.
- [ ] Maybe: show an employee's linked documents on the PIM profile.
- [ ] Carried over: Magic CWS duplicate-submission check, topnav overlap at about 1280px, #19 `tardiness_notify.py` duplicate IRs, Night Differential report.

---

## 2026-10-15 (2) — CSAT Year view, Break Log fixes, nav/search audit, page permissions enforced
Commit: `40e9d9a` (pushed) · Branch: `main`

### What was built / changed
- **My CSAT Year view:** `/api/my-csat?range=year&year=YYYY` in `app.py`, plus a Year button and year dropdown in `templates/dashboard.html`. The first column is the whole year, then one column per month. It uses the same weighting as the month view (Oct 2026 = 66.71% in both).
- **Break Log:**
  - `break_log/routes.py` `_cached_for_user()`: the `bl_account_ok` / `bl_is_overhead` caches are now tied to the email. Login As used to keep the admin's cached "no", which hid Break Log while viewing as a Numa agent.
  - The `/break-log` banner shows the scheduled shift ("Thu, Oct 15, 2026 · 1pm – 10pm"). The "Overnight shift" tag now comes from the schedule; it used to depend on the viewer's clock being before 9 AM.
- **Nav + search** (`templates/nav/_topnav.html`), audited against 42 roles:
  - Fixed the empty Analytics dropdown for `can_holiday_awol`-only users, and hid Night Differential (the route is a stub).
  - New items: Overbreak Review, Requirements, Medicine Inventory. QA Updates Data moved next to Manage QA.
  - Every page now has a sub-nav row. New Coaching and Routine Tracker groups replace the in-page tab bars (`coaching/_nav.html` keeps only the width CSS; `routines/_tabs.html` is now empty).
  - Search: `data-k` keywords per item, every word must match at a word start, title matches rank first, plus permission-gated search-only entries (`EXTRA`).
- **Page permissions now match their menu links:**
  - `modules/floor_map.py`: viewing requires admin or `can_floor_map`; Seat Setup and the seat APIs are admin-only.
  - `modules/incident_reports.py`: `/new` and `/submit` require `ir_can_file()`.
  - `modules/fts_review.py`: admin or `can_file_requests` (was any sub-admin).
  - `_has_supervisor_access` (Break Monitoring, Overbreak Review): admin, supervisor, `can_overbreak` or an overhead group (was any sub-admin).
  - `permission.py` catalog lists all 38 permissions (it had 28).

### Why / decisions
- User chose the nav/search fixes first, then asked for all permission fixes. Access was matched to what the menu already showed, rather than inventing new rules.

### How to verify
- `/dashboard` My CSAT → Year. Log in as a Numa agent → Break Log banner. Ctrl K and search "overtime", "swipe", "ir", "certificate".
- Nav audit harness approach: test-client sessions per role, collect `.tn-item` links from `/dashboard`, then GET each link (no writes).

### Open items / next steps
- [ ] Magic CWS form duplicate-submission check (`magic_cws_form`).
- [ ] Topnav: at about 1280px the search pill overlaps Operations/Analytics/Admin.
- [ ] **#19** `tardiness_notify.py` duplicate IRs, live by default. Then #20–#22 and #25–#35.
- [ ] Night Differential report (`admin_night_diff`) was never built; re-add the nav item when it is.
- [ ] Incident Reports dashboard shows agents the IRs filed within their own group. Confirm this is intended.
- [ ] Carried over from the previous entry: #6 peak-hour check, retiring the break autofix trigger, separated sub-admins, negative Magic CWS balances, 2025 entitlement drift.

---

## 2026-10-15 — Audit fixes #1–#24, dashboard redesign, File a Request, Magic CWS approvals, standard page header
Commit: `9de4757` (pushed) · Branch: `main`

A read-only debugger audit listed 35 issues. This session fixed most Critical, High and Medium ones, then built UI work on request. #13 was deliberately left alone, and #7 is infrastructure.

### What was built / changed
- **#1** `admin_required`, the active-leaves list and `supervisor_action` no longer treat `can_attrition_report` as admin.
- **#2** OT/RDW/CWS/FTS approve/reject now share one path: `_decide_file_request` / `_decide_file_requests` / `_file_request_scope` in `app.py`.
  - Team scope (`get_supervisor_file_request_employees`), no self-approval, and a `status='Pending'` guard against concurrent approvers.
  - The email goes to the record's own employee. `_local_next()` blocks open redirects.
  - Policy: the "All Requests" permission means view only.
- **#3** `adjust_leave_entitlement()` charges and refunds exactly one entitlement row: the one for the leave date's year, from the portal table if it has a row, otherwise OrangeHRM. All 8 filing, reject and cancel sites use it, and AWOL is never refunded.
- **#4** `/api/import-schedule` returns 503 when `IMPORT_API_KEY` is unset.
- **#5** `_sync_token_ok()`: the attrition and exit-interview syncs require `SYNC_SECRET`; the hard-coded fallback was removed.
- **#6** connection leaks and polling:
  - `pymysql.connect` is wrapped so every connection opened during a request is closed in `teardown_request`.
  - The break page no longer refetches `get_logs` every second.
- **#8** `coe_auto_release.py` now loads `.env` and has a `--dry-run` flag.
- **#9** material and facilities emails split the admin list and send through `send_email_as`.
- **#10** collation fix in the sub-admin "bulk by group" action.
- **#11** FTS delete used the wrong column (`employeeID`).
- **#12** `SELECT … FOR UPDATE` in all four leave approve/reject handlers.
- **#14** separated (non-admin) staff can't log in: `_is_separated_non_admin` in `auth_callback`.
- **#15** Keycloak/OAuth errors land on the login page with a message instead of a 500.
- **#16** the material-confirm invalid-link page.
- **#17** CSRF tokens sent by the ticket update modal, CoE "Mark as Received" and the assistant chat.
- **#18** the overbreak review counts by payroll cycle (8–22 / 23–7).
- **#23** coaching reminders skip separated agents.
- **#24** IR env lists are trimmed and lowercased.
- **Overbreak Review:** schedule column, readable times, "outside shift" flags.
  - `_normalize_shift` now parses `12mn`, `12nn` and `5:30pm`.
  - Report: `docs/break_log_dating_error_report.md`, **untracked on purpose** (employee names).
- **UMMS-Core** `webapp/app.py` `/webhook/break-autofix` is now schedule-aware: it only moves a pre-9 AM break back a day for overnight shifts.
  - This app is not under git. Backup: `UMMS-Core/webapp/app.py.bak.20261014040400`. The `employee-sync` service was restarted.
- **Dashboard** (`templates/dashboard.html`, `_dashboard_extras()` in `app.py`):
  - today tiles, rest days from the schedule, balances in days (LWOP marked Unpaid)
  - this-week strip, recent requests, and a full-width CSAT row with an 88% target (hidden for staff with no CSAT history)
  - aligned time records, equal-height calendar and DTR
- **Break Log links** only show for Arctic/Numa/Test accounts (`break_log_user_access()`).
- **File a Request** (`modules/request_hub.py`, `/file-requests/new`, `templates/file_requests/new.html` + `_new_times.html`) files OT, RDW, CWS, Magic CWS and FTS.
  - Each form posts to its **existing** handler with `return_to=hub`, and the handler redirects back.
  - The old forms still work by URL. The menu now shows one "File a Request" link.
- **Magic CWS** joins the shared decision path as `mcws` (`_decide_magic_cws`). Approving adds `deduction_hours` to `magic_cws_balance.used_hrs`.
  - Shown on the Approvals page and in the requests list with a Type filter.
  - `magic_cws_action` repaired (#26). The `employee_name` bug fixed (it read `session["name"]`).
- **Layout:**
  - Content is capped at 1560px (the topnav width).
  - Phone fixes on 10 pages, plus a mobile safety net in `base.html`: `.min-w-0`, table scroll wrapper, 16px inputs.
  - **Standard page header** in `base.html` built from `page_title` / `page_subtitle` / `page_actions`. Override `page_head` to replace it; the dashboard does this to keep its greeting.
  - ~55 templates converted. `#mainContent > .container(-fluid)` padding is removed.

### Database changes (all on live data, each checked first)
```sql
-- central_db.break_logs: 133 early-shift (5am-2pm) breaks re-dated to DATE(break_start)
--   rollback file: /root/break_logs_shiftdate_rollback_20261014.sql
-- central_db.magic_cws_requests: employee_name backfilled for ids 1-11 from orangehrm2.hs_hr_employee
-- central_db.magic_cws_requests ids 1-11: status='Approved', approved_by=0 ("System"), approved_at=now;
--   magic_cws_balance.used_hrs += 10 each (Jasmin -8 hrs, Marian -10 hrs remaining: duplicate submissions, user chose "approve all")
-- central_db.coaching_sessions ids 140, 202 -> status='cancelled' (separated agents)
-- orangehrm2: no writes. 2025 entitlement drift NOT corrected (user: report only)
```

### Config / environment
- `.env`: added `SYNC_SECRET`. The Apps Script reads it from the Script Property `SYNC_SECRET`, and both syncs were verified. `IMPORT_API_KEY` is intentionally unset (the endpoint is disabled).
- Root crontab: `coe_auto_release.py` now runs on `venv/bin/python`. Backup: `/root/crontab.bak.20261014025643`.
- Apps Script: the duplicate `autoFixBreaksViaWebhook` trigger was removed by the user.
- Backups kept: `/root/.env.leavesystem.bak.*` and the 3 newest `app.py.bak.*` (older ones deleted).

### How to verify
- `/dashboard`, `/file-requests/new`, `/file-requests?req_type=MCWS`, `/file-requests/approvals?req_type=MCWS`, `/overbreak-review`
- Phone-width audit approach: render a page through the test client, then run `~/.cache/ms-playwright/chromium_headless_shell-1243/*/chrome-headless-shell --window-size=390,900 --dump-dom` with a probe script.

### Open items / next steps
- [ ] Add a duplicate-submission check to the Magic CWS form (`magic_cws_form`), like the CWS form has.
- [ ] Topnav: at about 1280px the search pill overlaps Operations/Analytics/Admin.
- [ ] **#19** `tardiness_notify.py`: one commit after emailing can cause duplicate IRs, it runs live by default, and its idempotency check uses `==`.
- [ ] #20–#22, #25–#35 from the audit (#21 is partly done).
- [ ] Check peak-hour journal for `Too many connections` after the #6 fix. Consider gunicorn after #7 (2 GB RAM, heavy swap).
- [ ] Retire the remaining break-autofix Apps Script trigger once the portal is confirmed to date breaks correctly on its own.
- [ ] Remove the separated sub-admins (Jed Sagardui, Honey Bea Cortes, Anamarie Muñez, Judy Hel Abuloc), and Honey Bea from `IR_SGA`.
- [ ] HR: Jasmin's and Marian's negative Magic CWS balances. 2025 entitlement drift: see `docs/leave_entitlement_2025_reconciliation.md` (SQL not run).
- [ ] #13 (approver/reportto mismatch, Jovin as reportto for 162 employees): left as is by the user's choice.

---

## 2026-10-12 — Fix: per-date leave Cancel silently did nothing on /supervisor/pending
Commit: `367604b` (fix **uncommitted**: `templates/supervisor/pending.html`) · Branch: `main`

### What was built / changed
- `templates/supervisor/pending.html`: removed the outer `<form id="bulkForm" action="supervisor_bulk_action">` wrapper and its stray `csrf_token` hidden input. Backup: `templates/supervisor/pending.html.bak.20261012182559`.

### Why / decisions
- An approver could not cancel Princess Anne Mier's Oct 6 SL (`leave4day_requests.id` 2761, `leave_request_id` 3955). No error was shown.
- Cause: the per-date Cancel `<form>` (Manage Individual Dates) was nested inside `bulkForm`. Browsers drop the first nested `<form>` tag, so the **first date's Cancel on the first card** submitted `bulkForm` instead. It went to `/supervisor/bulk-action` with no selection and no action. The "No leaves selected" flash was then lost, because the page's JS redirects to add the default date range. Later dates and cards worked.
- Logs: the approver sent 5× `POST /supervisor/bulk-action` between 18:21 and 18:22, and never `/cancel-leave/2761`.
- The wrapper was dead code: Bulk Approve/Reject and Single Approve/Reject use `fetch` with the `<meta name="csrf-token">` token. `supervisor_bulk_action()` in `app.py` is now unused by this page, but was left in place.

### Deploy / run notes
- Template only. `debug=False` caches templates, so the user restarted `leavesystem`. Service active, clean.

### How to verify
- `/supervisor/pending?emp_search=<name>`: Manage Individual Dates → Cancel on the first date. The log should show `POST /cancel-leave/<id>`. The user confirmed the fix works.

### Open items / next steps
- [ ] Commit `templates/supervisor/pending.html`, along with the other uncommitted work below.

---

## 2026-10-07 (2) — absence_notify.py: absence alerts from portal data (parallel dry run)
Commit: `5b2c0f7` (code changes **uncommitted**: `app.py`, `absence_notify.py`) · Branch: `main`

### What was built / changed
- `app.py`: the core of `admin_absences()` was extracted into `get_absence_records(date_from, date_to)`, which returns `(records, leave_map, cws_map)`. The route is now a thin wrapper and its output is unchanged (Oct 5 and Oct 6 checked against the old code).
- New `absence_notify.py` (repo root) is the replacement for `/var/www/html/automation/send_absence_report.php`, which reads the Google Sheet. It is a dry run unless you pass `--live`; an optional positional `YYYY-MM-DD` picks the day.
  - It emails only `Absent` with no leave in any non-rejected status (pending, scheduled and approved all skip). It also skips a non-rejected CWS on that date, a SUS (`central_db.absence_records` code `SUS`) or approved OT.
  - FTS IN/OUT is **never** emailed.
  - The email lists the absent days in a 7-day window. 2 or more turns the subject into "REPEATED ABSENCE ALERT".
  - Recipients: To is the TL (`tl_view_map`), SOM (`gsheet_employees.approver`) and OM. BCC is the RTA and admin.
  - A failed run sends a "FAILED to run" email.
  - Every run logs a summary line, including 0 emailed.
  - It refuses a date that isn't finished yet (same 13:00 cutoff as the page).

### Why / decisions
- The user reported no absence email on 2026-10-07. Cause: the sheet's `Yesterday Absences` tab was empty, and the PHP script exits silently when it is.
- The sheet only covers about 84 employees a day, versus about 146 scheduled in `employee_schedules`. It also matches punches by calendar date only, so it misses new hires. Examples: Antonette Joy Lanuza on Oct 4–5 and Pia Katrina Algoso on Oct 1 (both genuinely had no punches).
- The user decided: no FTS cases in the email; pending or scheduled leaves suppress the alert.
- The logic stays in `app.py` instead of `helpers/`, because the tests patch `app.get_central_db`/`app.get_db`. The cron script imports `app`, the same way `scripts/live_sanity_check_awol.py` does.

### Config / environment
- Optional `.env` keys (defaults are in the code): `ABSENCE_NOTIFY_OM`, `ABSENCE_NOTIFY_BCC`, `ABSENCE_FAILURE_RECIPIENTS`, `ABSENCE_SENDER_NAME`.
- Root crontab: added `5 13 * * * .../absence_notify.py >> .../absence_notify.log` with **no `--live`**, a parallel dry run. The PHP line at 13:00 is unchanged and still sends.
- Backup: `app.py.bak.20261007210509`. `ast.parse` gate passed. `leavesystem` restarted at 21:08 with a clean start.

### How to verify
- `venv/bin/python absence_notify.py 2026-10-05` (dry run). It should report Antonette (REPEATED) and skip Randolf (SUS), James and Karlo (leave).
- `tail absence_notify.log` after 13:05 daily.

### Open items / next steps
- [ ] About 3 days of parallel run (Oct 8–10): compare `absence_notify.log` with the PHP sends and the sheet's "Possible Absent" rows.
- [ ] After user approval: add `--live` to the 13:05 line and remove or comment out the `send_absence_report.php` cron line (leave the PHP file in place for rollback).
- [ ] Commit `app.py` and `absence_notify.py`.
- [ ] Pre-existing test failures, unrelated to this change (they failed before it too): `test_empty_scheduled_renders_empty_state_no_crash` (awol + undertime) and `test_leave_covered_short_shift_is_excluded`.
- [ ] `send_absence_report.php` has SMTP and DB passwords in plain text.

### 2026-10-08 follow-up: IR exclusion for independent contractors
- New `.env` key `IR_EXCLUDED_EMPLOYEE_IDS` (currently 260706-01, Roselito "Lito" Orque). `is_ir_excluded()` in `helpers/ir_autofile.py` reads it.
- `tardiness_notify.py` (live loop and `--dry-run` simulation), `undertime_notify.py` and `overbreak_notify.py` skip listed employees: no cycle counting, no IR, no memo. Overbreak marks their rows `notify_processed = 1`.
- Verified: a tardiness dry run for Sep 23 – Oct 7 no longer includes him. Undertime and overbreak dry runs are clean. A `.env` backup was taken. No service restart was needed, because only the cron scripts changed.
- [ ] His existing IRs `IR-20261001-787808` and `IR-20261007-C4FD73` are still `pending`, so the stale-IR reminder will keep listing them. The user should waive them in the portal.
- [ ] These code changes are uncommitted, along with `app.py` and `absence_notify.py`.

---

## 2026-10-07 — Inventory Copy Table, PIM Time Records tab, CSAT trend
Commit: `e65aaa8` · Branch: `main`

### What was built / changed
- `/admin/inventory`: the **Export PDF** button is replaced by **Copy Table** (`copyItemsTable()` in `templates/admin/inventory.html`). It copies `Item Name | Actual Count (blank) | Unit` as TSV, using the current filter and sort, for physical stock counts. Commit `584daee`.
- PIM profile: a **Time Records** tab (`#tab-time`) shows the last 5 biometric IN/OUT pairs. The data comes from `_pim_recent_punches()` in `app.py`, which reads `central_db.dailytimerecord` via the `userdata.companyid → personid` lookup. Commit `e65aaa8`.
- PIM CSAT card: adds a trend line (8 weekly points in the Month view, 12 months in the Year view) and CSR %. These come from `_csat_scope_data(..., trend_buckets)` and were already live before this session. Committed in `e65aaa8`.

### Why / decisions
- Copy Table: the PDF duplicated the Inventory History. The admin should count stock physically, so the copy leaves out stock figures and has a blank count column.
- Time Records pairing: each IN is paired with the next punch only if it is an OUT within 20h, so overnight shifts stay on one row. An unmatched IN shows the **On shift** badge if it is the latest punch and under 20h old, otherwise **No out**.
- The `inventory_pdf` route is still in `app.py`; only the button was removed.

### Deploy / run notes
- `app.py` backup taken; `ast.parse` gate passed. `leavesystem` restarted cleanly.

### How to verify
- `/admin/inventory` → Copy Table → paste into Sheets.
- `/pim/230411-01#tab-time`

### Open items / next steps
- [ ] Decide whether to delete the unused `inventory_pdf` route.
- [ ] Suggested PIM additions (none started): IR & Coaching tab (recommended first), 201/requirements checklist, COE and issued-items history, employment timeline / probation countdown, HR-only notes, Time Records date filter/export with scheduled shift.

---

## 2026-10-05 (3) — /admin/file-leave redesigned to match File Request for Employee
Commit: `4c96c89` (changes below committed later in `c99caee` / `cc608d8` / `1085e16`) · Branch: `main`

### What was built / changed
- `templates/admin/file_leave_admin.html` rewritten. It now has:
  - a searchable employee picker (GET `?employee_id=`) and an employee card with a PIM link;
  - clickable balance cards that pick the leave type;
  - half-day mode that locks End Date to Start Date;
  - a range summary line and a confirm dialog;
  - a Recent leaves table (last 120 days and upcoming);
  - a notice that leaves filed here are **auto-approved** (taken/scheduled) and deducted immediately.
- `app.py` `admin_file_leave()`: display-only changes. A nested `_render()` replaces the 6 `render_template` calls; it passes `selected_emp`, `emp`, `balance_data` (from `_pim_leave_balance_data`), `recent` and `form`. GET with `?employee_id=` now loads balances. **The filing logic is untouched.**
- Fixed a bug: validation errors used to re-render with no employee selected and the form cleared. Both the selection and the form values are now kept.
- The employee list still excludes Separated employees (unchanged).
- **Follow-up (same day): Custom Hours + Use Remaining Balance + shift_half** added to `admin_file_leave()`, matching the regular `file_leave()` rules.
  - Custom Hours: single day, whole hours, not AWOL, at most the remaining hours. It overrides that day's deduction.
  - Use Remaining Balance: only offered when 0 < remaining < 10h. It scales the deduction to exactly the remaining hours.
  - Inserts now also write `leave4day_requests.shift_half` ('1st Half' / '2nd Half'), which admin filing used to drop.
  - Tested against the real DB with commits swapped for rollbacks: 3h custom, 1st half, and remaining 8h on 210528-05 all inserted correctly and were rolled back. Row counts and days_used were unchanged. Backup: `app.py.bak.20261005220954`. Restarted at 22:12.

### Config / environment
- Backup: `app.py.bak.20261005220538`. Original template copied to the session scratchpad.
- `leavesystem` restarted 2026-10-05 22:06 with a clean start.

### How to verify
- Open `https://hrportal.cohere.ph/admin/file-leave?employee_id=220525-01`. Validation errors (start > end, multi-day half-day) should keep the form filled in. These paths were tested with no rows written.

---

## 2026-10-05 (2) — File Request for Employee page (FTS / CWS / OT-RDW, incl. Separated)
Commit: `2621cd4` (changes below committed later in `c99caee` / `cc608d8` / `1085e16`) · Branch: `main`

### What was built / changed
- New page `/admin/file-request` (endpoint `file_for_emp.index`). Blueprint in `modules/file_for_employee.py`, template `templates/admin/file_request_for_employee.html`.
  - Pick any employee, including Separated ones, then file FTS, CWS or OT/RDW on their behalf. Recent requests (last 120 days) show underneath.
  - Admin-only "File as approved" checkbox. It writes `status='Approved'`, `approver_name` and `approved_at`, the same three columns `/api/approvals/action` sets.
  - Blocks request dates after the exit date, and the date pickers are capped at it.
  - Warns when there's no approver on file, which is typical for Separated employees.
- Nav: "File Request for Employee" added next to "File for Employee" in `templates/nav/_topnav.html` (menu + People subnav) and `templates/nav/_sidebar.html`.
- `app.py` registers the blueprint right after `survey_bp`.
- Tests: `tests/test_file_for_employee.py`, 19 tests. POSTs are fully mocked; the GET render tests are read-only against the real DB.
- One-off before the page existed: filed FTS OUT #839 for Lornelyn Sancover (260618-12), 2026-09-29 21:00, Pending.

### Why / decisions
- Permission is `can_file_for_emp`, the same flag as the leave page. "File as approved" is limited to `is_admin`, at the user's request.
- The validation and duplicate checks copy `file_fts` / `file_cws` / `file_ot` in app.py, without touching those live routes. **If self-service filing rules change, update `modules/file_for_employee.py` too.** OT also checks ot_type, rate, category and time against the form's option lists.
- The blueprint doesn't `from app import`, because app.py runs as `__main__` and that would re-execute the whole file. Instead `init_file_for_emp(...)` hands it `get_central_db`, `get_db`, `send_email`, `get_supervisor_email`, `_fetch_pim_requests` and `TICKET_REQUIRED_TYPES`. That's why it's registered down by `survey_bp`.
- Audit trail: the request tables have no "filed by" column, so each filing writes a `central_db.employee_audit_log` row (`field_name='filed_request'`, `change_source='file_for_employee'`) plus an app log line.
- Email:
  - Active employee: gets a "filed on your behalf" notice.
  - Pending request: also goes to the approver via `get_supervisor_email()`.
  - Separated employee: gets no email.

### Config / environment
- No new .env keys and no schema changes. All INSERTs were checked with `EXPLAIN` against the live tables. Backup: `app.py.bak.20261005215620`.
- `leavesystem` restarted 2026-10-05 21:58 with a clean start.

### How to verify
- `./venv/bin/python -m unittest tests.test_file_for_employee -v` (with .env loaded).
- Open `https://hrportal.cohere.ph/admin/file-request?employee_id=260618-12`; #839 shows in Recent requests.

### Open items / next steps
- [ ] Someone with All Requests access needs to approve FTS #839. Lornelyn has no approver.
- [ ] Possibly show `filed_request` audit rows somewhere in the UI (only in the DB for now).

---

## 2026-10-05 — PIM profile: Leave Balance + Attendance Grid tabs
Commit: `b8dbb0e` (changes below committed later in `c99caee` / `cc608d8` / `1085e16`) · Branch: `main`

### What was built / changed
- **Leave Balance tab** on `/pim/<employee_id>`. New helper `_pim_leave_balance_data()` in `app.py`.
  - One card per leave type showing entitled / used / remaining in days and hours, with a progress bar. A card turns red when 1 day (8h) or less remains.
  - Used is split into approved, scheduled and pending.
  - A Portal Entitlements table appears only when `leave4day_entitlements` has rows for the employee.
  - Always shows the current year. It ignores the viewed month and year.
- **Work Schedule card → "Work Schedule & Attendance"**, using new helper `_pim_attendance_grid()` in `app.py`. It started as a separate Attendance Grid tab, but the user found that redundant with the Work Schedule calendar, so the grid was merged into the calendar.
  - Evaluated days show P/A/FI/FO/SUS/RD/leave codes and pending `*`, with the admin grid's colours. Days not yet evaluated keep the old schedule look (blue with the shift, or "Rest Day").
  - Holidays and attendance notes show on the day. Hovering a day shows shift, punches, leave and note.
  - The header shows the attendance rate, a best-case forecast and per-code counts.
  - With no userdata personid, the card shows the schedule only.
- `templates/pim/profile.html`: a Leave Balance tab button and pane before Memos, the merged calendar card, and a small script that opens the tab named in the URL hash (e.g. `#tab-balance`).

### Why / decisions
- The balances reuse `get_leave_balances()`, which is the same source as the employee's `/dashboard`. The used-hours split uses its exact filter, so approved + scheduled + pending always equals Used.
- The grid copies `admin_attendance_grid()`'s day rules for a single employee instead of refactoring that live route. Verified against `/admin/attendance-grid` for 220525-01: Sep 14/15 = 93.3% and Aug 18/18 = 100% on both pages. **If the admin grid rules change, update `_pim_attendance_grid()` too.**
- One deliberate difference: the 1 PM safe-cutoff (`perf_safe_last_day`) applies to every month. The admin grid applies it only to the current month, so future months there show A's.
- Access: no new permission. Anyone with `is_admin` or `can_pim` (the existing PIM gate) sees both tabs, even without `can_absences`.

### Config / environment
- No new .env keys and no DB changes. Backup: `app.py.bak.20261005212425`.
- `leavesystem` restarted 2026-10-05 at 21:27 and again at 21:32 (after the merge). Both starts were clean.

### How to verify
- Open `https://hrportal.cohere.ph/pim/220525-01#tab-balance`, then step through months on the Work Schedule & Attendance card.

### Open items / next steps
- [ ] Get user feedback on the layout and on whether the PIM grid should honour `can_absences`.
- [ ] 220525-01 shows LWOP at 196.38 days left. That is real `ohrm_leave_entitlement` data, and `/dashboard` shows the same, but it is worth checking with HR.

---

## 2026-10-02 — Stale IR reminder email moved from old PHP dashboard to portal script
Commit: `8ef4bb4` · Branch: `main`

### What was built / changed
- The stale-incident reminder ("Incident Report Reminder: N Stale Incident(s) Require Attention") now runs from `scripts/cron_incident_reminder.py`. Before this, it ran from `/var/www/html/cohere_dashboard/incident_report/cron_reminder.php`, whose links pointed to `dashboard.cohere.ph`. The new links go to `hrportal.cohere.ph/incident-reports/<report_number>` and `/incident-reports/`.
- Committed the script's pending edits: a `--send` flag (dry run by default), a `COLLATE utf8mb4_unicode_ci` on the `supervisor_mapping` ⋈ `gsheet_employees` join, and the `import os` moved below the shebang.

### Why / decisions
- Both scripts use the same `central_db.incident_reports` query (72h idle, 24h reminder throttle) and the same `last_reminder_sent` column, so switching over doesn't double-send.
- Only the script was committed. The other modified files are separate unreviewed work (see the 2026-10-01 entry and below).

### Config / environment
- Root crontab line 56 changed to `0 9,17 * * * .../venv/bin/python .../scripts/cron_incident_reminder.py --send >> /var/log/incident_reminders.log 2>&1`. The previous crontab was backed up to the session scratchpad (`crontab.bak.20261002003100`).
- Renamed `/var/www/html/cohere_dashboard/incident_report/cron_reminder.php` to `cron_reminder.php.disabled`. It was publicly reachable with no auth, and opening it would send the old email. The old URL now returns 404. To undo, rename it back.
- Restarted `leavesystem` on 2026-10-02 00:32. It started cleanly with no code changes, because every modified .py file was older than the previous start.

### How to verify
- After 17:00 on 2026-10-02, check `/var/log/incident_reminders.log`. Python lines have `,mmm` milliseconds; the old PHP lines don't.
- In the next reminder email, the IR number links should open the portal.
- Dry run: `./venv/bin/python scripts/cron_incident_reminder.py` (no `--send`).

### Open items / next steps
- [ ] Confirm that the first live reminder email from the Python script arrives with portal links.
- [x] Committed 2026-10-05: IR workflow `1085e16`, tardiness + requirements `cc608d8`, PIM/app.py work `c99caee`.

---

Older entries: [docs/handoff-archive.md](docs/handoff-archive.md)

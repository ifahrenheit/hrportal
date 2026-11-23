# Sheet webhooks (formerly the employee-sync service)

Until 2026-10-30 a separate app, `employee-sync` (systemd, gunicorn on :5000, code in
`/var/www/html/UMMS-Core/webapp/app.py`), received the Google Sheet pushes. The portal now
serves those endpoints itself from [modules/sheet_webhooks.py](../modules/sheet_webhooks.py),
so there is one service to run.

## Who calls what

| Endpoint | Caller | Writes |
|---|---|---|
| `POST /webhook/employee-sync` | Employee sheet Apps Script (`check_sync_needed`, `batch_sync`, single row) | `gsheet_employees` |
| `POST /webhook/schedule-sync` | Schedule sheet Apps Script | `employee_schedules` (replaces the employee+date pairs sent) |
| `POST /webhook/break-autofix` | Apps Script | `break_logs.shift_date` for after-midnight breaks on overnight shifts |
| `GET /api/biometric/pending`, `POST /api/biometric/complete` | Blade Server on the office PCs (113.19.42.130, 203.28.65.222) | `pending_biometric_sync`, filled by the portal's new-hire flow |
| `GET /health` | anyone | nothing (DB check) |

`/webhook/schedule-sync` skips rows it can't use before writing, and logs one summary line
(not an error) per sync: rows whose employee ID is blank or a sheet error such as `#N/A`
(the sheet looks the ID up from the agent's name, so a misspelled or blank name gives `#N/A`),
and a repeated employee + date in the same payload (a person listed twice in a tab; the first
row wins). The reply includes `skipped_count`. Added 2026-11-23: the "GYG CC & BO Schedule
2026" sheet keeps re-sending old month tabs with such rows, which logged about 200 errors a day.

`/webhook/employee-sync` `batch_sync` commits **each employee separately** and retries a
deadlock or lock-wait timeout (MySQL 1213/1205) up to 3 times (`_write_row`). The sheet sends
the employee batch and the schedule sync at the same moment (~00:47, 08:47, 16:47). Schedule
inserts lock `gsheet_employees` rows through the foreign key while the upsert updates them,
which caused a deadlock or two a night. A deadlock rolls back the whole transaction, so with
the old one-commit-per-batch the rows before it were silently undone. Changed 2026-11-23.

The `/webhook/*` routes require the `X-Secret-Key` header to equal `WEBHOOK_SECRET_KEY` in
`.env`. The biometric routes have no key, the same as before, so the PCs did not need changes.

## Routing

The callers still use `https://webhook.cohere.ph`. Its Apache vhosts
(`/etc/apache2/sites-available/employee-sync.conf` and `employee-sync-le-ssl.conf`) proxy only
`/webhook/`, `/api/biometric/` and `/health` to the portal on 127.0.0.1:5001, and redirect
everything else (the old web UI) to `https://hrportal.cohere.ph/`.

## Logs

`sheet_webhooks.log` in the repo root (sync counts, errors, rejected keys). Apache access log:
`/var/log/apache2/employee-sync-access.log`.

## Changing the secret

Generate a new value, update `WEBHOOK_SECRET_KEY` in `.env` and the key in the Apps Script
together, then `sudo systemctl restart leavesystem`. Until both match, syncs return 401.

## Rollback

Restore the `*.bak.20261030*` vhost files next to the originals, `sudo systemctl start
employee-sync`, and `sudo systemctl reload apache2`.

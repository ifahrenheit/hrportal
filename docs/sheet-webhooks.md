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

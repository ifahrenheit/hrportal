# Mailing Lists

Group email addresses on the cohere cPanel account, viewed in the portal at
`/admin/mailing-lists` (Admin menu → Mailing Lists). Code: [modules/mailing_lists.py](../modules/mailing_lists.py),
templates in [templates/mailing_lists/](../templates/mailing_lists/).

**Status:** step 1, read-only. Nothing in the portal changes cPanel yet.

## Access
Admins, and sub-admins with **Mailing Lists** (`can_mailing_lists`, Sub-Admins page → Tools & Access).
Everyone else gets a 404 and no menu item.

## Two kinds of group address
| Type | Where members live | How the portal reads them |
|---|---|---|
| Forwarder | cPanel → Forwarders | UAPI `Email::list_forwarders` (one call for all groups) |
| Mailman list | Mailman's own admin page | UAPI `Email::generate_mailman_otp` gives a one-time admin password; the portal logs in at `MAILMAN_BASE_URL/admin/<listid>` and reads every letter page of the member list |

- Mailman pages state "N members total". If fewer were read, the page says the list is **incomplete**.
- Some addresses are both a forwarder and a Mailman list (e.g. `gygccall@`, `l2@`). Mail to them reaches both member lists. The index page lists them.
- The cPanel route to Mailman (`:2083/3rdparty/mailman`) returns 403. Use the domain URL.

## Member status
Matched by email to `central_db.gsheet_employees`. If an email appears more than once (rehires), the Active/Training row wins.
- **Active**: Active or Training
- **Separated**: flagged red, likely needs removal
- **Not in employee list**: outside or personal address

## Caching
cPanel and Mailman reads are cached in-process for 5 minutes. **Refresh** bypasses the cache.
A big Mailman list (`gygccall@`, 226 members) takes about 13 s on the first read.

## Config
Existing `CPANEL_HOST`, `CPANEL_PORT`, `CPANEL_USERNAME`, `CPANEL_API_TOKEN`. Optional `MAILMAN_BASE_URL`
(default `https://cohere.ph/mailman`).

## Next steps
1. Add/remove members (forwarders via UAPI `add_forwarder`/`delete_forwarder`; Mailman via its mass subscribe/remove admin pages), with an audit log table.
2. Create/delete group addresses.
3. Offboarding: remove a separated employee from every group.

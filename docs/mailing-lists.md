# Mailing Lists

Group email addresses on the cohere cPanel account, viewed in the portal at
`/admin/mailing-lists` (Admin menu → Mailing Lists). Code: [modules/mailing_lists.py](../modules/mailing_lists.py),
templates in [templates/mailing_lists/](../templates/mailing_lists/).

**Status:** step 2. Members can be viewed, added and removed. Creating or deleting group addresses is still done in cPanel.

## Adding and removing members
- **Add:** paste one or more addresses (commas, spaces or new lines; up to 300) on a group's page.
- **Remove:** tick members (or **Select separated**), then **Remove selected**. A dialog lists exactly who will be removed before anything changes.
- **Forwarders:** UAPI `Email::add_forwarder` / `Email::delete_forwarder`, one call per member.
- **Mailman:** the list's mass subscribe / mass remove admin forms (including Mailman's own `csrf_token`). No welcome or goodbye email unless the checkbox is ticked. The list owner is never notified.
- **Verification:** after every change the list is read back fresh. A member counts as done only if the read-back shows it. Otherwise it is **failed**, even if cPanel said OK.
- **Skipped:** adding someone who is already a member, or removing someone who isn't.
- **Audit:** every attempted change is a row in `central_db.mailing_list_audit` (DDL: [scripts/mailing_list_audit_schema.sql](../scripts/mailing_list_audit_schema.sql)): who, when, list, member, result. During Login As, the real admin is recorded, with `acting_as`. The last 50 changes show at the bottom of each group's page.

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
1. Create/delete group addresses.
2. Offboarding: remove a separated employee from every group.

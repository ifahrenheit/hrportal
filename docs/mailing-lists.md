# Mailing Lists

Group email addresses on the cohere cPanel account, viewed in the portal at
`/admin/mailing-lists` (Admin menu → Mailing Lists). Code: [modules/mailing_lists.py](../modules/mailing_lists.py),
templates in [templates/mailing_lists/](../templates/mailing_lists/).

**Status:** step 3. Members can be viewed, added and removed, and new group addresses created. Deleting a group address is still done in cPanel.

## Creating a group address
**New mailing list** on the index page (`/admin/mailing-lists/new`). The inputs match cPanel's own forms:

| | Forwarder ("Add Forwarder") | Mailman list ("Create a Mailing List") |
|---|---|---|
| Address | name + domain | list name + domain |
| Members | forward-to addresses, **at least one** | first members, optional (welcome email optional) |
| Extra | — | password + again (Generate button), access type Public / Private (UAPI `add_list` `private=0/1`) |

- **Name:** lowercase letters, numbers, `.`, `_`, `-`, starting and ending with a letter or number.
- **Domain:** one of cPanel's mail domains (UAPI `list_mail_domains`). Defaults to `CPANEL_DOMAIN`.
- **Refused** if the address already exists as an email account (`list_pops`), forwarder or Mailman list (checked fresh at submit).
- **Mailman password:** cPanel's minimum strength (65, from `PasswdStrength::get_required_strength`) is enforced by `add_list` itself. cPanel has no API to score a password in advance, so a weak one comes back as cPanel's own error. The portal only refuses passwords under 8 characters or mismatched. The password is never stored, logged or shown again.
- **Verification:** afterwards the list is read back. A `create` row and per-member `add` rows go to the audit table.

## Adding and removing members
- **Add:** paste one or more addresses (commas, spaces or new lines; up to 300) on a group's page.
- **Remove:** tick members (or **Select separated**), then **Remove selected**. A dialog lists exactly who will be removed before anything changes.
- **Forwarders:** UAPI `Email::add_forwarder` / `Email::delete_forwarder`, one call per member.
- **Mailman:** the list's mass subscribe / mass remove admin forms (including Mailman's own `csrf_token`). No welcome or goodbye email unless the checkbox is ticked. The list owner is never notified.
- **Verification:** after every change the list is read back fresh. A member counts as done only if the read-back shows it. Otherwise it is **failed**, even if cPanel said OK.
- **Skipped:** adding someone who is already a member, or removing someone who isn't.
- **Sync:** choose **Sync** instead of **Add only** and paste the full list the group should have. Addresses already on it are kept, new ones are added, and members not in the paste are removed.
  - Sync always shows a preview first (will add / will remove / unchanged, with names) and changes nothing until **Confirm sync**.
  - The confirm carries a fingerprint of the previewed plan. If the list changed in between, a new preview is shown instead of applying.
  - The preview warns in red if every current member would be removed.
  - Adds run before removes. Up to 1000 pasted, at most 300 adds and 300 removes per sync.
- **Audit:** every attempted change is a row in `central_db.mailing_list_audit` (DDL: [scripts/mailing_list_audit_schema.sql](../scripts/mailing_list_audit_schema.sql)): who, when, list, member, result. During Login As, the real admin is recorded, with `acting_as`. The last 50 changes show at the bottom of each group's page.

## Pending requests (Mailman moderation queue)
Each Mailman list has a **Pending requests** page (`/admin/mailing-lists/mailman/<address>/pending`). The index shows a **Pending** count per list.

- **Reads:** Mailman's `admindb?details=all` (held posts) and the `admindb` summary (join/leave requests). Cached 5 minutes.
- **Held posts:** sender, subject, Mailman's reason, date.
  - Badges from the server's spam headers: **Spam** (`X-Spam-Status: Yes`, with score) and **Phishing link** (`URI_PHISH` or a `*_PH_SURBL` blocklist hit).
  - **View** shows the plain-text part and the domains its links point to. The HTML is never rendered and links are not clickable.
- **By sender:** acts on every held post from that sender, needed for big queues. The message table lists the newest 200.
- **Decisions:**
  - Accept (sends to every member; confirmation required, stronger above 20 or when flagged)
  - Reject (reason emailed to sender; defaults to Mailman's own)
  - Discard
  - Membership requests: Approve / Reject / Discard (codes 4/5, 2, 3).
- **Remember these senders:** adds them to Mailman's sender filter. With Accept they go on accept_these_nonmembers (code 6); with Discard, on discard_these_nonmembers (code 3).
- **Block in SpamAssassin** (with Discard): adds the sender to cPanel's SpamAssassin `blacklist_from`.
  - It blocks `*@domain` for most senders, but only the exact address for free-mail domains (Gmail, Yahoo, Outlook …).
  - It never blocks our own mail domains or anything matching `whitelist_from`.
  - `SpamAssassin::update_user_preference` replaces the whole list, so the portal reads the list, appends, writes it back and checks it saved.
- **Verification:** the queue is re-read after posting. An item still there is **failed**; one already gone beforehand is **skipped**. Each decision is a `moderate` row in the audit table.

## Alerts: menu badge + daily email
- `scripts/mailing_list_pending_check.py` (root cron):
  - `--store` every 30 min: reads all Mailman queues and stores counts in `central_db.mailing_list_pending`.
  - `--send` at 08:00 (Asia/Manila): also emails `MAILING_LIST_ALERT_EMAILS`, listing only items new since the last email (Mailman request ids only go up). The first stored run takes the existing backlog as the starting point.
  - `--test`: sends a sample; `--dry-run` is the default with no flag. Log: `mailing_list_pending_check.log`.
- **Badge:** Admin → Mailing Lists shows the total waiting (`/admin/mailing-lists/api/pending-total`, refreshed every 60 s like the other badges). It lights the Admin menu dot.

## Members, senders, people
- **Delivery off:** Mailman switches delivery off after repeated bounces (or an admin/member does). Those members are flagged on the list page, and **Turn delivery on** switches it back through the members-page form.
- **Allowed & blocked senders** (Mailman lists): view/add/remove `accept_these_nonmembers`, `hold_…`, `reject_…` and `discard_…`.
  - Mailman 2 matches exact addresses or `^regex` only. `*@domain` lines never match and are flagged, with a **Fix** that converts them to `^.*@domain$`.
- **Mailman form changes** go through `mailman_form_update()`: the whole form is submitted as a browser would. It refuses if anything other than the intended fields changed, or if the change didn't stick.
- **Find a person** (`/admin/mailing-lists/person`): every group an employee is on (all forwarders + all Mailman lists, read in parallel). Remove from or add to several at once.
- **Separated cleanup** (`/admin/mailing-lists/cleanup`): every Separated employee still on any group, removable in bulk.
- **Offboarding page:** after offboarding, it shows the leaver's lists with **Remove from all**. Staff with `can_onboarding` can use only these two lookup/removal endpoints.
- **Delete a group:** type the address to confirm. Members at deletion are written to the change history first (rebuild with Sync). A Mailman delete also deletes its archives.

## Privacy, owners, history
- **Privacy card** (Mailman lists): shows the four settings and a **Make private / Make public** button.
  - Private = not advertised, archives for members only, joining needs confirm + approval, member list for admins only.
  - The first three are set through UAPI `set_list_privacy_options`; member-list visibility goes through Mailman's privacy form.
  - New lists default to Private and get the full preset after creation.
- **Owners & moderators card:** add/remove through Mailman's General Options (`owner` / `moderator`). The last owner can't be removed.
- **History** (`/admin/mailing-lists/history`): every change on every list. Filter by list, action, result, who, text and dates. **Export CSV** uses the same filters; cells that could act as spreadsheet formulas are neutralised.

## Access
- **Mailing Lists** (`can_mailing_lists`): view and change everything.
- **Mailing Lists (view only)** (`can_mailing_lists_view`): every page, no change controls. Every POST and the new-list form return 403 for them.
- Admins have full access. Offboarding staff (`can_onboarding`) can use only the two offboarding endpoints.
- Everyone else gets a 404 and no menu item.

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
1. Delete a group address.
2. Offboarding: remove a separated employee from every group.

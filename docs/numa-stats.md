# Numa Stats

`/numa-stats/` shows the Numa CS team's monthly and weekly numbers (daily too, once the sheet has a daily tab): conversations, WhatsApp CSAT, email CSAT, email SLA, median first reply and median response. There's also a month-by-month grid like the sheet's "MoM Numa CS Stat Board".

Team rows and tiles follow the user's rules (2026-11-18):
- **Conversations:** the total of the agents.
- **CSAT (WhatsApp and email):** the average across the team's conversations. Each agent's CSAT is weighted by their conversation count, and agents with no conversation count are left out.
- **Email SLA and response times:** the median of the agents.

A "Median per agent" tile still shows the median conversation count.

## Where the data comes from

The **NUMA Stat Dashboard** Google Sheet. Kim Bolongaita (numastays.com) owns it, and Jareth Becari edits it. Its numbers come from Numa's Looker (looks 2603 and 2604).

- The portal reads the **raw** tabs: `Raw` (monthly) and `Raw-Weekly`, plus `Raw-Daily` once that tab exists. The "MoM Numa CS" and "WoW Numa CS" tabs are only dropdown views over those tabs, so the portal builds its own period picker instead of reading them.
- Columns are matched by header name. A new tab must keep the same headers: `Username`, a `Created …` column with a full date (or `YYYY-MM` for months), `Unique Conversation Count`, `Avg Csat`, `Email Message SLA Percentage`, `CSAT - Whatsapp`, and the response-time and Q1–Q3 columns. The tab names can be changed in `.env` (`NUMA_STATS_MONTH_TAB`, `NUMA_STATS_WEEK_TAB`, `NUMA_STATS_DAY_TAB`).
- Access is through the portal's Drive service account `timestamp-drive-reader@wpforms-smtp-501221.iam.gserviceaccount.com`. It's shared on the sheet as Viewer. The Google Sheets API is enabled in that Google Cloud project. If the sheet is ever re-shared or copied, the service account has to be added again.

## Sync

- `helpers/numa_stats.py` reads the sheet and replaces the rows for one period type in `central_db.numa_stats` inside a single transaction, so corrections and deletions in the sheet carry over.
- Guards: a tab without the expected headers, with no rows, or shrinking below half its stored rows is skipped and reported. The existing data is kept.
- Cron runs 5 minutes past each hour: `scripts/sync_numa_stats.py --live` writes to `numa_stats_sync.log`. Run it without `--live` for a dry run. `--force` loads anyway when the shrink guard fires and you've checked the sheet.
- Admins, plus anyone listed in `.env` `NUMA_STATS_SYNC_EMAILS` (comma-separated; currently Jareth Becari and Kim Bolongaita), have a **Sync now** button on both pages. People on that list can also view the pages. Edit teams stays admin only. Restart the service after changing the list.
- Every run is logged in `numa_stats_sync_log`. The page shows when the last successful sync was and flags a failed attempt.
- The schema is in `scripts/numa_stats_schema.sql`.

## Teams · Looker page (`/numa-stats/teams`)

Team-level figures exactly as Looker calculates them, from the **Jareth SLA Copy** sheet (owned by jareth.becari@numastays.com, ID `1MMUbRgcDbxIJrNB-12atXsp-ITo-os7-6-FbcGWVCNo`, or `NUMA_TEAM_SHEET_ID` in `.env`). The portal's service account can read it.

**Where the data comes from:**
- The sheet has three tabs (`SLAs`, `conversation`, `CSAT`). Each holds several Looker exports side by side (weekly, monthly and yearly/YTD), and each export starts with a "Team Attribute" column.
- `helpers/numa_team_stats.py` finds the blocks by their headers and the period type by the date's shape. It keeps only Looker's **Team Cohere** (shown as **GX Team**) and **GX Team Night Shift** (shown as **GX Night Shift**). Numa's other teams are never stored.
- Rows are upserted into `central_db.numa_team_stats`, one value per block/period/team/metric, and are never deleted. Weeks that fall off Looker's 500-row exports stay in the portal.
- It runs inside the same sync as the Agents page (hourly cron and Sync now).

**What the page shows:**
- Per team and period:
  - conversations (Unique Conversation Count summed over email, WhatsApp, WhatsApp threads and SMS)
  - Agent CSAT (weekly uses "Agent + Emma CSAT", because Looker's weekly export has no Agent CSAT column)
  - avg CSAT, responses and response rate
  - email SLA and WhatsApp (chatbot) SLA, with met and not-met counts
- The current period is labelled "Running"/"so far". November's **running email SLA 34.88%** and **Agent CSAT 91.82%** for GX Team are the figures the user quoted from Looker.
- "Looker vs the Agents page" puts each team's figure next to what the per-agent rows add up to, with the gap. The two don't match by design:
  - Looker's team attribute isn't our GX/Night split. Looker's Night Shift had 4,732 conversations in November, against 1,544 for our 4 night agents.
  - Agents' conversations double-count shared conversations.
  - The per-agent "Email Message SLA Percentage" (about 9%) is clearly a different measure from the team SLA (about 35%).
- A trend table covers the last 12 periods.

## Who can see it

Everyone with access sees the whole team, as in the sheet:

- admins
- Active or Training employees in group **Numa CS** or **Numa BO** (`gsheet_employees.group_name`)
- anyone who is the `approver` of such an employee (currently Jareth)

The check is cached in the session for 10 minutes. The same check controls the "Numa Stats" link in the sidebar and in the top nav.

## Teams

The page splits agents into **GX Team** and **GX Night Shift**.

- Under "All teams", the table and the month-by-month grid show each team as its own section with its own team row, plus an overall row. The Team filter narrows the page to one team.
- Assignments are stored in `central_db.numa_stats_teams`. Anyone without a row is in GX Team.
- Seeded 2026-11-18 with the night shift: Mancao, Dequino, Inson and Lumbab.
- Admins change assignments with **Edit teams** on the page (`/numa-stats/api/teams`, admin only, CSRF). The team names are `TEAMS` in `modules/numa_stats.py`.

## Which agents are listed

Current Numa CS and BO staff, matched to Numa usernames by email (`nina.delcastillo@cohere.ph` → `nina_delcastillo`). Agents with no data for the selected view (new hires, and BO until Numa tracks them) aren't listed. Numa's own staff in the raw tabs (e.g. `marta_natividad`) are stored but never shown.

## Open question

**Email SLA %.** It was 45–74% in January and has been around 2–24% since mid-year. Confirm with Kim or Jareth whether it now measures emails that *missed* SLA. Until then the page labels it as Looker does and doesn't colour its arrows as good or bad.

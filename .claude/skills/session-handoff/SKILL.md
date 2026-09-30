---
name: session-handoff
description: Use when the user says handoff, wrap up, save progress or end of session, or asks to resume/continue where we left off — writes and reads HANDOFF.md in the repo.
---

# Session Handoff

Keeps a running record of what was built in this repo so the next session (local or cloud agent) can pick up without re-explaining. The record lives in `HANDOFF.md` at the repo root and is committed, so it survives ephemeral cloud sessions.

## Two modes

- **Resume** — user says "resume", "continue", "where did we leave off", or a session starts in a repo that has `HANDOFF.md`.
- **Write** — user says "handoff", "wrap up", "save progress", "end session", or the task is finished.

---

## Resume mode

1. Read `HANDOFF.md` (top entry first; older entries only if needed).
2. Run `git log --oneline <last-handoff-commit>..HEAD` to catch commits made after the last handoff.
3. Run `git status` to spot uncommitted work.
4. Tell the user in a few lines: what was last done, what is open, anything changed since. Then ask what to work on, or continue the top open item if they already said.

If `HANDOFF.md` doesn't exist, say so in one line and carry on.

---

## Write mode

### 1. Gather facts (don't rely on memory of the chat alone)

```bash
git rev-parse --short HEAD
git branch --show-current
git status --short
git diff --stat HEAD
git log --oneline -20
```

Find the commit hash recorded in the previous handoff entry and list commits since then. Look at the actual diffs of key files if the summary needs detail.

### 2. Write the new entry

Insert at the **top** of the entries in `HANDOFF.md` (newest first). Use this shape and drop sections that are empty:

~~~markdown
## YYYY-MM-DD — <one-line summary>
Commit: `<short-hash>` · Branch: `<branch>`

### What was built / changed
- <feature or fix> — `path/to/file.py`, `templates/x.html`

### Why / decisions
- <decision and the reason, especially anything non-obvious>

### Database changes
```sql
-- exact ALTER/CREATE statements that were run, and on which DB
```

### Config / environment
- <new .env KEYS (names only, never values)>, cron jobs, Apache/nginx, systemd, services restarted

### Deploy / run notes
- <commands run on the server, where the app lives, how to restart>

### How to verify
- <URL or command that shows it works>

### Open items / next steps
- [ ] <unfinished thing, known bug, TODO the user mentioned>
~~~

### 3. Rules

- **Never write secrets**: no passwords, API keys, tokens, DB credentials, `.env` values. Key *names* are fine.
- Record what actually happened, not plans that were dropped.
- Use real file paths and route names so the next session can jump straight to them.
- Keep entries tight — bullets, not prose.
- If `HANDOFF.md` grows past ~400 lines, move entries older than the latest 10 into `docs/handoff-archive.md` (create if missing) and leave a one-line pointer.

### 4. Save it

1. Create `HANDOFF.md` if missing, with a first line `# Handoff Log — <repo name>` and a short **Project overview** section (stack, main entry points, databases, where it's deployed) above the entries. Update that overview whenever it becomes wrong.
2. Commit: `git add HANDOFF.md docs/handoff-archive.md 2>/dev/null; git commit -m "docs: handoff YYYY-MM-DD"`.
3. Push if this session pushes normally (always in cloud-agent sessions — otherwise the record is lost when the session ends). If unsure, ask once.
4. Tell the user in 2–3 lines what was recorded and the top open item.

---

## Tip for automatic resume

If the repo has a `CLAUDE.md`, add this line so every session reads the handoff without being asked (offer to add it the first time the skill runs):

```
At the start of each session, read HANDOFF.md and follow the session-handoff skill's resume mode.
```
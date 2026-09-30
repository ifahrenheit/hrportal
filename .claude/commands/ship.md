Build and ship a feature to the HR portal following the house workflow: $ARGUMENTS

1. developer agent — build the route + Jinja template pair (correct connector, parameterized %s, house theme).
2. tester agent — write/run tests; if it touches app.py, use the /tmp patch-script workflow with ast.parse() gate and timestamped .bak.
3. reviewer agent — check %%s f-string escaping, DictCursor usage, COLLATE on cross-DB joins, gsheet.approver routing.
4. security agent — SQL injection, exposed secrets in .env, access-control gaps.
5. database agent — validate schema changes; run SELECT/count before any destructive op.
6. Only if all pass: apply, `sudo systemctl restart leavesystem`, then tail journalctl to confirm clean start.

## Final step: handoff
When the feature is shipped (committed and pushed), run the session-handoff skill in write mode so the work is recorded in HANDOFF.md.

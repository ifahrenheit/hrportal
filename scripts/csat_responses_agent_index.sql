-- central_db. Per-agent CSAT lookups (PIM profile, CSAT agent view, My CSAT)
-- filter on agent_email + performed_at_date; without this every lookup
-- scanned the whole table. Non-destructive, built online.
ALTER TABLE csat_responses
  ADD INDEX idx_agent_email_date (agent_email, performed_at_date),
  ALGORITHM=INPLACE, LOCK=NONE;

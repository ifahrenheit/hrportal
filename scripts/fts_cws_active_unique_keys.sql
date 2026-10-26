-- central_db. Applied 2026-10-27. The duplicate keys on fts_requests and
-- cws_requests used to cover Rejected/Deleted rows too, so refiling after a
-- rejection or deletion (allowed since 4d44445) hit a 1062 and a 500.
-- is_active is 1 for live requests and NULL otherwise; NULLs never collide,
-- so the keys now only block a second active copy of the same request.
ALTER TABLE fts_requests
  ADD COLUMN is_active TINYINT AS (CASE WHEN deleted_at IS NULL AND (status IS NULL OR status NOT IN ('Rejected','Deleted','Cancelled')) THEN 1 END) VIRTUAL,
  DROP INDEX uniq_fts_request,
  ADD UNIQUE KEY uniq_fts_request (employeeID, fts_date, fts_time, fts_type, is_active);

ALTER TABLE cws_requests
  ADD COLUMN is_active TINYINT AS (CASE WHEN deleted_at IS NULL AND (status IS NULL OR status NOT IN ('Rejected','Deleted','Cancelled')) THEN 1 END) VIRTUAL,
  DROP INDEX uniq_cws_request,
  ADD UNIQUE KEY uniq_cws_request (employee_id, original_date, original_time, new_date, new_time, is_active);

-- Rollback:
-- ALTER TABLE fts_requests DROP INDEX uniq_fts_request, ADD UNIQUE KEY uniq_fts_request (employeeID, fts_date, fts_time, fts_type), DROP COLUMN is_active;
-- ALTER TABLE cws_requests DROP INDEX uniq_cws_request, ADD UNIQUE KEY uniq_cws_request (employee_id, original_date, original_time, new_date, new_time, deleted_at), DROP COLUMN is_active;

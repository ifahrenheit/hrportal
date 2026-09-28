-- central_db. State for undertime_notify.py -- same shape as
-- tardiness_cycle_state, keyed by employee_id instead of personid.
CREATE TABLE IF NOT EXISTS undertime_cycle_state (
  employee_id            varchar(20) NOT NULL,
  period_start           date NOT NULL,
  count_since_reset      int NOT NULL DEFAULT 0,
  minutes_since_reset    int NOT NULL DEFAULT 0,
  total_count_in_cycle   int NOT NULL DEFAULT 0,
  total_minutes_in_cycle int NOT NULL DEFAULT 0,
  last_processed_date    date DEFAULT NULL,
  count_triggers_sent    int NOT NULL DEFAULT 0,
  minutes_triggers_sent  int NOT NULL DEFAULT 0,
  count_breakdown        text,
  minutes_breakdown      text,
  updated_at             timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (employee_id, period_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

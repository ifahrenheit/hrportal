-- Break Log access per account + group (2026-11-13), central_db.
-- Replaces the hard-coded BREAK_LOG_ACCOUNTS / OVERHEAD_GROUPS / OVERBREAK_ACCOUNTS
-- in break_log/routes.py. Edited on Admin -> Break Log Access.
-- Blank account/group in gsheet_employees is stored as ''.

CREATE TABLE IF NOT EXISTS break_log_group_access (
  account          VARCHAR(100) NOT NULL DEFAULT '',
  group_name       VARCHAR(100) NOT NULL DEFAULT '',
  can_log          TINYINT(1)   NOT NULL DEFAULT 0,  -- may use /break-log
  can_monitor      TINYINT(1)   NOT NULL DEFAULT 0,  -- may open Break Monitoring / Overbreak Review
  overbreak_review TINYINT(1)   NOT NULL DEFAULT 0,  -- their breaks feed the portal Overbreak Review
  updated_by_id    VARCHAR(50)  NULL,
  updated_by_name  VARCHAR(150) NULL,
  updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (account, group_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS break_log_access_log (
  id          INT          NOT NULL AUTO_INCREMENT,
  account     VARCHAR(100) NOT NULL,
  group_name  VARCHAR(100) NOT NULL,
  field       VARCHAR(30)  NOT NULL,   -- can_log / can_monitor / overbreak_review
  old_value   TINYINT(1)   NULL,       -- NULL = row did not exist yet
  new_value   TINYINT(1)   NOT NULL,
  actor_id    VARCHAR(50)  NOT NULL,
  actor_name  VARCHAR(150) NULL,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Seed with the rules that were hard-coded on 2026-11-13, for every account/group
-- pair currently in the employee master (non-separated):
--   can_log          = account in (Arctic, Numa, GYG, Test)
--   can_monitor      = group in (Finest, RTA, QA, IT, TL, Trainer, BO TL)
--   overbreak_review = account in (Numa, Arctic)
INSERT IGNORE INTO break_log_group_access
  (account, group_name, can_log, can_monitor, overbreak_review, updated_by_id, updated_by_name)
SELECT acc, grp,
       acc IN ('Arctic', 'Numa', 'GYG', 'Test'),
       grp IN ('Finest', 'RTA', 'QA', 'IT', 'TL', 'Trainer', 'BO TL'),
       acc IN ('Numa', 'Arctic'),
       'seed', 'Initial setup (previous hard-coded rules)'
FROM (SELECT DISTINCT COALESCE(account, '') AS acc, COALESCE(group_name, '') AS grp
      FROM gsheet_employees WHERE status <> 'Separated') p;

-- Trainer had monitor access in code but nobody is in that group right now.
INSERT IGNORE INTO break_log_group_access
  (account, group_name, can_log, can_monitor, overbreak_review, updated_by_id, updated_by_name)
VALUES ('General', 'Trainer', 0, 1, 0, 'seed', 'Initial setup (previous hard-coded rules)');

-- 2026-11-13: effective date for Overbreak Review. From this shift date on, the
-- group's Break Log overbreaks show in the portal review. NULL = always. The
-- overbreak sheet's automatic IRs (overbreak_notify.py) are unaffected; the
-- review lists any IR already filed for the agent + date.
ALTER TABLE break_log_group_access
  ADD COLUMN overbreak_review_from DATE NULL AFTER overbreak_review;

-- Text values for log entries that are not on/off (the from date).
ALTER TABLE break_log_access_log
  ADD COLUMN old_text VARCHAR(20) NULL AFTER new_value,
  ADD COLUMN new_text VARCHAR(20) NULL AFTER old_text;

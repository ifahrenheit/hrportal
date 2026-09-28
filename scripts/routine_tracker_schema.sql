-- Routine Tracker schema (central_db). Idempotent: safe to re-run.
--
-- routine_tasks : task definitions (what must be checked, and how often)
-- routine_logs  : one row per check performed. Flat and typed on purpose so it
--                 can be pulled straight into Power BI / external reporting
--                 (real DATETIME, short ENUM strings, no serialized blobs).

CREATE TABLE IF NOT EXISTS routine_tasks (
    id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
    title       VARCHAR(150) NOT NULL,
    description TEXT NULL,
    category    VARCHAR(60)  NOT NULL DEFAULT 'IT group',
    frequency   ENUM('daily','weekly','biweekly','monthly') NOT NULL DEFAULT 'daily',
    active      TINYINT(1)   NOT NULL DEFAULT 1,
    created_by  VARCHAR(255) NOT NULL,
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_routine_tasks_active (active, frequency)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS routine_logs (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    task_id    INT UNSIGNED NOT NULL,
    checked_by VARCHAR(255) NOT NULL,
    checked_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    result     ENUM('pass','fail') NOT NULL,
    comment    TEXT NULL,
    PRIMARY KEY (id),
    KEY idx_routine_logs_task_time (task_id, checked_at),
    KEY idx_routine_logs_time (checked_at),
    CONSTRAINT fk_routine_logs_task FOREIGN KEY (task_id)
        REFERENCES routine_tasks (id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

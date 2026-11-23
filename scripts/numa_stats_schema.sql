-- Numa Stats (central_db). Filled from the "NUMA Stat Dashboard" Google Sheet by
-- helpers/numa_stats.py (cron: scripts/sync_numa_stats.py --live, plus the
-- page's Sync now button). One row per agent per period; the sync replaces a
-- period type's rows wholesale, so this table always mirrors the sheet.

CREATE TABLE IF NOT EXISTS numa_stats (
    id                      INT UNSIGNED NOT NULL AUTO_INCREMENT,
    period_type             ENUM('month','week','day') NOT NULL,
    period_start            DATE NOT NULL,               -- 1st of month / week's Monday / the day
    username                VARCHAR(100) NOT NULL,       -- Numa username, e.g. nina_delcastillo
    conversations           INT NULL,
    email_csat              DECIMAL(4,2) NULL,           -- 1-5 scale ("Avg Csat")
    email_sla_pct           DECIMAL(6,2) NULL,           -- "Email Message SLA Percentage", as Looker reports it
    whatsapp_csat_pct       DECIMAL(6,2) NULL,
    q1_pct                  DECIMAL(6,2) NULL,
    q2_pct                  DECIMAL(6,2) NULL,
    q3_pct                  DECIMAL(6,2) NULL,
    avg_response_sec        INT NULL,
    median_response_sec     INT NULL,
    p90_response_sec        INT NULL,
    avg_first_reply_sec     INT NULL,
    median_first_reply_sec  INT NULL,
    synced_at               DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_numa_stats_period_user (period_type, period_start, username),
    KEY ix_numa_stats_user (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS numa_stats_sync_log (
    id           INT UNSIGNED NOT NULL AUTO_INCREMENT,
    started_at   DATETIME NOT NULL,
    finished_at  DATETIME NULL,
    triggered_by VARCHAR(190) NOT NULL,                  -- 'cron' or the admin's email
    ok           TINYINT(1) NOT NULL DEFAULT 0,
    summary      TEXT NULL,
    PRIMARY KEY (id),
    KEY ix_numa_stats_sync_started (started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Team split shown on the page (added 2026-11-18). Agents without a row are in
-- 'GX Team'; admins change assignments from the page's Edit teams dialog.
CREATE TABLE IF NOT EXISTS numa_stats_teams (
    username    VARCHAR(100) NOT NULL,
    team        VARCHAR(50)  NOT NULL,
    updated_by  VARCHAR(190) NOT NULL,
    updated_at  DATETIME     NOT NULL,
    PRIMARY KEY (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Team-level Looker figures from the "Jareth SLA Copy" sheet (helpers/numa_team_stats.py).
-- Narrow: one value per block/period/team/metric. Upserted, never deleted, so weeks
-- that fall off Looker's 500-row exports stay. Percentages are fractions (0.3488).
CREATE TABLE IF NOT EXISTS numa_team_stats (
    id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
    block         ENUM('chatbot_sla','email_sla','conversations','csat') NOT NULL,
    period_type   ENUM('week','month','year') NOT NULL,
    period_start  DATE NOT NULL,                 -- week's Monday / 1st of month / Jan 1
    team          VARCHAR(50) NOT NULL,          -- 'GX Team' | 'GX Night Shift'
    metric        VARCHAR(100) NOT NULL,         -- e.g. email_message_sla_met, email.unique_conversation_count, agent_csat
    value         DECIMAL(18,6) NULL,
    synced_at     DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_numa_team_stats (block, period_type, period_start, team, metric),
    KEY ix_numa_team_stats_period (period_type, period_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

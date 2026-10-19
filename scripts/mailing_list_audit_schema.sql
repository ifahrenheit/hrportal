-- Mailing Lists audit log (central_db). Idempotent: safe to re-run.
--
-- mailing_list_audit : one row per member change attempted from the portal
--                      (/admin/mailing-lists), including skipped and failed ones.
--                      actor_* is the real person; during Login As, acting_as is
--                      the user they were viewing as.

CREATE TABLE IF NOT EXISTS mailing_list_audit (
    id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actor_email   VARCHAR(255) NOT NULL,
    actor_name    VARCHAR(255) NULL,
    acting_as     VARCHAR(255) NULL,
    list_type     ENUM('forwarder','mailman') NOT NULL,
    list_address  VARCHAR(255) NOT NULL,
    action        ENUM('add','remove','create','moderate','delivery','senders','delete') NOT NULL,
    member_email  VARCHAR(255) NOT NULL,
    result        ENUM('done','failed','skipped') NOT NULL,
    detail        VARCHAR(500) NULL,
    PRIMARY KEY (id),
    KEY idx_mla_list (list_address, created_at),
    KEY idx_mla_member (member_email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- mailing_list_pending : per Mailman list, the moderation queue as last seen by
--                        scripts/mailing_list_pending_check.py (feeds the menu
--                        badge) and how far the daily alert email has got.
CREATE TABLE IF NOT EXISTS mailing_list_pending (
    list_address     VARCHAR(255) NOT NULL,
    posts            INT UNSIGNED NOT NULL DEFAULT 0,
    subs             INT UNSIGNED NOT NULL DEFAULT 0,
    suspicious       INT UNSIGNED NOT NULL DEFAULT 0,
    max_request_id   INT UNSIGNED NOT NULL DEFAULT 0,
    notified_max_id  INT UNSIGNED NULL,
    error            VARCHAR(500) NULL,
    checked_at       DATETIME     NOT NULL,
    PRIMARY KEY (list_address)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

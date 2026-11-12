-- IR attachment editing (2026-11-12), central_db.
-- Attachments are soft-removed (row and file kept for audit) and every
-- add/remove is written to incident_attachment_log with who, when and why.

ALTER TABLE incident_attachments
  ADD COLUMN removed_at      DATETIME     NULL,
  ADD COLUMN removed_by_id   VARCHAR(50)  NULL,
  ADD COLUMN removed_by_name VARCHAR(150) NULL,
  ADD COLUMN remove_reason   VARCHAR(255) NULL;

ALTER TABLE incident_comment_attachments
  ADD COLUMN removed_at      DATETIME     NULL,
  ADD COLUMN removed_by_id   VARCHAR(50)  COLLATE utf8mb4_unicode_ci NULL,
  ADD COLUMN removed_by_name VARCHAR(150) COLLATE utf8mb4_unicode_ci NULL,
  ADD COLUMN remove_reason   VARCHAR(255) COLLATE utf8mb4_unicode_ci NULL;

CREATE TABLE IF NOT EXISTS incident_attachment_log (
  id                INT          NOT NULL AUTO_INCREMENT,
  report_id         INT          NOT NULL,
  comment_id        INT          NULL,          -- set for comment attachments
  attachment_id     INT          NOT NULL,      -- id in incident_(comment_)attachments
  action            ENUM('added','removed') NOT NULL,
  file_name         VARCHAR(255) NOT NULL,      -- stored name in the uploads dir
  original_name     VARCHAR(255) NULL,          -- name on the uploader's device
  file_size         INT          NULL,
  mime_type         VARCHAR(100) NULL,
  reason            VARCHAR(255) NULL,
  actor_employee_id VARCHAR(50)  NOT NULL,
  actor_name        VARCHAR(150) NULL,
  created_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_report (report_id, created_at),
  CONSTRAINT fk_ir_att_log_report FOREIGN KEY (report_id)
    REFERENCES incident_reports (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

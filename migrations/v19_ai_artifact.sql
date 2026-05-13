-- v19_ai_artifact.sql
-- Adds the AI artifact registry. Every PDF / chart PNG / CSV / markdown
-- export the AI chatbot writes to S3 also gets a row here so users can:
--   * list their prior artifacts,
--   * re-issue a signed URL after the original one expires.

CREATE TABLE IF NOT EXISTS ai_artifact (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id     INT NOT NULL,
  kind        VARCHAR(32) NOT NULL,         -- 'report' | 'chart' | 'csv' | 'markdown'
  s3_key      VARCHAR(512) NOT NULL,
  mime        VARCHAR(80) NOT NULL,
  file_name   VARCHAR(255) NULL,
  title       VARCHAR(200) NULL,
  meta        JSON NULL,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_artifact_user_time (user_id, created_at),
  INDEX idx_artifact_kind (kind)
);

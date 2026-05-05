-- v20_chat_polls_tasks.sql
-- Polls and Tasks attached to chat messages.
--
-- A poll / task is paired 1:1 with a `chat_messages` row of
-- message_type='poll' / 'task' so it broadcasts and lives in the
-- conversation timeline like any other message. The auxiliary tables
-- here hold the dynamic state (votes, assignees, statuses) that
-- updates after the original message is sent.

-- ── Polls ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS chat_polls (
  id              BIGINT PRIMARY KEY AUTO_INCREMENT,
  message_id      BIGINT NOT NULL UNIQUE,
  question        VARCHAR(500) NOT NULL,
  allow_multiple  TINYINT(1) NOT NULL DEFAULT 0,
  closed_at       DATETIME NULL,
  closed_by       INT NULL,
  created_by      INT NOT NULL,
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_poll_message FOREIGN KEY (message_id)
    REFERENCES chat_messages(id) ON DELETE CASCADE,
  INDEX idx_poll_message (message_id)
);

CREATE TABLE IF NOT EXISTS chat_poll_options (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  poll_id     BIGINT NOT NULL,
  text        VARCHAR(255) NOT NULL,
  position    INT NOT NULL DEFAULT 0,
  CONSTRAINT fk_option_poll FOREIGN KEY (poll_id)
    REFERENCES chat_polls(id) ON DELETE CASCADE,
  INDEX idx_option_poll (poll_id, position)
);

CREATE TABLE IF NOT EXISTS chat_poll_votes (
  id          BIGINT PRIMARY KEY AUTO_INCREMENT,
  poll_id     BIGINT NOT NULL,
  option_id   BIGINT NOT NULL,
  user_id     INT NOT NULL,
  voted_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_vote_poll FOREIGN KEY (poll_id)
    REFERENCES chat_polls(id) ON DELETE CASCADE,
  CONSTRAINT fk_vote_option FOREIGN KEY (option_id)
    REFERENCES chat_poll_options(id) ON DELETE CASCADE,
  UNIQUE KEY uq_poll_user_option (poll_id, option_id, user_id),
  INDEX idx_vote_poll_user (poll_id, user_id)
);

-- ── Tasks ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS chat_tasks (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  message_id    BIGINT NOT NULL UNIQUE,
  title         VARCHAR(200) NOT NULL,
  description   TEXT NULL,
  due_at        DATETIME NULL,
  priority      ENUM('low','medium','high') NOT NULL DEFAULT 'medium',
  status        ENUM('open','in_progress','done','cancelled')
                NOT NULL DEFAULT 'open',
  created_by    INT NOT NULL,
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_by  INT NULL,
  completed_at  DATETIME NULL,
  CONSTRAINT fk_task_message FOREIGN KEY (message_id)
    REFERENCES chat_messages(id) ON DELETE CASCADE,
  INDEX idx_task_status (status),
  INDEX idx_task_due (due_at)
);

CREATE TABLE IF NOT EXISTS chat_task_assignees (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  task_id       BIGINT NOT NULL,
  user_id       INT NOT NULL,
  status        ENUM('open','done') NOT NULL DEFAULT 'open',
  assigned_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  assigned_by   INT NOT NULL,
  completed_at  DATETIME NULL,
  CONSTRAINT fk_assignee_task FOREIGN KEY (task_id)
    REFERENCES chat_tasks(id) ON DELETE CASCADE,
  UNIQUE KEY uq_task_user (task_id, user_id),
  INDEX idx_assignee_user_status (user_id, status)
);

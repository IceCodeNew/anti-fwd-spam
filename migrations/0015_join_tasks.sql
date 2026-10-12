CREATE TABLE model_tasks_replacement (
    bot_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    subject_id INTEGER NOT NULL DEFAULT 0,
    phase TEXT NOT NULL CHECK (phase IN ('classify', 'delete', 'done')),
    input_json TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 0,
    due_at INTEGER NOT NULL,
    lease_until INTEGER NOT NULL DEFAULT 0,
    stop_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    edit_pending INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bot_id, chat_id, message_id, subject_id)
);
INSERT INTO model_tasks_replacement (
    bot_id, chat_id, message_id, subject_id, phase, input_json, attempts, generation,
    due_at, lease_until, stop_at, created_at, expires_at, revision, edit_pending
)
SELECT bot_id, chat_id, message_id, 0, phase, input_json, attempts, generation,
    due_at, lease_until, stop_at, created_at, expires_at, revision, edit_pending
FROM model_tasks;
DROP TABLE model_tasks;
ALTER TABLE model_tasks_replacement RENAME TO model_tasks;
CREATE INDEX model_tasks_due ON model_tasks(bot_id, due_at) WHERE phase != 'done';
CREATE INDEX model_tasks_expiry ON model_tasks(expires_at);

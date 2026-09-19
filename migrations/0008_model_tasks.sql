CREATE TABLE IF NOT EXISTS model_tasks (
    bot_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('classify', 'delete', 'done')),
    input_json TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 0,
    due_at INTEGER NOT NULL,
    lease_until INTEGER NOT NULL DEFAULT 0,
    stop_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (bot_id, chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS model_tasks_due ON model_tasks(bot_id, due_at) WHERE phase != 'done';
CREATE INDEX IF NOT EXISTS model_tasks_expiry ON model_tasks(expires_at);

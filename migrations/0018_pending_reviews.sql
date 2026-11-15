CREATE TABLE IF NOT EXISTS pending_reviews (
    bot_id INTEGER NOT NULL,
    update_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    target_message_id INTEGER NOT NULL,
    target_sender_id INTEGER,
    reporter_id INTEGER,
    payload_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    resolved_at INTEGER,
    verdict TEXT CHECK (verdict IN ('spam', 'not_spam')),
    outcome TEXT,
    PRIMARY KEY (bot_id, update_id)
);
CREATE INDEX IF NOT EXISTS pending_reviews_pending ON pending_reviews(bot_id, created_at) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS pending_reviews_expiry ON pending_reviews(expires_at);

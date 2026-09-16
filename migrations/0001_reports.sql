CREATE TABLE reports (
    bot_id INTEGER NOT NULL,
    update_id INTEGER NOT NULL,
    received_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    raw_update TEXT NOT NULL CHECK (json_valid(raw_update)),
    classification TEXT NOT NULL CHECK (json_valid(classification)),
    response_status INTEGER,
    response_body TEXT,
    PRIMARY KEY (bot_id, update_id)
);

CREATE INDEX reports_expiry ON reports(expires_at);

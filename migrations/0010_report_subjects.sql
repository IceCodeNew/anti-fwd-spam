CREATE TABLE reports_with_subjects (
    bot_id INTEGER NOT NULL,
    update_id INTEGER NOT NULL,
    subject_id INTEGER NOT NULL DEFAULT 0,
    received_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    raw_update TEXT NOT NULL CHECK (json_valid(raw_update)),
    classification TEXT NOT NULL CHECK (json_valid(classification)),
    response_status INTEGER,
    response_body TEXT,
    moderation_result TEXT,
    ban_claimed INTEGER NOT NULL DEFAULT 0 CHECK (ban_claimed IN (0, 1)),
    command_source_id INTEGER,
    PRIMARY KEY (bot_id, update_id, subject_id)
);

INSERT INTO reports_with_subjects (
    bot_id, update_id, received_at, expires_at, raw_update, classification,
    response_status, response_body, moderation_result, ban_claimed, command_source_id
)
SELECT bot_id, update_id, received_at, expires_at, raw_update, classification,
       response_status, response_body, moderation_result, ban_claimed, command_source_id
FROM reports;

DROP TABLE reports;
ALTER TABLE reports_with_subjects RENAME TO reports;
CREATE INDEX reports_expiry ON reports(expires_at);

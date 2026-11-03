CREATE TABLE source_registrations (
    bot_id INTEGER NOT NULL,
    update_id INTEGER NOT NULL,
    source_id INTEGER NOT NULL,
    introduced INTEGER NOT NULL CHECK (introduced IN (0, 1)),
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (bot_id, update_id, source_id)
);
CREATE INDEX source_registrations_account ON source_registrations(bot_id, source_id, update_id);
CREATE INDEX source_registrations_expiry ON source_registrations(expires_at);

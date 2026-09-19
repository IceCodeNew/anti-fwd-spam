CREATE TABLE IF NOT EXISTS blacklisted_users (
    bot_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (bot_id, user_id)
);

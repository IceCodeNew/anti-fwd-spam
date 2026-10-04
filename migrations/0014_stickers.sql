CREATE TABLE blacklisted_stickers (
    bot_id INTEGER NOT NULL,
    file_unique_id TEXT NOT NULL,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (bot_id, file_unique_id)
);

CREATE TABLE blacklisted_sticker_sets (
    bot_id INTEGER NOT NULL,
    set_name TEXT NOT NULL,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (bot_id, set_name)
);

CREATE TABLE automatic_mutes (
    bot_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (bot_id, chat_id, message_id)
);

CREATE INDEX automatic_mutes_expiry ON automatic_mutes(expires_at);

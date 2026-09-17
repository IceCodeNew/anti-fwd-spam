CREATE TABLE recent_messages (
    bot_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    sent_at INTEGER NOT NULL,
    PRIMARY KEY (bot_id, chat_id, message_id)
);

CREATE INDEX recent_messages_sender ON recent_messages(bot_id, chat_id, sender_id, message_id);
CREATE INDEX recent_messages_expiry ON recent_messages(sent_at);

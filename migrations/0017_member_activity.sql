ALTER TABLE recent_messages ADD COLUMN activity_eligible INTEGER NOT NULL DEFAULT 0
    CHECK (activity_eligible IN (0, 1));
ALTER TABLE recent_messages ADD COLUMN history_removed INTEGER NOT NULL DEFAULT 0
    CHECK (history_removed IN (0, 1));

ALTER TABLE automatic_mutes ADD COLUMN user_id INTEGER;
ALTER TABLE automatic_mutes ADD COLUMN update_id INTEGER;
CREATE INDEX automatic_mutes_account ON automatic_mutes(bot_id, chat_id, user_id, update_id);

CREATE TABLE member_activity (
    bot_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    matured_count INTEGER NOT NULL CHECK (matured_count BETWEEN 0 AND 10),
    PRIMARY KEY (bot_id, chat_id, user_id)
);

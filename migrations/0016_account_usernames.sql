ALTER TABLE blacklisted_users ADD COLUMN username TEXT;
CREATE INDEX blacklisted_users_username ON blacklisted_users(bot_id, username COLLATE NOCASE) WHERE username IS NOT NULL;

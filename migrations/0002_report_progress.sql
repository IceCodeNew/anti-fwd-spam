ALTER TABLE reports ADD COLUMN moderation_result TEXT;
ALTER TABLE reports ADD COLUMN ban_claimed INTEGER NOT NULL DEFAULT 0 CHECK (ban_claimed IN (0, 1));

UPDATE reports SET moderation_result = 'target removed before upgrade'
WHERE response_status IS NULL
AND (response_body LIKE 'deleted%' OR response_body LIKE 'already absent%');

UPDATE reports SET ban_claimed = 1
WHERE response_status IS NULL AND response_body LIKE 'ban failed%';

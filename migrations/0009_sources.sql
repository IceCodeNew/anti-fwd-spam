CREATE TABLE blacklisted_sources (
    source_id INTEGER PRIMARY KEY CHECK (source_id != 0 AND abs(source_id) <= 4503599627370495)
);

ALTER TABLE reports ADD COLUMN command_source_id INTEGER;

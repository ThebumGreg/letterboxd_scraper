-- The originals each worker keeps after replacing a file (JSON list the agent sends with its poll), for the Originals tab.
ALTER TABLE worker ADD COLUMN originals TEXT;

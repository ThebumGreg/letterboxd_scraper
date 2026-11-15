-- A per-file encoder choice for conversions and compression tests: NULL (what the profile says), 'cpu' or 'gpu'. It is reset when the file changes.
ALTER TABLE library_file ADD COLUMN encoder_choice TEXT CHECK (encoder_choice IN ('cpu', 'gpu') OR encoder_choice IS NULL);
-- The encoder the test was asked to use and the one that ran, so the card can say which.
ALTER TABLE compression_test ADD COLUMN encoder_wanted TEXT;
-- A person said this file is a mobile (smaller) copy of the film, although its name does not follow `Film (Year) - 1080p.mkv`.
ALTER TABLE library_file ADD COLUMN is_mobile INTEGER NOT NULL DEFAULT 0;

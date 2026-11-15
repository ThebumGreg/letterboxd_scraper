-- "Test before converting": a conversion that compresses waits for a compression test as well as the quality check, and a Fail or Borderline holds it.
-- ctest_override remembers the settings a person accepted on purpose ("Convert anyway"): it holds the tested profile key, so a changed profile or encoder asks again.
ALTER TABLE convert_wait ADD COLUMN compression_test_id INTEGER;
ALTER TABLE library_file ADD COLUMN ctest_override TEXT;

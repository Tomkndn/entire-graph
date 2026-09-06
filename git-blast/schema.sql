-- Git-Blast Live — SQLite schema (see SPEC.md "Database Schema").
--
-- The Databricks variant of this DDL adds `USING DELTA` to each CREATE TABLE
-- (SPEC.md "Next Stage: Databricks Integration"). `files_modified` is a JSON
-- array of strings in SQLite and ARRAY<STRING> in Databricks.

-- Source file to test file mappings.
CREATE TABLE IF NOT EXISTS module_test_map (
    repo_id TEXT NOT NULL,
    source_file TEXT NOT NULL,
    imported_symbol TEXT,
    test_file TEXT NOT NULL,
    last_execution_status TEXT,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Checkpoint logs for session recovery.
CREATE TABLE IF NOT EXISTS checkpoint_logs (
    repo_id TEXT NOT NULL,
    checkpoint_sha TEXT NOT NULL,
    prompt_summary TEXT,
    files_modified TEXT,  -- JSON array in SQLite, ARRAY<STRING> in Databricks
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Lookup path for fetch_target_tests: source files -> covering tests, per repo.
CREATE INDEX IF NOT EXISTS idx_module_test_map_lookup
    ON module_test_map (repo_id, source_file);

-- Recent-first checkpoint retrieval for session recovery.
CREATE INDEX IF NOT EXISTS idx_checkpoint_logs_recent
    ON checkpoint_logs (repo_id, created_at);

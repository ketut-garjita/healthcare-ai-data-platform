CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS platform_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO platform_metadata (key, value)
VALUES ('schema_version', '0.1.0')
ON CONFLICT (key) DO NOTHING;

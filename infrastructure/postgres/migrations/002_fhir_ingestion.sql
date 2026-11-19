CREATE TABLE IF NOT EXISTS fhir_ingestion_attempts (
    ingestion_id UUID PRIMARY KEY,
    source TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    content_hash CHAR(64),
    status TEXT NOT NULL CHECK (status IN ('processing', 'ingested', 'unchanged', 'failed')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    raw_object_key TEXT,
    error_category TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_fhir_ingestion_attempts_resource
    ON fhir_ingestion_attempts (source, resource_type, resource_id, started_at DESC);

CREATE TABLE IF NOT EXISTS fhir_resources (
    source TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    version_id TEXT,
    last_updated TIMESTAMPTZ,
    content_hash CHAR(64) NOT NULL,
    raw_object_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    first_ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, resource_type, resource_id)
);

CREATE TABLE IF NOT EXISTS fhir_resource_versions (
    source TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    version_id TEXT,
    content_hash CHAR(64) NOT NULL,
    raw_object_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, resource_type, resource_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_fhir_resources_type_updated
    ON fhir_resources (resource_type, last_updated DESC);

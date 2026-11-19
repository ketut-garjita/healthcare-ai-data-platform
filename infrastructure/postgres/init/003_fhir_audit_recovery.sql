ALTER TABLE fhir_ingestion_attempts
    ADD COLUMN IF NOT EXISTS payload JSONB,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS silo_uploaded BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_fhir_ingestion_attempts_stale
    ON fhir_ingestion_attempts (lease_expires_at)
    WHERE status = 'processing';

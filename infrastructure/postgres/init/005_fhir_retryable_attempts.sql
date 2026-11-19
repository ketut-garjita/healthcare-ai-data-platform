ALTER TABLE fhir_ingestion_attempts
    ADD COLUMN IF NOT EXISTS retryable BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_fhir_ingestion_attempts_retryable
    ON fhir_ingestion_attempts (lease_expires_at)
    WHERE status = 'processing' OR retryable = TRUE;

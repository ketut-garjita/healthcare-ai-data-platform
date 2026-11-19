ALTER TABLE hl7_ingestion_attempts
    ADD COLUMN processing_duration_ms BIGINT NOT NULL DEFAULT 0
        CHECK (processing_duration_ms >= 0),
    ADD COLUMN active_processing_started_at TIMESTAMPTZ;

UPDATE hl7_ingestion_attempts
SET active_processing_started_at = updated_at
WHERE status = 'processing';

ALTER TABLE hl7_ingestion_attempt_retries
    ADD COLUMN processing_duration_ms BIGINT NOT NULL DEFAULT 0
        CHECK (processing_duration_ms >= 0),
    ADD COLUMN result_error_code TEXT;

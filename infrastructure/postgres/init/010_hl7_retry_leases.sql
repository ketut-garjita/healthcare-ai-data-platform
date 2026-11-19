ALTER TABLE hl7_ingestion_attempts
    ADD COLUMN lease_expires_at TIMESTAMPTZ;

UPDATE hl7_ingestion_attempts
SET lease_expires_at = CASE
    WHEN status = 'processing' THEN started_at + INTERVAL '5 minutes'
    WHEN status = 'failed' AND retryable THEN updated_at + INTERVAL '30 seconds'
    ELSE NULL
END;

CREATE TABLE hl7_ingestion_attempt_retries (
    attempt_id UUID NOT NULL REFERENCES hl7_ingestion_attempts(id),
    retry_number INTEGER NOT NULL CHECK (retry_number > 0),
    previous_status TEXT NOT NULL CHECK (previous_status IN ('processing', 'failed')),
    previous_error_code TEXT,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    result_status TEXT CHECK (result_status IN ('accepted', 'unchanged', 'conflict', 'rejected', 'failed')),
    PRIMARY KEY (attempt_id, retry_number)
);

CREATE INDEX hl7_attempts_due_idx
    ON hl7_ingestion_attempts (lease_expires_at)
    WHERE status = 'processing' OR (status = 'failed' AND retryable = TRUE);

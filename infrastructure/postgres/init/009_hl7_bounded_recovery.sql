ALTER TABLE hl7_ingestion_attempts
    ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0);

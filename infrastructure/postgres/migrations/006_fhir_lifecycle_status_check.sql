DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fhir_ingestion_attempts_lifecycle_status_check'
          AND conrelid = 'fhir_ingestion_attempts'::regclass
    ) THEN
        ALTER TABLE fhir_ingestion_attempts
            ADD CONSTRAINT fhir_ingestion_attempts_lifecycle_status_check
            CHECK (status <> 'retryable');
    END IF;
END $$;

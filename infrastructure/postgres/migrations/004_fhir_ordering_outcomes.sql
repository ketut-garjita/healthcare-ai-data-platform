ALTER TABLE fhir_ingestion_attempts
    ADD COLUMN IF NOT EXISTS outcome TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fhir_ingestion_attempts_outcome_check'
    ) THEN
        ALTER TABLE fhir_ingestion_attempts
            ADD CONSTRAINT fhir_ingestion_attempts_outcome_check
            CHECK (outcome IS NULL OR outcome IN
                ('created', 'updated', 'unchanged', 'historical_only', 'conflict', 'failed'));
    END IF;
END $$;

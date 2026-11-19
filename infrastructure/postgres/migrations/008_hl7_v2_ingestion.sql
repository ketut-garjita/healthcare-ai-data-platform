CREATE TABLE hl7_messages (
    id UUID PRIMARY KEY,
    source_id TEXT NOT NULL,
    sending_application TEXT NOT NULL,
    sending_facility TEXT NOT NULL,
    message_control_id TEXT NOT NULL,
    message_type TEXT NOT NULL,
    trigger_event TEXT NOT NULL,
    hl7_version TEXT NOT NULL,
    message_timestamp TEXT,
    raw_object_key TEXT NOT NULL,
    raw_sha256 CHAR(64) NOT NULL,
    parsed_message JSONB NOT NULL,
    lifecycle_status TEXT NOT NULL CHECK (lifecycle_status = 'accepted'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, sending_application, sending_facility, message_control_id)
);

CREATE TABLE hl7_message_versions (
    id BIGSERIAL PRIMARY KEY,
    message_id UUID NOT NULL REFERENCES hl7_messages(id),
    raw_object_key TEXT NOT NULL,
    raw_sha256 CHAR(64) NOT NULL,
    parsed_message JSONB,
    outcome TEXT NOT NULL CHECK (outcome IN ('accepted', 'duplicate', 'conflict')),
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (message_id, raw_sha256)
);

CREATE TABLE hl7_ingestion_attempts (
    id UUID PRIMARY KEY,
    source_id TEXT NOT NULL,
    sending_application TEXT,
    sending_facility TEXT,
    message_control_id TEXT,
    message_type TEXT,
    trigger_event TEXT,
    hl7_version TEXT,
    raw_sha256 CHAR(64) NOT NULL,
    raw_object_key TEXT NOT NULL,
    raw_payload BYTEA,
    message_id UUID REFERENCES hl7_messages(id),
    status TEXT NOT NULL CHECK (status IN ('processing', 'accepted', 'unchanged', 'conflict', 'rejected', 'failed')),
    outcome TEXT NOT NULL,
    error_code TEXT,
    sanitized_error TEXT,
    retryable BOOLEAN NOT NULL DEFAULT FALSE,
    silo_uploaded BOOLEAN NOT NULL DEFAULT FALSE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX hl7_attempts_stale_idx
    ON hl7_ingestion_attempts (started_at)
    WHERE status = 'processing';
CREATE INDEX hl7_attempts_status_idx
    ON hl7_ingestion_attempts (status, started_at DESC);
CREATE INDEX hl7_versions_message_idx
    ON hl7_message_versions (message_id, observed_at DESC);


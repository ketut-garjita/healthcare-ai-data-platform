# System Architecture — M1

## Context

A local-first engineering simulation of a healthcare data platform. Use synthetic or explicitly
authorized data only. This is not a production EHR.

## M1 components

- FastAPI: application/API entry point.
- PostgreSQL 16 + pgvector: relational storage and future vector retrieval.
- HAPI FHIR: FHIR interoperability sandbox.
- MinIO: S3-compatible object-storage sandbox.
- Docker Compose: reproducible local runtime.

## Trust boundaries

1. Developer workstation.
2. Docker bridge network.
3. API service.
4. Database and object storage.
5. External FHIR clients and future LLM providers.

Do not expose database, object-storage, or FHIR administration endpoints publicly.
Local development credentials are placeholders and must be changed before shared deployment.

## Future AWS mapping

- MinIO → S3.
- PostgreSQL → RDS for PostgreSQL or Aurora PostgreSQL.
- API container → ECS or another approved container runtime.
- Secrets → Secrets Manager.
- Encryption → KMS.
- Logs and metrics → CloudWatch.

Service selection requires workload, security, availability, and cost analysis.

## FHIR ingestion

The application uses a configurable synchronous FHIR REST client that checks the HAPI
CapabilityStatement for FHIR 4.0.1 before resource operations. Patient, Practitioner, Encounter,
and Observation payloads can be searched, read, created, and updated. The ingestion service
validates supported resource types and FHIR IDs, records deterministic SHA-256 hashes, writes
content-addressed raw JSON to Silo, and persists current JSONB state plus observed content versions
and per-attempt audit status in PostgreSQL. Mutating API routes require an environment-configured
Bearer token and fail closed when it is absent; health and readiness remain public. The local API
port is published on loopback only.

Ingestion attempts commit before external storage access. PostgreSQL transaction-scoped advisory
locks serialize updates by source, resource type, and ID. Timezone-aware `meta.lastUpdated` controls
current-state promotion; opaque `meta.versionId` is never ordered. Older, unordered, and
equal-timestamp conflicting content remains in version history with an explicit outcome. Expired
attempts can be retried by `python -m healthcare_platform.ingestion.reconcile`; retries reuse the
content-addressed Silo key and never delete objects. A Silo upload can outlive a rolled-back
PostgreSQL transaction, so a reconciliation retry may attach that existing object to committed
database state. Raw payloads preserve semantic JSON after canonical serialization, not source bytes.

For an existing local database, apply migrations `002` through `007` in order. Matching
initialization scripts under `infrastructure/postgres/init` apply to fresh local databases. These
are additive forward migrations.

## HL7 v2 ingestion

The local MLLP receiver runs as a separate Compose service on `127.0.0.1:2575`; internal Compose
clients connect to `hl7-mllp:2575`. MLLP framing and connection limits are independent of parsing
and persistence. The `hl7apy` parser validates configured versions (default `2.5.1`), supports the
initial ADT A01/A02/A03/A04/A08/A40, ORM O01, and ORU R01 categories, and stores ordered segments
including unknown Z segments alongside small message-specific normalized views. Parsing does not
infer missing clinical values or transform to FHIR.

The receiver persists an HL7 attempt and exact message bytes before Silo I/O. Raw ER7 bytes are
stored under SHA-256 content-addressed keys; a valid MSH-7 timestamp provides a stable date path,
otherwise the key uses `undated`. PostgreSQL stores message identity, metadata, parsed JSONB, each
distinct observed payload hash, and attempt outcomes in HL7-specific tables. Sender identity plus
MSH-10 defines message identity. Same identity and same bytes is acknowledged `AA` as unchanged;
same identity with different bytes is recorded as conflict and acknowledged `AR` without replacing
the accepted message. `AA` is sent only after Silo and PostgreSQL persistence succeed. Malformed or
unsupported messages receive `AE` or `AR`; audit/database failures receive `AE`.

Expired `processing` attempts and expired `failed` attempts marked `retryable` are atomically claimed
with row locks and `FOR UPDATE SKIP LOCKED`. The claim moves the attempt to `processing`, increments
`retry_count`, records a retry ledger row, and refreshes its five-minute `lease_expires_at`. A
transient failure is eligible again after a 30-second delay. Generation checks fence late workers so
they cannot overwrite a retry or terminal result. `started_at` remains the original receipt time;
retryable failures keep `completed_at` null, while terminal outcomes set it, and `updated_at` tracks
state changes. `lease_expires_at` controls recovery eligibility. Per-generation processing milliseconds
are accumulated separately from end-to-end elapsed time (`completed_at - started_at`), so recovery
waiting is excluded from processing duration. For an interrupted worker, only work through its last
persisted processing checkpoint is counted; uncheckpointed work at the instant of a process crash
cannot be reconstructed from durable state. The retry limit
is controlled by `HL7_RECONCILIATION_MAX_RETRIES` (default three); exhausted attempts become
non-retryable and retain raw bytes for audit/manual handling. The deterministic Silo key is reused;
objects are never removed automatically. A Silo upload may succeed before PostgreSQL promotion
fails, leaving an orphan object until a retry attaches it.

The HTTP API exposes protected attempt status at `GET /ingestions/hl7/{attempt_id}` and aggregate
counters at `GET /integrations/hl7/metrics`, including separately named average processing and
end-to-end elapsed seconds. Interrupted attempts can be retried with
`python -m healthcare_platform.ingestion.reconcile_hl7`. The MLLP listener admits at most
`HL7_MAX_CONNECTIONS` live application client sockets (default 20), including idle clients awaiting
their first frame; excess connections are closed immediately before a handler task is created. The
first frame must begin within `HL7_CONNECTION_TIMEOUT_SECONDS`; later frames use
`HL7_READ_TIMEOUT_SECONDS`. MLLP itself has no transport authentication in this local development
listener; keep it loopback-bound and do not expose it to untrusted networks. This is not a production
hospital connectivity design.

For an existing database, apply migrations `008_hl7_v2_ingestion.sql`,
`009_hl7_bounded_recovery.sql`, `010_hl7_retry_leases.sql`, and
`011_hl7_processing_metrics.sql` after migrations `002` through `007`.
Fresh databases receive the byte-identical scripts from `infrastructure/postgres/init`.

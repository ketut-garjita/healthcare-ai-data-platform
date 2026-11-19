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

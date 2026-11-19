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

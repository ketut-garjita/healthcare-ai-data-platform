# Healthcare AI Data Platform

A modular portfolio project for healthcare data engineering, interoperability, analytics, secure AI data access, and AWS-oriented architecture.

> **Safety:** This is an engineering simulation, not a clinical product or HIPAA-certified system. Use synthetic or legally authorized data only. Never commit real PHI or credentials.

## M1 — Foundation

This starter provides FastAPI, PostgreSQL + pgvector, HAPI FHIR, S3-compatible Object Storage (PGSTY Silo), pytest, Ruff, GitHub Actions CI, and initial architecture/agent governance documentation.

## Prerequisites

- Docker Engine and Docker Compose v2
- Python 3.12+
- `uv`
- Git

## Quick start

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

| Service | URL |
|---|---|
| FastAPI docs | http://127.0.0.1:8000/docs |
| HAPI FHIR metadata | http://localhost:8080/fhir/metadata |
| SILO console | http://localhost:9001 |
| PostgreSQL | localhost:5432 |

SILO credentials are in `.env`. Change all development placeholders before sharing the environment.

## Local Python development

```bash
uv sync --all-groups
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run uvicorn healthcare_platform.main:app --reload
```

When running the API directly on the host, set `DATABASE_URL` to use `localhost` instead of `postgres`.
Set `FHIR_API_BEARER_TOKEN` to a locally generated secret before using FHIR API routes. There is no
built-in token value; protected routes return 503 until it is configured. Keep the value in an
untracked `.env` or another secret source. `/health` and `/ready` remain public.

## Useful commands

```bash
docker compose logs -f api
docker compose down
```

## FHIR R4 integration

The API verifies the upstream CapabilityStatement and requires `fhirVersion` `4.0.1` before
resource operations. The initial integration supports Patient, Practitioner, Encounter, and
Observation. Configure `FHIR_BASE_URL` and the Silo connection using environment variables.

Apply the additive FHIR schema migrations in order to an existing local database:

```bash
for migration in infrastructure/postgres/migrations/00[2-7]_*.sql; do
  docker compose exec -T postgres sh -lc \
    'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$migration" || exit 1
done
```

Fresh databases receive matching SQL through `infrastructure/postgres/init`. These are forward,
additive migrations; they do not drop or rewrite existing resource/version data.

| Operation | Request |
|---|---|
| Check HAPI R4 capability | `GET /integrations/fhir/capability` |
| Search | `GET /integrations/fhir/Patient?active=true` (same route for the other supported types) |
| Read | `GET /integrations/fhir/Patient/{id}` |
| Create | `POST /integrations/fhir/resources` with a FHIR JSON resource body |
| Update | `PUT /integrations/fhir/resources` with a FHIR JSON resource body containing `id` |
| Ingest a resource | `POST /ingestions/fhir` with a FHIR JSON resource body |
| Check an attempt | `GET /ingestions/{ingestion_id}` |

All FHIR API routes, including capability, search, read, create, update, ingestion, and ingestion
status, require `Authorization: Bearer <FHIR_API_BEARER_TOKEN>`. Set a non-empty
`FHIR_API_BEARER_TOKEN` in the local environment; the API fails closed when it is absent.
`/health` and `/ready` remain public operational probes. Compose binds every host-published service
port to `127.0.0.1`; service-to-service traffic continues over the private Compose network. This is
a local bearer-token security boundary, not a substitute for an identity provider or TLS deployment
for remote access.
The `status` field is the canonical lifecycle (`processing`, `ingested`, `unchanged`, or `failed`)
and the separate `outcome` field reports promotion (`created`, `updated`, `unchanged`,
`historical_only`, `conflict`, or `failed`). POST responses match the follow-up ingestion status
response. Conflicts return HTTP 409 while retaining lifecycle status `ingested`; validation errors
return 422 and retryable dependency/audit failures return 503 with `retryable: true`. Search
preserves FHIR Bundle metadata, repeated query parameters, and follows at most
`FHIR_SEARCH_MAX_PAGES` pages (default 10). A returned `next` link can be used to continue when the
page limit is reached.
FHIR response bodies are streamed with a `FHIR_MAX_RESPONSE_BYTES` limit (default 10 MiB) per
upstream response. Successfully verified R4 CapabilityStatements are cached for
`FHIR_CAPABILITY_CACHE_SECONDS` (default 60 seconds); failed checks are not cached. Silo bucket
readiness is cached after a successful check and invalidated when an object write reports that the
bucket is missing.

Ingestion commits its audit attempt before contacting Silo. It stores canonical semantic JSON under
a content-addressed key and persists current JSONB state and each distinct observed version in
PostgreSQL. Current-state ordering uses timezone-aware `meta.lastUpdated`; `meta.versionId` is kept
as opaque metadata and is never sorted. Older or unordered observations are retained as
`historical_only`; equal timestamps with different content are recorded as `conflict`. If no current
record exists, the first observation can initialize it even without a timestamp. Later changed
content is not promoted if either its timestamp or the current record's timestamp is unavailable;
malformed or timezone-naive `lastUpdated` values fail validation.

There is a failure window after a Silo upload and before PostgreSQL commits. A failed database
transaction can leave an unreferenced Silo object. Objects are never automatically deleted. The
durable attempt retains its payload and deterministic object key so the local reconciler can safely
retry the upload/persistence step:

```bash
uv run python -m healthcare_platform.ingestion.reconcile --limit 50 --max-retries 5
```

Attempts have five-minute processing leases, retryable failures wait 30 seconds, and reconciliation
stops after five attempts by default. Use only synthetic resources. Live service checks with
`REQUIRE_INTEGRATION_SERVICES=true uv run pytest -q -ra tests/integration` requires HAPI,
PostgreSQL, and Silo to be healthy and treats any skipped test as a failure. Integration tests leave
synthetic records in the local services. Without mandatory mode, unavailable local services can be
skipped during ordinary unit-test runs.

Raw JSON is preserved semantically after parsing and canonical serialization; original HTTP bytes,
whitespace, and key formatting are not retained byte-for-byte.

Destructive: this deletes local database and object-storage volumes:

```bash
docker compose down -v
```

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| M1 | Foundation: Docker, API, PostgreSQL, FHIR, object storage, CI | Completed |
| M2 | FHIR, HL7 v2, X12, C-CDA ingestion | In progress |
| M3 | Data quality, transformations, warehouse, lineage | Planned |
| M4 | Clinical documents, embeddings, hybrid RAG, evaluation | Planned |
| M5 | Identity, authorization, audit, privacy controls | Planned |
| M6 | Analytics, observability, MLOps/LLMOps | Planned |
| M7 | AWS architecture, IaC, deployment, cost controls | Planned |

M2 progress:

| Submilestone | Scope | Status |
|--------|--------|--------|
| M2.1 | FHIR R4 Integration | (Completed) |
| M2.2 | HL7 v2 Integration | (Completed) |
| M2.3 | C-CDA Integration | (Planned) |
| M2.4 | X12 Integration | (Planned) |
| M2.5 | Unified Ingestion Pipeline | (Planned) |

## HL7 v2 integration (M2.2)

The local development stack includes an inbound MLLP listener at `127.0.0.1:2575` (inside the
Compose network: `hl7-mllp:2575`). It accepts version-configured HL7 v2.5.1 messages by default:
ADT^A01/A02/A03/A04/A08/A40, ORM^O01, and ORU^R01. It emits MLLP-framed AA, AE, or AR
acknowledgments. The listener binds loopback on the host and has no MLLP client authentication;
use it only in the local trusted development environment.

Configure `HL7_MLLP_BIND_HOST`, `HL7_MLLP_PORT`, `HL7_MLLP_HOST_PORT`, `HL7_SUPPORTED_VERSIONS`,
`HL7_MESSAGE_MAX_BYTES`, `HL7_MAX_CONNECTIONS`, connection/read timeouts, and message encoding in
`.env`. The listener admits at most `HL7_MAX_CONNECTIONS` live client sockets; excess connections
are closed immediately before application handler tasks are created. The first frame must begin
within `HL7_CONNECTION_TIMEOUT_SECONDS`; later frames use `HL7_READ_TIMEOUT_SECONDS`. For a host
sender, send a complete VT + ER7 + FS + CR frame
to `127.0.0.1:2575`. The adapter stores exact raw
bytes in Silo and parsed JSONB/metadata plus attempts/history in PostgreSQL. Same sender identity
and MSH-10 with identical bytes is idempotent; changed bytes under the same identity are recorded
as a conflict and are not promoted over the accepted message.

Apply migrations `008_hl7_v2_ingestion.sql`, `009_hl7_bounded_recovery.sql`,
`010_hl7_retry_leases.sql`, and `011_hl7_processing_metrics.sql` to an existing database after the
M2.1 migrations. Fresh databases apply
the matching initialization SQL automatically. The protected status endpoint is
`GET /ingestions/hl7/{attempt_id}` using the configured API bearer
token. The MLLP listener returns `AA` only after raw Silo storage and PostgreSQL audit/state commit.
No automatic Silo object deletion occurs. Use synthetic message data only; this listener is not
intended for production hospital connectivity.

The reconciler atomically claims either an expired `processing` attempt or an expired
`failed`/`retryable` attempt with `FOR UPDATE SKIP LOCKED`. Active claims get a five-minute
`lease_expires_at`; transient failures become eligible again after 30 seconds. `started_at` remains
the original receipt time, retryable failures keep `completed_at` null, terminal outcomes set it, and
`updated_at` changes on state transitions. Processing duration accumulates active generations and
excludes recovery waiting; end-to-end elapsed time runs from receipt to terminal completion. Retry
claims increment `retry_count` and are recorded in retry history. Raw bytes
and the same content-addressed key remain available until recovery succeeds. The retry limit counts
reconciliation claims; exhausted attempts are marked non-retryable and remain auditable. Non-retryable
validation failures and accepted terminal attempts are never retried.

Run the reconciler without deleting raw objects:

```bash
uv run python -m healthcare_platform.ingestion.reconcile_hl7 --limit 100
```

Recovery retries are capped by `HL7_RECONCILIATION_MAX_RETRIES` (default 3). A sender redelivery
after a failed terminal attempt is recorded as a new audited attempt. ACKs remain `AA` only after
raw and PostgreSQL persistence; retryable dependency failures receive `AE`, while unsupported,
malformed A40 merge structure, and identity conflicts follow their documented `AR` behavior.

Aggregate operational counters (without message identifiers) are available at the protected
`GET /integrations/hl7/metrics` endpoint with separately named average processing and end-to-end
elapsed durations. The MLLP listener admits at most `HL7_MAX_CONNECTIONS` live client sockets;
excess connections close immediately instead of waiting in application tasks. The first frame uses
`HL7_CONNECTION_TIMEOUT_SECONDS`, and later frames use `HL7_READ_TIMEOUT_SECONDS`. Keep the listener
loopback-bound because MLLP transport authentication is not enabled.


## AWS direction

| Local component | AWS target |
|---|---|
| SILO | Amazon S3 |
| PostgreSQL | RDS for PostgreSQL / Aurora PostgreSQL |
| FastAPI container | ECS or another approved container runtime |
| Pipelines | Glue / Step Functions, depending on workload |
| LLM | Amazon Bedrock |
| BI | Amazon QuickSight |
| Secrets | Secrets Manager |
| Encryption | KMS |
| Monitoring | CloudWatch |

The local Compose setup creates no AWS resources. AWS service choices will be validated against security, workload, and cost requirements before deployment.

## Development principles

- Synthetic data only; never commit PHI, credentials, `.env`, or production exports.
- Keep changes small and tested.
- Use feature branches and pull requests.
- Require human approval for security-sensitive changes and cloud operations.
- Record material architecture decisions in `docs/adr/`.

## License

MIT. See [LICENSE](LICENSE).

# Healthcare AI Data Platform

A modular portfolio project for healthcare data engineering, interoperability, analytics, secure AI data access, and AWS-oriented architecture.

> **Safety:** This is an engineering simulation, not a clinical product or HIPAA-certified system. Use synthetic or legally authorized data only. Never commit real PHI or credentials.

## M1 — Foundation

This starter provides FastAPI, PostgreSQL + pgvector, HAPI FHIR, MinIO, pytest, Ruff, GitHub Actions CI, and initial architecture/agent governance documentation.

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
| FastAPI docs | http://localhost:8000/docs |
| HAPI FHIR metadata | http://localhost:8080/fhir/metadata |
| MinIO console | http://localhost:9001 |
| PostgreSQL | localhost:5432 |

MinIO credentials are in `.env`. Change all development placeholders before sharing the environment.

## Local Python development

```bash
uv sync --all-groups
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run uvicorn healthcare_platform.main:app --reload
```

When running the API directly on the host, set `DATABASE_URL` to use `localhost` instead of `postgres`.

## Useful commands

```bash
docker compose logs -f api
docker compose down
```

Destructive: this deletes local database and object-storage volumes:

```bash
docker compose down -v
```

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| M1 | Foundation: Docker, API, PostgreSQL, FHIR, object storage, CI | In progress |
| M2 | FHIR, HL7 v2, X12, C-CDA ingestion | Planned |
| M3 | Data quality, transformations, warehouse, lineage | Planned |
| M4 | Clinical documents, embeddings, hybrid RAG, evaluation | Planned |
| M5 | Identity, authorization, audit, privacy controls | Planned |
| M6 | Analytics, observability, MLOps/LLMOps | Planned |
| M7 | AWS architecture, IaC, deployment, cost controls | Planned |

## AWS direction

| Local component | AWS target |
|---|---|
| MinIO | Amazon S3 |
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

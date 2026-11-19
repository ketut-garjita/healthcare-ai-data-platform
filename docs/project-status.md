# Project Status

## Current milestone

M1 — Foundation & Infrastructure

## Acceptance checklist

- [ ] Docker Compose starts all services successfully.
- [ ] FastAPI `/health` returns HTTP 200.
- [ ] FastAPI `/ready` returns HTTP 200 when PostgreSQL is reachable.
- [ ] PostgreSQL has the `vector` extension enabled.
- [ ] HAPI FHIR metadata endpoint responds.
- [ ] MinIO console is reachable.
- [ ] Unit tests pass.
- [ ] Ruff lint and format checks pass.
- [ ] GitHub Actions CI passes on a pull request.

## Known limitations

- HAPI FHIR uses its default container configuration; persistent production-grade database
  configuration is outside M1.
- MinIO uses a floating `latest` tag in this starter; pin a tested version before claiming
  reproducible infrastructure.
- Local credentials are development-only placeholders.
- Authentication and patient authorization are not implemented yet.
- No AWS resources are provisioned.

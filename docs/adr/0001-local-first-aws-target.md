# ADR-0001: Local-first development with AWS as target cloud

- Status: Accepted
- Date: 2026-11-19

## Context

The project must be reproducible on a developer workstation and demonstrate architecture relevant
to an AWS-oriented AI & Data Architect role. AWS credits are limited or unavailable.

## Decision

Develop and test core components locally with Docker Compose. Use S3-compatible MinIO and
PostgreSQL + pgvector locally. Document migration paths to AWS, and provision cloud resources
only in a later, explicitly approved milestone.

## Consequences

- Most development and tests run without cloud charges.
- Local behavior is not identical to managed AWS services.
- IAM, KMS, networking, managed service behavior, and deployment must be validated separately.
- Any AWS deployment requires cost review, least-privilege permissions, and human approval.

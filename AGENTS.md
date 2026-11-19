# Repository Instructions for Coding Agents

## Mission

Build a modular, secure-by-design healthcare data engineering and AI portfolio platform.
This is an educational engineering simulation, not a clinical product.

## Mandatory constraints

1. Use synthetic or explicitly authorized data only. Never introduce real PHI.
2. Never commit `.env`, credentials, tokens, private keys, or database dumps.
3. Do not claim HIPAA compliance or clinical safety certification.
4. Do not deploy to AWS, run Terraform apply/destroy, or incur cloud costs without human approval.
5. Do not perform destructive database or volume operations without human approval.
6. Prefer small changes with tests and documentation.
7. Do not merge directly to `main`.

## Engineering standards

- Python 3.12+.
- Use `uv` for local dependency management.
- Follow Ruff linting and formatting.
- Add type hints to application code.
- Keep API routes thin; put business logic in service modules.
- Use environment-based configuration; never hardcode secrets.
- Add unit tests for new logic and integration tests for external-service behavior.
- Preserve source provenance for ingested healthcare data.
- Enforce authorization server-side, not through prompt instructions.

## Workflow

1. Read README and relevant architecture docs.
2. Inspect existing code before editing.
3. State a short implementation plan and acceptance criteria.
4. Implement on a feature branch.
5. Run relevant tests, lint, and formatting checks.
6. Update documentation and ADRs when architecture changes.
7. Summarize files changed, checks run, limitations, and follow-up work.

Initial roles are documented in `docs/agentic-engineering/agent-responsibilities.md`.

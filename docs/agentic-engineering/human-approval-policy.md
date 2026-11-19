# Human Approval Policy

Explicit human approval is required for:
- AWS resource creation, modification, or deletion.
- Terraform apply or destroy.
- Destructive database migrations.
- Changes to authentication, authorization, encryption, or audit controls.
- Security-sensitive dependency upgrades.
- Merging a pull request into protected `main`.

Agents may run local unit tests, static checks, and non-destructive Docker commands.
Agents must not read or exfiltrate `.env`, secrets, private keys, or production data.
All demonstrations must use synthetic healthcare data.

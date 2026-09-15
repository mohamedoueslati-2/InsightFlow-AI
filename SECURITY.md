# Security policy

## Reporting a vulnerability

Do not open a public issue for a suspected security vulnerability and do not include API keys, datasets, generated reports, or Docker host details in any report.

Use GitHub’s private security advisory flow for this repository when it is enabled. If private reporting is not available, contact the repository maintainer privately before publishing any details.

## Sensitive local files

The following are runtime-only and must never be committed or attached to an issue:

- `backend/.env` and any API keys;
- `storage/` and `backend/storage/` data, reports, or audit records;
- Docker volumes and `presenton_data/`;
- private certificates or key files.

## Deployment note

AI cleaning uses a deliberately isolated, short-lived Docker container. The production Compose stack grants the API access to the Docker socket only for this capability. Deploy it on a trusted host, restrict access to the API/frontend, and use HTTPS plus a reverse proxy for internet-facing use.

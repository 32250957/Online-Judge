# Security configuration

## Required production configuration

- Set a strong `POSTGRES_PASSWORD`. Docker Compose now requires it and binds PostgreSQL only to localhost.
- Set a long, random `SESSION_SECRET_KEY` and keep it stable between web restarts.
- Set `SESSION_COOKIE_SECURE=1` when the service is reached through HTTPS.
- Provide `OJ_BOOTSTRAP_ADMIN_USERNAME` and a unique `OJ_BOOTSTRAP_ADMIN_PASSWORD` (at least 12 characters) only for the first initialization. Remove both afterward.
- Do not expose PostgreSQL or the Docker socket beyond trusted hosts. Judge workers require the Docker socket and therefore must be treated as privileged infrastructure.

Example secret generation:

```sh
python -c 'import secrets; print(secrets.token_urlsafe(64))'
```

## Upload limits

Public images are validated by file signature and limited by `MAX_IMAGE_UPLOAD_BYTES` (default 5 MiB). Private school-group application attachments are limited by `MAX_PRIVATE_ATTACHMENT_BYTES` (default 10 MiB) and are downloaded only through an authorized route. Unsafe HTTP requests with a declared body larger than `MAX_REQUEST_BODY_BYTES` (default 20 MiB) are rejected before CSRF form parsing.

## Remaining operational risks

- Add TLS and request-rate limiting at the reverse proxy, especially for `/login` and submission endpoints.
- Restrict worker hosts and Docker socket access. A Docker socket is effectively root-equivalent access to its host.
- Run dependency and container-image vulnerability scans in CI and update pinned dependencies regularly.

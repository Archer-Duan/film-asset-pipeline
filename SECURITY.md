# Security Policy

## Reporting a vulnerability

Please do not open a public issue for credential exposure or another security vulnerability. Use GitHub's private vulnerability reporting feature for this repository, or contact the maintainer through the private address listed in the repository profile.

Include the affected version, reproduction steps, impact, and any suggested mitigation. Do not include real API keys, film frames, generated assets, or account identifiers.

## Credential handling

- API credentials are supplied by each user and stored only on that user's computer.
- The application never returns full credentials through its HTTP API.
- Real `.env`, `config.toml`, `user-settings.json`, logs, databases, source frames, generated images, and models must not be committed.
- If a credential appears in a commit, screenshot, issue, or log, revoke and replace it immediately. Rewriting Git history alone is not sufficient.

The local web server binds to `127.0.0.1` by default. Do not expose it to a LAN or the public internet without adding authentication, CSRF protection, TLS, and an explicit security review.

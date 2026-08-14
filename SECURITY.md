# Security and privacy

This repository is a source-only demonstration. It intentionally contains no
production roster evidence, Telegram identifiers, credentials, runtime
databases, logs, account names, or delivery targets.

## Safe configuration

- Copy `.env.example` to a machine-local `.env`; never commit the result.
- Keep the default `WIFE_ROSTER_NOTIFIER=console` until a private delivery
  target has been configured and tested.
- Store Telegram sender and group allowlists only in protected runtime
  configuration.
- Treat uploaded rosters, transcriptions, review JSON, SQLite state, scheduler
  signals, and dispatcher logs as sensitive personal data.
- Rotate a credential immediately if it is ever committed, even if the commit
  is later removed.

## Reporting a vulnerability

Please open a GitHub security advisory for code-level vulnerabilities. Do not
include real credentials, identifiers, roster files, or runtime output in a
public issue.

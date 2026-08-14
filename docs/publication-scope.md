# Public snapshot boundary

This repository was assembled as a fresh, source-only snapshot. It does not
share Git history with the production workspace.

## Included

- Deterministic Python normalization, validation, timezone, diff, alert,
  database, scheduler, dispatcher, and notifier boundaries.
- The narrow OpenClaw Telegram review plugin.
- Unit and integration tests using fictional flights, dates, identities, and
  delivery targets.
- An inert `.env.example`, architecture documentation, CI, and an MIT license.

## Deliberately excluded

- `.env` files, tokens, API keys, account names, bot credentials, Telegram chat
  IDs, sender IDs, and delivery targets.
- Uploaded images or PDFs, transcription output, review state, real schedule
  data, and screenshots.
- SQLite databases and their WAL/SHM files, backups, logs, scheduler signals,
  launchd plists, and other runtime state.
- Virtual environments, installed dependencies, caches, generated package
  metadata, and the deployed application copy.
- Production Git history and machine-specific configuration.

The fictional `ZX` carrier, 2037 dates, and `test-*` identities in the test
suite are documentation fixtures only. They are not derived identifiers and
must not be replaced with real data in source control.

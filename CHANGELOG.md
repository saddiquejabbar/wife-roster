# Changelog

## 0.3.0 - 2026-08-13

- Replaced five-minute polling with a watch-driven planner and one calendar wake.
- Added schedule-generation and planner synchronization state.
- Added edit-aware rearming and full calendar-month roster replacement.
- Supersede pending, failed, and in-flight obsolete alerts without stale retries.
- Added three-attempt bounded delivery with 5/15-minute backoff and attempt history.
- Expanded health/status output for schedule, alerts, attempts, and delivery.
- Hardened runtime files, directories, scheduler files, logs, and umask.
- Allowed an explicit second Telegram state-change sender for spouse-managed edits.
- Selected `openai/gpt-5.6-sol` as the bounded roster image extractor.
- Added scheduler, replacement, retry, history, and permission regression tests.

## 0.2.1 - 2026-08-13

- Added Telegram Approve/Revise buttons and natural-language roster-run alias.

## 0.2.0 - 2026-08-10

- Added private Telegram review/approval integration and production deployment.

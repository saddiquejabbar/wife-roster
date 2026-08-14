# Scheduling architecture

## Event flow

```text
Telegram roster upload
  -> bounded vision transcription
  -> deterministic normalization and validation
  -> Approve / Revise review
  -> transactional roster reconciliation
  -> schedule_generation + 1
  -> planner signal
  -> one launchd calendar wake
  -> dispatcher claim/send/result
  -> planner signal for the next wake
```

The database is authoritative. launchd stores only the next wake and never owns
flight logic. A stale launchd invocation cannot send a superseded database row.

## Planner and dispatcher

The planner runs at login and whenever `runtime/private/schedule.request`
changes. It selects the earliest of:

1. a pending alert's `due_utc`;
2. a failed alert's `retry_not_before`; or
3. a sending alert's lease-expiry time.

It rewrites `org.example.wife-roster.dispatcher.plist` with one
`StartCalendarInterval`. If there is no candidate, it unloads and removes that
plist. Minute precision is intentional; seconds are rounded up.

The dispatcher recovers expired claims, marks out-of-grace alerts missed,
atomically moves eligible alerts from pending to sending, checks that the claim
still belongs to the current schedule generation, delivers through the selected
notifier, records the attempt, and requests another plan.

## Replacement and duplicate rules

- FULL evidence replaces every duty in each calendar month covered by the
  printed period. A period ending on the first of a month treats that date as an
  exclusive boundary; a period ending on the last day remains inclusive.
- PARTIAL or UNCERTAIN evidence replaces only matched sectors and retains all
  unrelated duties.
- Event keys describe alert type, flight, route, and event UTC time. They do not
  include roster version, so an unchanged event is not duplicated.
- Obsolete pending, failed, and sending alerts become superseded in the same
  transaction as roster activation. Retry times are cleared. An in-flight
  attempt is recorded as abandoned.
- Sent rows remain immutable delivery history. Re-uploading the same file set or
  equivalent normalized roster does not advance the generation.

## Retry state machine

```text
pending -> sending -> sent
                   -> failed --5m--> pending
                             --15m-> pending
                             --third failure--> failed (terminal)

pending/failed/sending --roster edit--> superseded
pending/failed/sending --past grace--> missed
```

Each claim inserts one `delivery_attempts` record keyed by event, schedule
generation, and attempt number. A successful attempt stores its notification ID;
failures and abandoned leases store bounded error text.

## Health checks

`roster status` reports active roster/version counts, all alert states, desired
and planned schedule generations, attempt/failure totals, and last successful
delivery. `roster scheduler-status` reports planner install/load state, whether
the dispatcher is armed, its UTC time and generation, the last dispatch, and the
next pending alert.

A healthy active schedule has:

- planner installed and loaded;
- desired and planned generations equal;
- a dispatcher wake armed when actionable alerts exist;
- no legacy `org.example.wife-roster.plist`; and
- the OpenClaw notifier target set to the intended Telegram group.

## Operational dependencies

At delivery time the Mac must be powered on or wakeable, the user LaunchAgent
session must be available, the production app/database paths must be readable,
OpenClaw and its Telegram account must be working, and network access must be
available. The design recovers recent late wakes inside alert-specific grace but
does not change macOS power settings.

## Privacy and publication

Production data is outside the Git app bundle. Runtime directories use `0700`;
database, `.env`, logs, signals, review state, and uploaded evidence use `0600`.
Do not commit `.env`, Telegram IDs, tokens, real roster images, SQLite files, or
logs when publishing the repository.

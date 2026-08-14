# wife-roster Stage 1 contract

Stage 1 accepts one or more image/PDF sources as one candidate roster, pins the
raw transcription schema, and deterministically performs row-date attribution,
fragment merging, `Duty == FLY` filtering, duty/sector grouping, airport and
timezone resolution, validation, version-independent hashing, diffing, display,
alert calculation, and SQLite persistence.

Roster times are port-local and displayed first in 24-hour `HHMM`. An event
outside Singapore appends its Singapore equivalent as `(HHMM SG)`, even when
the offsets happen to match. User output never shows timezone abbreviations,
UTC offsets, IANA names, or day-offset markers. Summaries and landing alerts use
city plus full country; pre-flight alerts use flight number and IATA route.

Only rows explicitly printed with duty `FLY` may start a sector. Blank-duty rows
may only contribute complementary fields to a preceding, identifiable FLY
sector. A printed date belongs to its row; an undated row inherits only the
nearest printed date above. Each time retains its own attributed row date.

A duty starts only at a printed RPT. Continuous later sectors without RPT may
join it; no RPT is invented. A sector needs flight, route, STD, STA, known
airports, resolvable local times, and no critical unreadable cells. Invalid
events produce `NEEDS REVIEW` and are not scheduled; unrelated valid events may
continue.

Alert plans contain one 12-hour and one 3-hour alert per valid future duty and
one 1-hour-before-STA landing alert per valid future sector. Event keys use the
event itself, never a roster version. SQLite preserves sent and attempt history.
A FULL roster replaces all duties in its covered calendar month(s), while
PARTIAL/UNCERTAIN evidence never deletes unrelated duties. Obsolete unsent
alerts are superseded transactionally.

No Telegram sending, launchd changes, OpenClaw integration, n8n integration,
continuous polling, or macOS configuration belongs to Stage 1.

## Stage 2

Stage 2 adds a planner agent (`org.example.wife-roster.planner`) and a calendar
dispatcher agent (`org.example.wife-roster.dispatcher`). The planner is driven by
RunAtLoad plus a private WatchPaths signal. It writes one calendar wake for the
next database-derived alert, retry, or sending-lease recovery. No StartInterval,
continuous poller, or per-flight agent is permitted. SQLite remains the only
alert authority.

The dispatcher recovers overdue rows, atomically claims `pending` rows as
`sending`, calls the notifier, then records `sent` plus `notification_id` or
`failed` plus `last_error` and `retry_not_before`. Each claim creates a durable
delivery-attempt row. Concurrent or repeated invocations cannot claim a
sent/sending row. Recovery grace is 60 minutes for 12-hour alerts, 30 minutes
for 3-hour alerts, and 20 minutes for landing alerts; anything older is missed.
Retries are bounded to three total attempts with 5- and 15-minute backoff and
are omitted when the next attempt would exceed grace.

Stage 2 provides only `ConsoleNotifier`. It does not contain Telegram, OpenClaw,
n8n, roster parsing loops, pmset changes, sleep changes, sudo, or one-job-per-
flight scheduling. The retired five-minute agent is unloaded and removed during
refresh.

## Stage 2.5

Development stays in the Git workspace under `Documents`. Production deploys
to `~/Library/Application Support/wife-roster/`, where launchd can execute it
without privacy-setting changes. The deployment command copies only package
source, prompts, safe metadata, and the required airport runtime package into
`app/`; it creates an isolated Python venv without relying on PATH or aliases.

Stable state lives outside the replaceable bundle at `runtime/state.db`,
`runtime/logs/`, and `runtime/private/`. Redeployment never copies development
runtime data or `.env`, and never replaces an existing production database,
private directory, or root `.env`. Both launchd labels use absolute Application
Support paths. Runtime/private directories are `0700`, sensitive files are
`0600`, and launchd uses umask `0077`.

## Notification adapter (active in production)

The dispatcher continues to depend only on the `Notifier` interface. Available
implementations are `ConsoleNotifier`, `OpenClawNotifier`, and
`TelegramNotifier`. `ConsoleNotifier` remains the fail-safe default.
`OpenClawNotifier` is the explicit production selection; direct Telegram is
retained only as an inactive fallback.

`WIFE_ROSTER_NOTIFIER` requires the exact value `console`, `openclaw`, or
`telegram`. Missing configuration selects Console. Unknown configuration fails
closed, and the presence of credentials never changes the selection.

`OpenClawNotifier` is a subprocess-only adapter around the existing absolute
OpenClaw CLI. It invokes `message send --channel telegram`, provides the already
rendered deterministic alert unchanged, requires a successful process plus a
confirmed top-level `messageId`, and records
`openclaw:telegram:<messageId>`. It imports no OpenClaw code and invokes no
agent, AI, or model. Because the installed CLI has no stdin/text-file body
option, the alert is passed with `--message` and briefly appears in the local
process argument list.

A controlled notifier delivery failure is recorded as `failed` in SQLite. The
planner arms its explicit retry time, if any. Unexpected notifier or program
failures remain non-zero. Scheduled delivery never invokes an AI/model call.

## Inbound review and approval hook

Inbound Telegram integration is a native OpenClaw plugin, outside the Python
roster package. Its terminal `reply_dispatch` hook handles only the configured
Telegram group, configured numeric senders, and exact commands that satisfy the
existing mention policy. It does not weaken group, DM, privacy, or wildcard
policy. Unrelated turns remain available to the normal assistant path.

`run wife-roster` requires an explicit bot mention and one or more PDF, PNG,
JPG, or JPEG attachments. With Telegram Privacy Mode enabled, the reliable
caption is `/run_wife_roster@RosterDemoBot`; the normalized command remains
`run wife-roster`. A Telegram media album is one candidate. Sources are
copied to a unique private ingestion directory, and an explicitly configured
image-capable OpenClaw structured extractor produces only the pinned raw
transcription schema. It must not inherit an unrelated general agent model. The
Python review service remains authoritative for normalization, validation, timezone
conversion, summaries, hashes, diffs, and proposed alerts. Review never applies
or initializes SQLite.

Approve/Revise callbacks require the explicit state-change sender allowlist.
Both spouses may be configured privately; no real sender identifier belongs in
source control. Before apply the workflow verifies ordered source hashes and sizes,
transcription evidence, normalized content, coverage, report period, and the
complete review-issue set. Any issue or mismatch blocks apply. The existing
transactional `RosterDatabase.apply_roster()` remains the sole activation path,
and its event-key uniqueness remains the alert idempotency guard. Before the
activation confirmation is returned, the command boundary verifies the active
content hash, production `OpenClawNotifier`, planner availability, pending
event-key uniqueness, and calendar rearming.

The inbox and ingestion directories are mode `0700`; sources, transcription,
state, and lock files are mode `0600`. No Telegram credential enters the
wife-roster repository or runtime configuration. Inbound extraction defaults to
the explicitly bounded `openai/gpt-5.6-sol` model; scheduled outbound delivery
never uses a model.

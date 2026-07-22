# Status Cache Retention Design

## Goal

Reduce recurring status-cache I/O without changing the widget's visible behavior,
animation timing, session ordering, compatibility files, or status semantics.

## Scope

This change covers only structured session records under
`~/.cache/ai-cli-status-monitor/statuses/`:

- parse every retained status file at most once per collection pass;
- ignore records whose filesystem modification time is older than the configured
  `hide_stale_after_seconds` threshold before parsing their JSON;
- remove those expired records opportunistically when the hook processes an event;
- keep fresh structured records, legacy records, and malformed-record handling
  compatible with current behavior;
- continue writing `claude.json`, `codex.json`, per-agent text files, and
  `combined.txt`.

Animations, animation timers, provider-usage refresh, `wmctrl`, widget-position
persistence, and cache directories other than `statuses/` are outside this change.

## Design

Add a dependency-free status-store helper under `bin/ai_agent_status_lib/`. The
helper will enumerate `*.json` records, classify expiration using file modification
time and a caller-provided retention duration, optionally remove expired records,
parse each retained file once, and return valid dictionary records ordered by their
`timestamp_iso` value. An optional diagnostic callback will let the widget preserve
its current malformed-file logging while the hook can continue ignoring malformed
records silently.

The hook will call the helper with cleanup enabled while regenerating
`combined.txt`. This reuses its existing collection pass, so cleanup adds no second
directory scan. The widget will call the same helper with cleanup disabled. It will
therefore avoid parsing expired files even before another hook event has had an
opportunity to remove them.

The two latest compatibility files remain fallbacks only when the structured status
directory has no retained records, matching the current fallback behavior. An
expired structured file must not prevent that fallback.

## Retention and failure behavior

The retention duration is the configured `hide_stale_after_seconds` value. A record
is expired when its modification time is at least that duration old. A negative age
caused by clock skew is treated as fresh.

Failure to stat, read, parse, or remove one record must not stop processing other
records. Removal failures are ignored as best-effort cache maintenance. Malformed
fresh JSON remains excluded from returned statuses, as it is today.

Only files matching `statuses/*.json` may be removed. Latest-agent compatibility
files, raw payloads, debug payloads, usage data, configuration, logs, and credentials
must never be touched by retention.

## Testing

Standard-library unit tests will cover:

- fresh records are parsed, returned, and ordered;
- expired records are skipped without being parsed;
- cleanup removes only expired `*.json` records inside the status directory;
- cleanup-disabled widget reads leave expired files on disk;
- malformed fresh records and filesystem failures do not block valid records;
- an expired-only structured directory permits compatibility-file fallback;
- hook compatibility output contains retained sessions after cleanup.

Verification will include the focused tests, the full `unittest` suite, Python
compilation, isolated hook test runs with a temporary cache, and a GTK demo smoke
test. The live cache will not be used as test data and will not be deleted during
development or verification.

## Success criteria

- The widget no longer parses expired structured session records every second.
- Each retained structured record is decoded no more than once per collection pass.
- A hook event removes expired structured records on a best-effort basis.
- Existing visible session behavior and compatibility outputs remain unchanged for
  retained records.
- No animation code or timing constants change.

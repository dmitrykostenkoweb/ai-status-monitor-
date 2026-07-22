# Status Cache Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the widget and hook from repeatedly parsing expired structured status files while preserving all visible behavior and compatibility outputs.

**Architecture:** Add one dependency-free status-store module that owns discovery, modification-time retention, best-effort cleanup, one-pass JSON decoding, diagnostics, and timestamp ordering. The hook uses it with cleanup enabled; the widget uses it read-only and falls back to the latest-agent compatibility files when no retained structured files remain.

**Tech Stack:** Python 3 standard library, `unittest`, GTK3/PyGObject smoke tests through `xvfb-run`.

## Global Constraints

- Do not change animation code, animation timers, or timing constants.
- Preserve `claude.json`, `codex.json`, per-agent text files, and `combined.txt`.
- Remove only expired `*.json` files directly inside the configured `statuses/` directory.
- Use `hide_stale_after_seconds` as the exact retention duration.
- Treat clock-skewed negative file ages as fresh.
- Filesystem and malformed-JSON failures must not stop other records from loading.
- Do not read, mutate, or delete the live cache during tests or verification.

---

### Task 1: Add the shared retained-status reader

**Files:**
- Create: `bin/ai_agent_status_lib/status_store.py`
- Create: `tests/test_status_store.py`

**Interfaces:**
- Produces: `StatusRecord = tuple[Path, dict[str, Any]]`.
- Produces: immutable `StatusCollection(records: list[StatusRecord], has_retained_files: bool)`.
- Produces: `collect_status_records(status_dir: Path, *, retention_seconds: int, remove_expired: bool = False, now: float | None = None, diagnostic: Callable[[str], None] | None = None) -> StatusCollection`.
- Produces: `read_status_paths(paths: Iterable[Path], *, diagnostic: Callable[[str], None] | None = None) -> StatusCollection` for the two compatibility files, without retention or deletion.

- [ ] **Step 1: Write failing unit tests for ordering, one-pass expiry, cleanup, and diagnostics**

Create `tests/test_status_store.py` with tests structured as follows:

```python
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from ai_agent_status_lib.status_store import collect_status_records
from ai_agent_status_lib.status_store import read_status_paths


class StatusStoreTests(unittest.TestCase):
    def write_record(self, path: Path, timestamp: str, mtime: float) -> None:
        path.write_text(json.dumps({"timestamp_iso": timestamp, "status": path.stem}), encoding="utf-8")
        os.utime(path, (mtime, mtime))

    def test_returns_fresh_records_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status_dir = Path(directory)
            self.write_record(status_dir / "older.json", "2026-07-22T10:00:00", 950.0)
            self.write_record(status_dir / "newer.json", "2026-07-22T11:00:00", 960.0)

            collection = collect_status_records(
                status_dir,
                retention_seconds=100,
                now=1_000.0,
            )

            self.assertTrue(collection.has_retained_files)
            self.assertEqual(
                [record[1]["status"] for record in collection.records],
                ["newer", "older"],
            )

    def test_expired_malformed_record_is_not_parsed_or_removed_in_read_only_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status_dir = Path(directory)
            expired = status_dir / "expired.json"
            expired.write_text("{malformed", encoding="utf-8")
            os.utime(expired, (800.0, 800.0))
            messages: list[str] = []

            collection = collect_status_records(
                status_dir,
                retention_seconds=100,
                now=1_000.0,
                diagnostic=messages.append,
            )

            self.assertFalse(collection.has_retained_files)
            self.assertEqual(collection.records, [])
            self.assertEqual(messages, [])
            self.assertTrue(expired.exists())

    def test_cleanup_removes_only_expired_json_in_status_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status_dir = Path(directory)
            expired = status_dir / "expired.json"
            fresh = status_dir / "fresh.json"
            unrelated = status_dir / "keep.txt"
            self.write_record(expired, "2026-07-22T09:00:00", 800.0)
            self.write_record(fresh, "2026-07-22T11:00:00", 950.0)
            unrelated.write_text("keep", encoding="utf-8")
            os.utime(unrelated, (800.0, 800.0))

            collection = collect_status_records(
                status_dir,
                retention_seconds=100,
                remove_expired=True,
                now=1_000.0,
            )

            self.assertFalse(expired.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(unrelated.exists())
            self.assertEqual([record[0] for record in collection.records], [fresh])

    def test_fresh_malformed_record_reports_diagnostic_without_blocking_valid_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status_dir = Path(directory)
            malformed = status_dir / "malformed.json"
            malformed.write_text("{malformed", encoding="utf-8")
            os.utime(malformed, (950.0, 950.0))
            valid = status_dir / "valid.json"
            self.write_record(valid, "2026-07-22T11:00:00", 960.0)
            messages: list[str] = []

            collection = collect_status_records(
                status_dir,
                retention_seconds=100,
                now=1_000.0,
                diagnostic=messages.append,
            )

            self.assertTrue(collection.has_retained_files)
            self.assertEqual([record[0] for record in collection.records], [valid])
            self.assertEqual(len(messages), 1)
            self.assertIn(str(malformed), messages[0])

    def test_compatibility_paths_are_parsed_once_and_ordered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude = root / "claude.json"
            codex = root / "codex.json"
            self.write_record(claude, "2026-07-22T10:00:00", 100.0)
            self.write_record(codex, "2026-07-22T11:00:00", 100.0)

            collection = read_status_paths((claude, codex))

            self.assertTrue(collection.has_retained_files)
            self.assertEqual([record[0] for record in collection.records], [codex, claude])

    def test_stat_failure_treats_record_as_fresh_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status_dir = Path(directory)
            uncertain = status_dir / "uncertain.json"
            valid = status_dir / "valid.json"
            self.write_record(uncertain, "2026-07-22T10:00:00", 950.0)
            self.write_record(valid, "2026-07-22T11:00:00", 960.0)
            real_stat = Path.stat
            messages: list[str] = []

            def selective_stat(path: Path, *args: object, **kwargs: object):
                if path == uncertain:
                    raise OSError("stat failed")
                return real_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", autospec=True, side_effect=selective_stat):
                collection = collect_status_records(
                    status_dir,
                    retention_seconds=100,
                    now=1_000.0,
                    diagnostic=messages.append,
                )

            self.assertEqual(
                [record[0] for record in collection.records],
                [valid, uncertain],
            )
            self.assertTrue(any("failed to stat" in message for message in messages))

    def test_remove_failure_keeps_processing_fresh_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status_dir = Path(directory)
            expired = status_dir / "expired.json"
            fresh = status_dir / "fresh.json"
            self.write_record(expired, "2026-07-22T09:00:00", 800.0)
            self.write_record(fresh, "2026-07-22T11:00:00", 950.0)

            with patch.object(Path, "unlink", side_effect=OSError("remove failed")):
                collection = collect_status_records(
                    status_dir,
                    retention_seconds=100,
                    remove_expired=True,
                    now=1_000.0,
                )

            self.assertTrue(expired.exists())
            self.assertEqual([record[0] for record in collection.records], [fresh])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_status_store.py' -v
```

Expected: `ERROR` because `ai_agent_status_lib.status_store` does not exist.

- [ ] **Step 3: Implement the minimal shared reader**

Create `bin/ai_agent_status_lib/status_store.py` with this public structure and behavior:

```python
"""Bounded, one-pass access to structured agent status records."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


Diagnostic = Callable[[str], None]
StatusRecord = tuple[Path, dict[str, Any]]


@dataclass(frozen=True)
class StatusCollection:
    records: list[StatusRecord]
    has_retained_files: bool


def _timestamp(record: StatusRecord) -> str:
    value = record[1].get("timestamp_iso")
    return value if isinstance(value, str) else ""


def _read_records(paths: Iterable[Path], diagnostic: Diagnostic | None) -> StatusCollection:
    records: list[StatusRecord] = []
    has_files = False
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (json.JSONDecodeError, OSError) as error:
            has_files = True
            if diagnostic is not None:
                diagnostic(f"failed to read status json {path}: {error}")
            continue
        has_files = True
        if isinstance(value, dict):
            records.append((path, value))
    records.sort(key=_timestamp, reverse=True)
    return StatusCollection(records, has_files)


def read_status_paths(
    paths: Iterable[Path],
    *,
    diagnostic: Diagnostic | None = None,
) -> StatusCollection:
    return _read_records(paths, diagnostic)


def collect_status_records(
    status_dir: Path,
    *,
    retention_seconds: int,
    remove_expired: bool = False,
    now: float | None = None,
    diagnostic: Diagnostic | None = None,
) -> StatusCollection:
    current = time.time() if now is None else now
    retained: list[Path] = []
    try:
        paths = list(status_dir.glob("*.json"))
    except OSError as error:
        if diagnostic is not None:
            diagnostic(f"failed to list status directory {status_dir}: {error}")
        return StatusCollection([], False)

    for path in paths:
        try:
            age = current - path.stat().st_mtime
        except OSError as error:
            if diagnostic is not None:
                diagnostic(f"failed to stat status json {path}: {error}")
            retained.append(path)
            continue
        if age >= retention_seconds:
            if remove_expired:
                try:
                    path.unlink()
                except OSError:
                    pass
            continue
        retained.append(path)

    return _read_records(retained, diagnostic)
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_status_store.py' -v
```

Expected: 7 tests pass.

- [ ] **Step 5: Commit the shared reader**

```bash
git add bin/ai_agent_status_lib/status_store.py tests/test_status_store.py
git commit -m "feat(status): add retained status reader"
```

---

### Task 2: Prune expired records during hook updates

**Files:**
- Modify: `bin/ai-agent-status-hook:12-14,169-189`
- Create: `tests/test_status_hook.py`

**Interfaces:**
- Consumes: `collect_status_records(...) -> StatusCollection` from Task 1.
- Consumes: `read_status_paths(...) -> StatusCollection` from Task 1.
- Preserves: `read_status_files() -> list[dict[str, Any]]` and `update_combined() -> str`.

- [ ] **Step 1: Write a failing isolated hook integration test**

Create `tests/test_status_hook.py`:

```python
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "bin" / "ai-agent-status-hook"


class StatusHookRetentionTests(unittest.TestCase):
    def test_hook_removes_expired_status_and_preserves_combined_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status_dir = root / "cache" / "statuses"
            status_dir.mkdir(parents=True)
            expired = status_dir / "claude-expired.json"
            expired.write_text(
                json.dumps({
                    "agent": "claude",
                    "status": "Claude: old",
                    "timestamp_iso": "2026-07-20T10:00:00",
                }),
                encoding="utf-8",
            )
            old = time.time() - 901
            os.utime(expired, (old, old))
            env = dict(os.environ)
            env.update({
                "AI_STATUS_CACHE_DIR": str(root / "cache"),
                "AI_STATUS_CONFIG_DIR": str(root / "config"),
                "AI_STATUS_DATA_DIR": str(root / "data"),
                "AI_STATUS_HIDE_STALE_AFTER_SECONDS": "900",
            })
            payload = json.dumps({
                "hook_event_name": "PreToolUse",
                "tool_name": "Read",
                "cwd": "/tmp/perf-audit",
                "session_id": "fresh-codex-session",
            })

            completed = subprocess.run(
                [sys.executable, str(HOOK), "--agent", "codex"],
                input=payload,
                text=True,
                env=env,
                cwd=ROOT,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(expired.exists())
            combined = (root / "cache" / "combined.txt").read_text(encoding="utf-8")
            self.assertIn("Codex: reading", combined)
            self.assertNotIn("Claude: old", combined)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the hook test and verify RED**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_status_hook.py' -v
```

Expected: `FAIL` because `claude-expired.json` still exists.

- [ ] **Step 3: Route hook collection through the shared reader**

In `bin/ai-agent-status-hook`, import the two Task 1 functions:

```python
from ai_agent_status_lib.status_store import collect_status_records
from ai_agent_status_lib.status_store import read_status_paths
```

Delete the local `status_timestamp()` helper and replace `read_status_files()` with:

```python
def read_status_files() -> list[dict[str, Any]]:
    collection = collect_status_records(
        STATUS_DIR,
        retention_seconds=SETTINGS.hide_stale_after_seconds,
        remove_expired=True,
    )
    if not collection.has_retained_files:
        collection = read_status_paths(CACHE_DIR / f"{agent}.json" for agent in AGENTS)
    return [status for _path, status in collection.records]
```

Do not change `handle_payload()`, compatibility writes, or `update_combined()`.

- [ ] **Step 4: Run the focused hook and store tests**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_status_store.py' -v
python3 -m unittest discover -s tests -p 'test_status_hook.py' -v
```

Expected: 8 tests pass in total and the hook test leaves the temporary directory automatically removed.

- [ ] **Step 5: Commit hook cleanup**

```bash
git add bin/ai-agent-status-hook tests/test_status_hook.py
git commit -m "perf(hook): prune expired session statuses"
```

---

### Task 3: Make widget status reads bounded and single-pass

**Files:**
- Modify: `bin/ai-agent-status-widget:17-21,645-686,723-738`
- Create: `tests/test_widget_status_retention.py`

**Interfaces:**
- Consumes: `collect_status_records(...) -> StatusCollection` from Task 1 with `remove_expired=False`.
- Consumes: `read_status_paths(...) -> StatusCollection` from Task 1.
- Preserves: `StatusWidget.read_structured_sessions() -> list[dict[str, object]] | None`.

- [ ] **Step 1: Write a failing GTK-isolated fallback test**

Create `tests/test_widget_status_retention.py` using the existing `xvfb-run` test pattern:

```python
from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WidgetStatusRetentionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("xvfb-run"), "xvfb-run is required")
    def test_expired_structured_status_uses_compatibility_fallback_without_cleanup(self) -> None:
        probe = textwrap.dedent(
            """
            import json
            import os
            import runpy
            import sys
            import tempfile
            import time
            from datetime import datetime
            from pathlib import Path

            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cache = root / "cache"
                statuses = cache / "statuses"
                statuses.mkdir(parents=True)
                expired = statuses / "codex-expired.json"
                expired.write_text(json.dumps({
                    "agent": "codex",
                    "kind": "reading",
                    "status": "Codex: old",
                    "timestamp_iso": "2026-07-20T10:00:00",
                }), encoding="utf-8")
                old = time.time() - 901
                os.utime(expired, (old, old))
                (cache / "claude.json").write_text(json.dumps({
                    "agent": "claude",
                    "kind": "reading",
                    "status": "Claude: reading",
                    "timestamp_iso": datetime.now().isoformat(timespec="seconds"),
                    "timestamp_hhmm": datetime.now().strftime("%H:%M"),
                }), encoding="utf-8")
                os.environ.update({
                    "AI_STATUS_CACHE_DIR": str(cache),
                    "AI_STATUS_CONFIG_DIR": str(root / "config"),
                    "AI_STATUS_DATA_DIR": str(root / "data"),
                    "AI_STATUS_HIDE_STALE_AFTER_SECONDS": "900",
                })
                sys.path.insert(0, "bin")
                module = runpy.run_path(
                    "bin/ai-agent-status-widget",
                    run_name="widget_status_retention_smoke",
                )
                widget = module["StatusWidget"].__new__(module["StatusWidget"])
                widget.demo = False
                widget.config = dict(module["DEFAULT_CONFIG"])

                sessions = widget.read_structured_sessions()

                assert sessions is not None
                assert [session["agent"] for session in sessions] == ["claude"]
                assert expired.exists()
            """
        )
        completed = subprocess.run(
            ["xvfb-run", "-a", sys.executable, "-c", probe],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the widget test and verify RED**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_widget_status_retention.py' -v
```

Expected: `FAIL`; the existing widget sees the expired structured file and does not use `claude.json` as fallback.

- [ ] **Step 3: Replace the widget's two-pass reader**

Add imports in `bin/ai-agent-status-widget`:

```python
from ai_agent_status_lib.status_store import collect_status_records
from ai_agent_status_lib.status_store import read_status_paths
```

At the start of `read_structured_sessions()`, replace `status_file_paths()` with:

```python
        collection = collect_status_records(
            STATUS_DIR,
            retention_seconds=self.config_seconds("hide_stale_after_seconds"),
            diagnostic=log,
        )
        if not collection.has_retained_files:
            collection = read_status_paths(AGENT_STATUS_FILES, diagnostic=log)
        if not collection.has_retained_files:
            return None
```

Change the parsing loop to consume already-decoded records:

```python
        sessions: list[dict[str, object]] = []
        for path, data in collection.records:
            status = str(data.get("status") or "")
            project = str(data.get("project") or "")
            hhmm = str(data.get("timestamp_hhmm") or "")
            agent = str(data.get("agent") or path.stem).lower()
```

Keep the remaining normalization, age transformation, visibility, and session-building logic unchanged. Delete `status_file_paths()` and `status_file_timestamp()` because the shared reader replaces both.

- [ ] **Step 4: Run focused and full regression tests**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_widget_status_retention.py' -v
python3 -m unittest discover -s tests -v
```

Expected: the focused test passes; all existing tests, including the provider animation test, pass unchanged.

- [ ] **Step 5: Run syntax, isolated hook, and GTK smoke verification**

Run:

```bash
python3 -m py_compile bin/ai-agent-status-hook bin/ai-agent-status-widget \
  bin/ai-agent-status-doctor bin/ai_agent_status_lib/*.py
hook_test_cache="$(mktemp -d)"
AI_STATUS_CACHE_DIR="$hook_test_cache" bin/ai-agent-status-hook --agent codex --test
widget_smoke_cache="$(mktemp -d)"
AI_STATUS_CACHE_DIR="$widget_smoke_cache" timeout 5s xvfb-run -a bin/ai-agent-status-widget --demo
git diff --check
```

Expected: compilation succeeds; the hook prints simulated Codex output; the demo exits only because of `timeout` with no GTK traceback; `git diff --check` prints nothing. Do not point `AI_STATUS_CACHE_DIR` at the live cache.

- [ ] **Step 6: Commit widget integration**

```bash
git add bin/ai-agent-status-widget tests/test_widget_status_retention.py
git commit -m "perf(widget): bound structured status reads"
```

- [ ] **Step 7: Measure the isolated retained-status scan**

Create 142 temporary records with 141 older than 900 seconds, call `collect_status_records(..., remove_expired=False)` repeatedly, and report median and p95 without committing benchmark artifacts. Expected functional result: one returned record, 141 expired files left untouched in read-only mode, and no live-cache access.

---

## Final verification

- [ ] Run `python3 -m unittest discover -s tests -v`; expect all tests to pass.
- [ ] Run the repository `py_compile` command; expect no output.
- [ ] Run `timeout 5s xvfb-run -a bin/ai-agent-status-widget --demo`; accept timeout exit `124`, but no GTK/Python error output.
- [ ] Run `git diff --check`; expect no output.
- [ ] Inspect `git status --short --branch`; expect only the planned commits and no untracked benchmark/cache files.
- [ ] Confirm with `git diff 9f1dcb9..HEAD -- bin/ai-agent-status-widget` that no animation constants, animation timer registration, or animation methods changed.

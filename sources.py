"""Read-only agent providers. Public snapshots contain aggregate data only."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from typing import Protocol


class SourceError(RuntimeError):
    pass


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).astimezone().isoformat(timespec="seconds")


def readonly(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection
    except sqlite3.Error as exc:
        raise SourceError("database_unavailable") from exc


def lock_held(path: Path) -> bool:
    """Probe the OS lock, not file existence. Never write to the Codex lock file."""
    try:
        with path.open("r+b", buffering=0) as handle:
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        return True
                    raise
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(handle, fcntl.LOCK_UN)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SourceError("lock_probe_failed") from exc
    return False


def quota_snapshot(raw: dict, observed: float, now: float) -> dict:
    windows = []
    for name in ("primary", "secondary"):
        window = raw.get(name)
        if not isinstance(window, dict):
            continue
        used = window.get("used_percent", window.get("usedPercent"))
        duration = window.get("window_minutes", window.get("windowDurationMins"))
        resets = window.get("resets_at", window.get("resetsAt"))
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) for value in (used, duration, resets)):
            continue
        if not (0 <= used <= 100 and duration > 0 and 0 <= resets <= 253402300799):
            continue
        windows.append({"name": name, "used_percent": used,
                        "remaining_percent": round(100 - used, 2),
                        "window_minutes": duration, "resets_at": iso(resets)})
    if not windows:
        return {"status": "unavailable", "windows": []}
    expired = any(datetime.fromisoformat(w["resets_at"]).timestamp() <= now for w in windows)
    return {"status": "stale" if now - observed > 900 or expired else "available",
            "source": "local_token_count", "observed_at": iso(observed), "windows": windows}


@dataclass
class Counts:
    running: int = 0
    waiting: int = 0
    idle: int = 0
    stale: int = 0

    def public(self) -> dict:
        return {"running": self.running, "waiting": self.waiting, "idle": self.idle,
                "stale": self.stale, "active": self.running + self.waiting + self.stale,
                "total": self.running + self.waiting + self.idle + self.stale}


@dataclass
class Snapshot:
    counts: Counts = field(default_factory=Counts)
    subagents: Counts = field(default_factory=Counts)
    warnings: set[str] = field(default_factory=set)
    quota: dict = field(default_factory=lambda: {"status": "unavailable", "windows": []})

    def public(self, now: float) -> dict:
        return {**self.counts.public(), "subagents": self.subagents.public(),
                "source": {"kind": "local_files", "scope": "loaded_threads",
                           "health": "degraded" if self.warnings else "ok",
                           "observed_at": iso(now), "warnings": sorted(self.warnings),
                           "waiting_detection": "persisted_requests_only"},
                "quota": self.quota}


class Provider(Protocol):
    name: str

    def collect(self, now: float) -> Snapshot: ...


@dataclass
class Transcript:
    path: Path
    offset: int
    pending: set[tuple[str, int]] = field(default_factory=set)
    asynchronous: set[str] = field(default_factory=set)
    quota: dict | None = None
    quota_at: float = 0
    caught_up: bool = False
    discarding: bool = False

    def event(self, record: dict) -> None:
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if record.get("type") == "event_msg":
            if kind in ("task_complete", "turn_aborted", "turn_failed"):
                self.pending = {item for item in self.pending if item[0] in self.asynchronous}
            raw = payload.get("rate_limits")
            if isinstance(raw, dict):
                try:
                    observed = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).timestamp()
                    if observed >= self.quota_at:
                        self.quota, self.quota_at = raw, observed
                except (KeyError, ValueError, TypeError):
                    pass
            if kind in ("request_user_input", "approval_request", "exec_approval_request",
                        "apply_patch_approval_request"):
                request = payload.get("call_id", payload.get("request_id"))
                if isinstance(request, str):
                    self.pending.add((request, 0))
            if kind in ("approval_response", "exec_approval_response", "request_resolved"):
                self.resolve(payload.get("call_id", payload.get("request_id")))
        if record.get("type") != "response_item":
            return
        call_id = payload.get("call_id")
        if kind in ("function_call", "custom_tool_call"):
            name = str(payload.get("name", "")).rsplit(".", 1)[-1]
            if name not in ("request_user_input", "request_user_input_async") or not isinstance(call_id, str):
                return
            try:
                args = payload.get("arguments", payload.get("input", "{}"))
                args = json.loads(args) if isinstance(args, str) else args
                questions = args.get("questions", [])
                count = len(questions) if isinstance(questions, list) else 1
            except (ValueError, AttributeError):
                count = 1
            self.pending.update((call_id, index) for index in range(max(1, count)))
            if name.endswith("_async"):
                self.asynchronous.add(call_id)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            if call_id not in self.asynchronous:
                self.resolve(call_id)
            else:
                try:
                    output = payload.get("output", "{}")
                    output = json.loads(output) if isinstance(output, str) else output
                    if isinstance(output, dict) and output.get("accepted") is False:
                        self.resolve(call_id)
                except ValueError:
                    pass
        elif kind == "message" and payload.get("role") == "user":
            for content in payload.get("content", []):
                if not isinstance(content, dict):
                    continue
                text = content.get("text", "")
                if not isinstance(text, str):
                    continue
                match = re.fullmatch(r"\s*<send_user_message_question_reply>\s*(.*?)\s*</send_user_message_question_reply>\s*", text, re.S)
                if not match:
                    continue
                try:
                    replies = json.loads(match[1])
                    for reply in replies:
                        identity = json.loads(reply["questionItemId"])
                        if (isinstance(identity, list) and len(identity) == 3
                                and identity[0] in ("request_user_input_async", "request_user_input")
                                and isinstance(identity[1], str) and isinstance(identity[2], int)):
                            self.pending.discard((identity[1], identity[2]))
                            if not any(item[0] == identity[1] for item in self.pending):
                                self.asynchronous.discard(identity[1])
                except (ValueError, TypeError, KeyError):
                    pass

    def resolve(self, call_id: object) -> None:
        self.pending = {item for item in self.pending if item[0] != call_id}
        self.asynchronous.discard(call_id)

    def read(self, budget: int = 4 * 1024 * 1024) -> None:
        with self.path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size < self.offset:
                self.offset = max(0, size - 8 * 1024 * 1024)
                self.pending.clear()
                self.asynchronous.clear()
                self.quota, self.quota_at = None, 0
                self.discarding = self.offset > 0
            handle.seek(self.offset)
            read_bytes = 0
            while read_bytes < budget:
                if self.discarding:
                    chunk = handle.readline(min(64 * 1024, budget - read_bytes))
                    if not chunk:
                        break
                    self.offset = handle.tell()
                    read_bytes += len(chunk)
                    self.discarding = not chunk.endswith(b"\n")
                    continue
                # Large tool results can exceed the decoder's memory budget.
                # Skip them incrementally; waiting detection remains a lower bound.
                line = handle.readline(8 * 1024 * 1024 + 1)
                if len(line) > 8 * 1024 * 1024:
                    self.offset = handle.tell()
                    read_bytes += len(line)
                    self.discarding = not line.endswith(b"\n")
                    continue
                if not line or not line.endswith(b"\n"):
                    break
                read_bytes += len(line)
                self.offset = handle.tell()
                try:
                    record = json.loads(line)
                    if isinstance(record, dict):
                        self.event(record)
                except (ValueError, TypeError):
                    continue
            self.caught_up = self.offset >= size and not self.discarding


class CodexProvider:
    name = "codex"

    def __init__(self, home: Path, stale_after: float = 300):
        self.home = home
        self.stale_after = stale_after
        self.readers: dict[str, Transcript] = {}

    def collect(self, now: float) -> Snapshot:
        locks = self.home / "thread-writer-locks"
        if not locks.is_dir():
            raise SourceError("live_session_locks_unavailable")
        result = Snapshot()
        live = []
        for lock in locks.glob("*.lock"):
            if not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", lock.stem):
                continue
            try:
                if lock_held(lock):
                    live.append(lock.stem)
            except SourceError:
                result.warnings.add("lock_probe_failed")
        state = readonly(self.home / "state_5.sqlite")
        history = None
        try:
            history = readonly(self.home / "thread_history_1.sqlite")
            for thread_id in live:
                row = state.execute("SELECT rollout_path, source, archived FROM threads WHERE id=?", (thread_id,)).fetchone()
                if row is None or row["archived"]:
                    continue
                group = result.subagents if self.is_subagent(row["source"]) else result.counts
                turns = history.execute(
                    "SELECT status, rollout_byte_offset FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 2",
                    (thread_id,)).fetchall()
                if not turns:
                    group.stale += 1
                    result.warnings.add("turn_state_unavailable")
                    continue
                path = Path(row["rollout_path"])
                reader = self.readers.get(thread_id)
                try:
                    if reader is None or reader.path != path:
                        # shortcut: restart scans at most 8 MiB of two latest turns; older pending questions need a runtime event provider.
                        offsets = [turn["rollout_byte_offset"] for turn in turns if turn["rollout_byte_offset"] is not None]
                        size = path.stat().st_size
                        valid_offsets = [offset for offset in offsets if 0 <= offset <= size]
                        turn_start = min(valid_offsets) if valid_offsets else 0
                        start = max(turn_start, size - 8 * 1024 * 1024)
                        reader = Transcript(path, start, discarding=start > turn_start)
                        self.readers[thread_id] = reader
                    reader.read()
                    activity = history.execute(
                        "SELECT created_at_ms, started_at_ms, completed_at_ms FROM thread_items WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1",
                        (thread_id,)).fetchone()
                    # Windows can defer LastWriteTime updates while the writer keeps a handle open.
                    activity_at = max((value / 1000 for value in activity if value is not None), default=0) if activity else 0
                    fresh = now - max(activity_at, path.stat().st_mtime) <= self.stale_after
                except (OSError, SourceError):
                    group.stale += 1
                    result.warnings.add("transcript_unavailable")
                    continue
                if not reader.caught_up:
                    group.stale += 1
                    result.warnings.add("transcript_catching_up")
                elif reader.pending:
                    group.waiting += 1
                elif turns[0]["status"] in ("completed", "interrupted", "failed"):
                    group.idle += 1
                elif turns[0]["status"] == "inProgress" and fresh:
                    group.running += 1
                else:
                    group.stale += 1
                    result.warnings.add("unfinished_turn_without_recent_activity")
        except sqlite3.Error as exc:
            raise SourceError("unsupported_or_unavailable_codex_schema") from exc
        finally:
            state.close()
            if history is not None:
                history.close()
        self.readers = {key: value for key, value in self.readers.items() if key in live}
        samples = [reader for reader in self.readers.values() if reader.quota is not None]
        if samples:
            latest = max(samples, key=lambda reader: reader.quota_at)
            result.quota = quota_snapshot(latest.quota, latest.quota_at, now)
        return result

    @staticmethod
    def is_subagent(source: str) -> bool:
        try:
            value = json.loads(source)
            return isinstance(value, dict) and "subagent" in value
        except (ValueError, TypeError):
            return source.lower() in ("subagent", "subagentthreadspawn", "subagentreview", "subagentcompact")

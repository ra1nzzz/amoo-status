import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sources import CodexProvider, Counts, Snapshot, SourceError, Transcript, lock_held, quota_snapshot
from statusd import InstanceLock, Publisher, PublishError, atomic_json, collect, fingerprint, serve, LOG


@contextmanager
def database(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def record(kind, payload):
    return {"type": kind, "timestamp": "2026-10-18T14:00:00Z", "payload": payload}


def question(reader, call="call_a", asynchronous=True, count=1):
    reader.event(record("response_item", {"type": "function_call", "call_id": call,
        "name": "request_user_input_async" if asynchronous else "request_user_input",
        "arguments": json.dumps({"questions": [{}] * count})}))


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.reader = Transcript(Path("unused"), 0)

    def test_async_ack_is_not_an_answer_and_each_answer_resolves_one_question(self):
        question(self.reader, count=2)
        self.reader.event(record("response_item", {"type": "function_call_output", "call_id": "call_a", "output": '{"accepted":true}'}))
        self.assertEqual(len(self.reader.pending), 2)
        answer = [{"questionItemId": json.dumps(["request_user_input_async", "call_a", 0]), "answer": "yes"}]
        self.reader.event(record("response_item", {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<send_user_message_question_reply>" + json.dumps(answer) + "</send_user_message_question_reply>"}]}))
        self.assertEqual(self.reader.pending, {("call_a", 1)})

    def test_sync_output_and_rejected_async_request_clear_waiting(self):
        question(self.reader, asynchronous=False)
        self.reader.event(record("response_item", {"type": "function_call_output", "call_id": "call_a", "output": '{}'}))
        self.assertFalse(self.reader.pending)
        question(self.reader)
        self.reader.event(record("response_item", {"type": "function_call_output", "call_id": "call_a", "output": '{"accepted":false}'}))
        self.assertFalse(self.reader.pending)

    def test_natural_language_and_unrelated_answers_do_not_change_waiting(self):
        self.reader.event(record("response_item", {"type": "message", "role": "assistant", "content": [{"text": "Please approve?"}]}))
        self.assertFalse(self.reader.pending)
        question(self.reader)
        self.reader.event(record("response_item", {"type": "function_call_output", "call_id": "other", "output": '{}'}))
        self.assertTrue(self.reader.pending)

    def test_interruption_clears_sync_requests_but_completed_turn_keeps_async_question(self):
        question(self.reader, call="sync", asynchronous=False)
        question(self.reader, call="async")
        self.reader.event(record("event_msg", {"type": "turn_aborted"}))
        self.assertEqual(self.reader.pending, {("async", 0)})
        self.reader.event(record("event_msg", {"type": "task_complete"}))
        self.assertEqual(self.reader.pending, {("async", 0)})

    def test_partial_line_is_retried_and_invalid_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            data = json.dumps(record("event_msg", {"type": "approval_request", "request_id": "approval1"})).encode()
            path.write_bytes(b"bad json\n" + data[:-3])
            reader = Transcript(path, 0)
            reader.read()
            self.assertFalse(reader.caught_up)
            with path.open("ab") as handle:
                handle.write(data[-3:] + b"\n")
            reader.read()
            self.assertTrue(reader.caught_up)
            self.assertEqual(reader.pending, {("approval1", 0)})

    def test_large_unrelated_record_does_not_block_following_question(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            with path.open("wb") as handle:
                handle.write(b'{"large":"' + b"x" * (8 * 1024 * 1024 + 30) + b'"}\n')
                handle.write((json.dumps(record("event_msg", {"type": "approval_request", "request_id": "a"})) + "\n").encode())
            reader = Transcript(path, 0)
            for _ in range(5):
                reader.read()
            self.assertTrue(reader.caught_up)
            self.assertEqual(reader.pending, {("a", 0)})


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        (self.home / "thread-writer-locks").mkdir()
        self.identifier = "00000000-0000-0000-0000-000000000001"
        self.path = self.home / "session.jsonl"
        self.path.write_text(json.dumps(record("event_msg", {"type": "task_started"})) + "\n", encoding="utf-8")
        with database(self.home / "state_5.sqlite") as connection:
            connection.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,source TEXT,archived INTEGER)")
            connection.execute("INSERT INTO threads VALUES(?,?,?,0)", (self.identifier, str(self.path), "vscode"))
        with database(self.home / "thread_history_1.sqlite") as connection:
            connection.execute("CREATE TABLE thread_turns(thread_id TEXT,rollout_ordinal INTEGER,status TEXT,rollout_byte_offset INTEGER)")
            connection.execute("CREATE INDEX latest ON thread_turns(thread_id,rollout_ordinal)")
            connection.execute("INSERT INTO thread_turns VALUES(?,1,'inProgress',0)", (self.identifier,))
            connection.execute("CREATE TABLE thread_items(thread_id TEXT,rollout_ordinal INTEGER,created_at_ms INTEGER,started_at_ms INTEGER,completed_at_ms INTEGER)")

    def tearDown(self):
        self.temporary.cleanup()

    def test_unheld_lock_is_not_a_live_session(self):
        (self.home / "thread-writer-locks" / (self.identifier + ".lock")).touch()
        result = CodexProvider(self.home).collect(time.time())
        self.assertEqual(result.counts.public()["total"], 0)

    def test_held_root_and_subagent_are_separate(self):
        with InstanceLock(self.home / "thread-writer-locks" / (self.identifier + ".lock")):
            self.assertTrue(lock_held(self.home / "thread-writer-locks" / (self.identifier + ".lock")))
            self.assertEqual(CodexProvider(self.home).collect(time.time()).counts.running, 1)
            with database(self.home / "state_5.sqlite") as connection:
                connection.execute("UPDATE threads SET source=?", (json.dumps({"subagent": {"thread_spawn": {"parent_thread_id": "root"}}}),))
            result = CodexProvider(self.home).collect(time.time())
            self.assertEqual(result.counts.public()["total"], 0)
            self.assertEqual(result.subagents.running, 1)

    def test_unfinished_old_turn_is_stale_and_completed_turn_is_idle(self):
        old = time.time() - 1000
        os.utime(self.path, (old, old))
        with InstanceLock(self.home / "thread-writer-locks" / (self.identifier + ".lock")):
            self.assertEqual(CodexProvider(self.home).collect(time.time()).counts.stale, 1)
            with database(self.home / "thread_history_1.sqlite") as connection:
                connection.execute("UPDATE thread_turns SET status='completed'")
            self.assertEqual(CodexProvider(self.home).collect(time.time()).counts.idle, 1)

    def test_completed_turn_with_unanswered_question_is_waiting(self):
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record("event_msg", {"type": "request_user_input", "request_id": "q"})) + "\n")
        with database(self.home / "thread_history_1.sqlite") as connection:
            connection.execute("UPDATE thread_turns SET status='completed'")
        with InstanceLock(self.home / "thread-writer-locks" / (self.identifier + ".lock")):
            self.assertEqual(CodexProvider(self.home).collect(time.time()).counts.waiting, 1)

    def test_database_activity_overrides_delayed_windows_file_timestamp(self):
        now = time.time()
        os.utime(self.path, (now - 1000, now - 1000))
        with database(self.home / "thread_history_1.sqlite") as connection:
            connection.execute("INSERT INTO thread_items VALUES(?,1,?,NULL,NULL)", (self.identifier, now * 1000))
        with InstanceLock(self.home / "thread-writer-locks" / (self.identifier + ".lock")):
            self.assertEqual(CodexProvider(self.home).collect(now).counts.running, 1)

    def test_obsolete_database_offset_after_log_compaction_is_clamped(self):
        with database(self.home / "thread_history_1.sqlite") as connection:
            connection.execute("UPDATE thread_turns SET rollout_byte_offset=4635362545")
        provider = CodexProvider(self.home)
        with InstanceLock(self.home / "thread-writer-locks" / (self.identifier + ".lock")):
            self.assertEqual(provider.collect(time.time()).counts.running, 1)
        self.assertLessEqual(provider.readers[self.identifier].offset, self.path.stat().st_size)


class OutputTests(unittest.TestCase):
    def test_quota_is_sanitized_and_old_samples_expire(self):
        now = time.time()
        raw = {"primary": {"used_percent": 32, "window_minutes": 10080, "resets_at": now + 10000},
               "credits": {"balance": "PRIVATE"}, "account_id": "SECRET"}
        result = quota_snapshot(raw, now, now)
        self.assertEqual(result["windows"][0]["remaining_percent"], 68)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(quota_snapshot(raw, now - 1000, now)["status"], "stale")
        raw["primary"]["used_percent"] = float("nan")
        self.assertEqual(quota_snapshot(raw, now, now)["status"], "unavailable")

    def test_failed_source_retains_last_counts_with_unavailable_health(self):
        class Good:
            name = "codex"
            def collect(self, now): return Snapshot(counts=Counts(running=2))
        class Bad:
            name = "codex"
            def collect(self, now): raise SourceError("database_unavailable")
        previous = collect([Good()], time.time())
        result = collect([Bad()], time.time(), previous)
        self.assertEqual(result["active"], 2)
        self.assertEqual(result["health"], "degraded")
        self.assertTrue(result["providers"]["codex"]["source"]["retained"])

    def test_atomic_json_and_fingerprint_ignore_only_sample_times(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            atomic_json(path, {"detail": "等待人工", "updated": "a"})
            self.assertEqual(json.loads(path.read_text("utf-8"))["detail"], "等待人工")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
        self.assertEqual(fingerprint({"updated": "a", "source": {"observed_at": "b"}, "running": 1}),
                         fingerprint({"updated": "c", "source": {"observed_at": "d"}, "running": 1}))
        self.assertNotEqual(fingerprint({"running": 1}), fingerprint({"running": 2}))

    def test_singleton_rejects_second_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.lock"
            with InstanceLock(path):
                with self.assertRaises(RuntimeError):
                    with InstanceLock(path): pass


class PublisherTests(unittest.TestCase):
    def publisher(self):
        with patch("statusd.shutil.which", return_value="gh"):
            return Publisher("owner/repo", "main", "instance")

    def test_conflict_refreshes_sha_and_changes_only_status_json(self):
        publisher = self.publisher()
        responses = [subprocess.CompletedProcess([], 0, '{"sha":"old"}', ''),
                     subprocess.CompletedProcess([], 1, '', 'HTTP 409'),
                     subprocess.CompletedProcess([], 0, '{"sha":"new"}', ''),
                     subprocess.CompletedProcess([], 0, '{}', '')]
        with patch.object(publisher, "api", side_effect=responses) as api:
            publisher.publish({"running": 1})
        self.assertEqual(api.call_args_list[3].args[2]["sha"], "new")
        self.assertEqual(json.loads(base64.b64decode(api.call_args_list[3].args[2]["content"])), {"running": 1})
        self.assertTrue(all(call.args[1].startswith("repos/owner/repo/contents/status.json") for call in api.call_args_list))

    def test_other_writer_is_reported_after_first_publication(self):
        publisher = self.publisher()
        publisher.owned = True
        remote = {"sha": "new", "content": base64.b64encode(b'{"service":{"instance":"another"}}').decode()}
        with patch.object(publisher, "api", return_value=subprocess.CompletedProcess([], 0, json.dumps(remote), '')):
            with self.assertRaisesRegex(PublishError, "another_writer_detected"):
                publisher.publish({})

    def test_invalid_remote_shape_fails_visibly(self):
        publisher = self.publisher()
        with patch.object(publisher, "api", return_value=subprocess.CompletedProcess([], 0, '[]', '')):
            with self.assertRaisesRegex(PublishError, "github_invalid_response"):
                publisher.publish({})


class ServiceTests(unittest.TestCase):
    def test_slow_upload_does_not_block_next_local_poll(self):
        started, release = threading.Event(), threading.Event()
        class SlowPublisher:
            def publish(self, snapshot):
                started.set()
                release.wait(2)
        class LiveProvider:
            name = "codex"
            calls = 0
            def collect(self, now):
                self.calls += 1
                if self.calls == 2:
                    if not started.wait(1) or release.is_set():
                        raise AssertionError("publication was not still in progress")
                    raise RuntimeError("test_poll_reached_while_upload_blocked")
                return Snapshot(counts=Counts(running=1))
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            args = SimpleNamespace(runtime_dir=runtime, codex_home=runtime, stale_after=300,
                publish=True, repo="owner/repo", branch="main", output=runtime / "status.json",
                publish_interval=60, heartbeat=300, command="run", poll=0.01)
            old_handlers = set(LOG.handlers)
            try:
                with patch("statusd.CodexProvider", return_value=LiveProvider()), \
                     patch("statusd.Publisher", return_value=SlowPublisher()), \
                     patch("statusd.signal.signal"):
                    with self.assertRaisesRegex(RuntimeError, "test_poll_reached"):
                        serve(args)
                self.assertEqual(json.loads(args.output.read_text("utf-8"))["running"], 1)
            finally:
                release.set()
                for handler in set(LOG.handlers) - old_handlers:
                    LOG.removeHandler(handler)
                    handler.close()


if __name__ == "__main__":
    unittest.main()

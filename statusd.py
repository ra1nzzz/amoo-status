"""Lightweight local agent status service. Python 3.10+, no third-party modules."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote
import uuid

from sources import CodexProvider, Provider, SourceError, iso, quota_snapshot

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("statusd")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Windows readers can briefly hold the target without FILE_SHARE_DELETE.
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def fingerprint(value: object) -> str:
    def stable(item):
        if isinstance(item, dict):
            return {key: stable(val) for key, val in item.items()
                    if key not in ("updated", "observed_at")}
        if isinstance(item, list):
            return [stable(val) for val in item]
        return item
    return hashlib.sha256(json.dumps(stable(value), sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def collect(providers: list[Provider], now: float, previous: dict | None = None) -> dict:
    snapshots = {}
    for provider in providers:
        try:
            snapshots[provider.name] = provider.collect(now).public(now)
        except (SourceError, OSError, ValueError) as exc:
            LOG.warning("provider %s unavailable: %s", provider.name, type(exc).__name__)
            cached = (previous or {}).get("providers", {}).get(provider.name, {})
            snapshot = json.loads(json.dumps(cached))
            for key in ("running", "waiting", "idle", "stale", "active", "total"):
                snapshot.setdefault(key, 0)
            source = snapshot.setdefault("source", {})
            source.update(health="unavailable", warnings=[str(exc) if isinstance(exc, SourceError)
                                                         else "source_read_failed"], retained=bool(cached))
            snapshot.setdefault("quota", {"status": "unavailable", "windows": []})
            if snapshot["quota"].get("status") == "available":
                snapshot["quota"]["status"] = "stale"
            snapshots[provider.name] = snapshot
    counts = {key: sum(snapshot[key] for snapshot in snapshots.values())
              for key in ("running", "waiting", "idle", "stale", "active", "total")}
    degraded = any(snapshot["source"]["health"] != "ok" for snapshot in snapshots.values())
    status = "waiting" if counts["waiting"] else "developing" if counts["active"] else "idle"
    detail = f"运行 {counts['running']} / 等待 {counts['waiting']} / 空闲 {counts['idle']}"
    if counts["stale"]:
        detail += f" / 状态过期 {counts['stale']}"
    if degraded:
        detail += "（采集降级，数值包含最近记录）"
    return {"schema_version": 2, **counts, "status": status, "detail": detail,
            "updated": iso(now), "health": "degraded" if degraded else "ok",
            "providers": snapshots}


class PublishError(RuntimeError):
    pass


def run_command(command: list[str], payload: str | None = None, timeout: float = 20) -> subprocess.CompletedProcess:
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    return subprocess.run(command, input=payload, text=True, encoding="utf-8",
                          capture_output=True, timeout=timeout, **options)


class Publisher:
    def __init__(self, repo: str, branch: str, instance: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("repo must be owner/name")
        self.gh = shutil.which("gh")
        if not self.gh:
            raise PublishError("gh_not_installed")
        self.endpoint = f"repos/{repo}/contents/status.json"
        self.branch, self.instance = branch, instance
        self.owned = False

    def api(self, method: str, endpoint: str, payload: dict | None = None):
        command = [self.gh, "api", "--hostname", "github.com", "--method", method, endpoint]
        if payload is not None:
            command += ["--input", "-"]
        return run_command(command, json.dumps(payload) if payload is not None else None)

    def publish(self, snapshot: dict) -> None:
        content = (json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        for attempt in range(2):
            response = self.api("GET", self.endpoint + "?ref=" + quote(self.branch, safe=""))
            sha = None
            if response.returncode == 0:
                remote = json.loads(response.stdout)
                if not isinstance(remote, dict) or not isinstance(remote.get("sha"), str):
                    raise PublishError("github_invalid_response")
                sha = remote["sha"]
                if self.owned:
                    try:
                        old = json.loads(base64.b64decode(remote["content"]))
                        if old.get("service", {}).get("instance") != self.instance:
                            raise PublishError("another_writer_detected")
                    except (ValueError, KeyError):
                        raise PublishError("remote_status_invalid")
            elif "HTTP 404" not in response.stderr:
                raise PublishError("github_read_failed")
            body = {"message": "chore: update agent status", "branch": self.branch,
                    "content": base64.b64encode(content).decode("ascii")}
            if sha is not None:
                body["sha"] = sha
            response = self.api("PUT", self.endpoint, body)
            if response.returncode == 0:
                self.owned = True
                return
            if "HTTP 409" not in response.stderr or attempt:
                raise PublishError("github_write_failed")
        raise PublishError("github_conflict")


class InstanceLock:
    def __init__(self, path: Path):
        self.path, self.handle = path, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b", buffering=0)
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError("another statusd instance holds this runtime directory") from None
        return self

    def __exit__(self, *_):
        self.handle.close()


def instance_id(runtime: Path) -> str:
    path = runtime / "instance.json"
    if path.exists():
        return str(uuid.UUID(json.loads(path.read_text("utf-8"))["id"]))
    identity = str(uuid.uuid4())
    atomic_json(path, {"id": identity})
    return identity


def official_quota_probe() -> dict:
    """Only initialize and read limits. Never start/resume a thread or invoke a model."""
    executable = shutil.which("codex")
    if not executable:
        return {"status": "unavailable", "reason": "codex_not_installed"}
    # Prefer the actual binary so termination also closes app-server on Windows.
    if os.name == "nt":
        candidates = list((Path(executable).parent / "node_modules" / "@openai" / "codex").glob("**/codex.exe"))
        if candidates:
            executable = str(candidates[0])
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    process = subprocess.Popen([executable, "app-server", "--listen", "stdio://"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                               encoding="utf-8", **options)
    messages = queue.Queue()

    def read():
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                pass
        messages.put(None)

    threading.Thread(target=read, daemon=True).start()

    def rpc(identifier: int, method: str, params: dict | None = None):
        request = {"id": identifier, "method": method}
        if params is not None:
            request["params"] = params
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            response = messages.get(timeout=max(0.01, deadline - time.monotonic()))
            if response is None:
                raise RuntimeError("app_server_closed")
            if response.get("id") == identifier:
                if "error" in response:
                    raise RuntimeError("account_method_rejected")
                return response.get("result", {})
        raise TimeoutError("app_server_timeout")

    try:
        rpc(1, "initialize", {"clientInfo": {"name": "amoo_status", "title": "Amoo Status", "version": "1.0.0"}})
        process.stdin.write('{"method":"initialized"}\n')
        process.stdin.flush()
        result = rpc(2, "account/rateLimits/read")
        raw = result.get("rateLimits")
        if not isinstance(raw, dict):
            return {"status": "unavailable", "reason": "no_chatgpt_limits_returned"}
        now = time.time()
        output = quota_snapshot(raw, now, now)
        output["source"] = "official_app_server"
        return output
    except (OSError, RuntimeError, queue.Empty, TimeoutError) as exc:
        return {"status": "unavailable", "reason": type(exc).__name__}
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        process.stdout.close()


def serve(args) -> int:
    runtime = args.runtime_dir.resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(runtime / "statusd.log", maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    with InstanceLock(runtime / "service.lock"):
        identity = instance_id(runtime)
        providers = [CodexProvider(args.codex_home.resolve(), args.stale_after)]
        publisher = Publisher(args.repo, args.branch, identity) if args.publish else None
        previous = None
        last_good = runtime / "last-good.json"
        if last_good.exists():
            try:
                previous = json.loads(last_good.read_text("utf-8"))
            except (OSError, ValueError):
                LOG.warning("cached snapshot could not be read")
        stop = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        published = None
        last_attempt = last_publish = 0.0
        last_published_at = None
        last_error = None
        results = queue.Queue()
        in_flight = False

        def publish(snapshot, signature):
            try:
                publisher.publish(snapshot)
                results.put((signature, snapshot["active"], snapshot["waiting"], snapshot["idle"], None))
            except (PublishError, OSError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
                error = str(exc) if isinstance(exc, PublishError) else type(exc).__name__
                results.put((signature, 0, 0, 0, error))

        def finish_publication():
            nonlocal published, last_publish, last_published_at, last_error, in_flight
            if results.empty():
                return
            signature, active, waiting, idle, error = results.get_nowait()
            in_flight, last_error = False, error
            if error:
                LOG.warning("publication failed: %s", error)
            else:
                published, last_publish, last_published_at = signature, time.monotonic(), iso(time.time())
                LOG.info("published active=%s waiting=%s idle=%s", active, waiting, idle)

        started = time.time()
        LOG.info("service started pid=%s publish=%s", os.getpid(), bool(publisher))
        while not stop.is_set():
            now = time.time()
            snapshot = collect(providers, now, previous)
            snapshot["service"] = {"instance": identity, "version": "1.0.0"}
            atomic_json(args.output.resolve(), snapshot)
            previous = snapshot
            if snapshot["health"] == "ok":
                atomic_json(last_good, snapshot)
            current = fingerprint(snapshot)
            due = time.monotonic()
            finish_publication()
            changed = current != published
            if publisher and not in_flight and due - last_attempt >= args.publish_interval and (changed or due - last_publish >= args.heartbeat):
                last_attempt = due
                in_flight = True
                if args.command == "once":
                    publish(snapshot, current)
                    finish_publication()
                else:
                    # A slow network request must not block five-second local collection.
                    threading.Thread(target=publish, args=(snapshot, current), daemon=True).start()
            atomic_json(runtime / "service.json", {"pid": os.getpid(), "started_at": iso(started),
                        "last_poll": iso(now), "health": snapshot["health"],
                        "publishing": bool(publisher), "publish_in_flight": in_flight,
                        "last_published_at": last_published_at,
                        "publish_error": last_error})
            if args.command == "once":
                if sys.stdout is not None:
                    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
                return 1 if last_error or snapshot["health"] != "ok" else 0
            stop.wait(args.poll)
        LOG.info("service stopped")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "once", "probe-quota"))
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--output", type=Path, default=ROOT / "status.json")
    parser.add_argument("--runtime-dir", type=Path, default=ROOT / ".runtime")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--repo", default="ra1nzzz/amoo-status")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--poll", type=float, default=5)
    parser.add_argument("--publish-interval", type=float, default=60)
    parser.add_argument("--heartbeat", type=float, default=300)
    parser.add_argument("--stale-after", type=float, default=300)
    args = parser.parse_args()
    if any(value < 1 or not math.isfinite(value)
           for value in (args.poll, args.publish_interval, args.heartbeat, args.stale_after)):
        parser.error("intervals must be finite and at least one second")
    if args.command == "probe-quota":
        result = official_quota_probe()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "available" else 1
    return serve(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        LOG.exception("service failed")
        if sys.stderr is not None:
            print("statusd failed; see .runtime/statusd.log", file=sys.stderr)
        raise SystemExit(1)

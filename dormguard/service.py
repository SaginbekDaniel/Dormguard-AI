"""Supervise the existing API and independent workers in one persistent demo container.

One replica only. No provisioning, downloads, default credentials or provider fallback.
"""

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .config import load_settings
from .store import Store


def main():
    settings = load_settings()
    if settings.runtime_mode != "online_demo":
        raise ValueError("Service supervisor requires explicit DORMGUARD_MODE=online_demo")
    if (
        not settings.model_manifest
        or not settings.model_manifest.is_file()
        or not settings.model_rights_confirmed
    ):
        raise ValueError("Provision pinned local model and confirm permitted noncommercial demo scope first")
    port = int(os.environ.get("PORT", "8000"))
    if not 1024 <= port <= 65535:
        raise ValueError("PORT must be unprivileged")
    host = os.environ.get("DORMGUARD_BIND_HOST", "0.0.0.0")
    if host not in {"127.0.0.1", "0.0.0.0"}:
        raise ValueError("DORMGUARD_BIND_HOST must be loopback or the container interface")
    import fcntl

    lock_path = Path(settings.data_dir) / "service.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Only one supervisor may own this data volume") from None
        store = Store(settings.data_dir, settings.database_url)
        try:
            users = store.list_users()
            if not any(u["enabled"] and u["role"] == "director" for u in users):
                raise ValueError("Provision the private demo director with the trusted CLI before launch")
            store.heartbeat("inference", state="starting", detail="supervisor_starting_worker", metrics={})
        finally:
            store.db.close()
        stopping = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stopping.set())
        commands = [
            [
                sys.executable,
                "-m",
                "uvicorn",
                "dormguard.app:create_app",
                "--factory",
                "--host",
                host,
                "--port",
                str(port),
                "--no-proxy-headers",
                "--no-access-log",
            ],
            [sys.executable, "-m", "dormguard.worker"],
        ]
        if settings.telegram_bot_token:
            commands.append([sys.executable, "-m", "dormguard.notifications"])
        children = []
        failed = False
        try:
            for command in commands:
                children.append(subprocess.Popen(command, stdin=subprocess.DEVNULL))
            print(
                json.dumps(
                    {
                        "event": "services_started",
                        "processes": len(children),
                        "mode": "online_demo",
                        "persistent_data_required": True,
                        "readiness": "GET /ready",
                    }
                ),
                flush=True,
            )
            while not stopping.wait(0.25):
                if any(child.poll() is not None for child in children):
                    failed = True
                    print(json.dumps({"event": "service_child_exited", "restart_required": True}), flush=True)
                    break
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
            for child in children:
                try:
                    child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

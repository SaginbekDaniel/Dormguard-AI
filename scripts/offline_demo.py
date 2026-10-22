"""Persistent isolated offline demo supervisor and fail-closed readiness check.

No downloads, .env loading, hosted connections or automatic operational-data reset.
All child processes receive an explicit minimal local-only environment.
"""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_assets(path):
    data = json.loads(path.read_text())
    if data.get("version") != 1 or not data.get("sources"):
        raise ValueError("A versioned source manifest is required")
    sources = []
    for item in data["sources"]:
        media = (path.parent / item["file"]).resolve()
        if not all(item.get(key) for key in ("name", "scenario", "provenance", "rights", "sha256")):
            raise ValueError("Source provenance, rights, digest and scenario are mandatory")
        if not media.is_file() or sha256(media) != item["sha256"]:
            raise ValueError("Missing or changed authorized source: " + item["name"])
        start, end = float(item.get("start_seconds", 0)), float(item["end_seconds"])
        if not 0 <= start < end or end - start > 120:
            raise ValueError("Source segment must contain at most 120 seconds")
        sources.append(dict(item, original=str(media)))
    return sources


def readiness(args):
    started = time.monotonic()
    checks = []
    sources = []

    def check(name, operation):
        try:
            result = operation()
            checks.append({"name": name, "status": "PASS", "result": result})
        except Exception as error:
            # Only known safe local diagnostics; never print environment/config values.
            checks.append({"name": name, "status": "BLOCKED", "reason": str(error)[:240]})

    def runtime():
        import cv2
        import fastapi
        import numpy
        import psutil
        import uvicorn

        return {
            "python": sys.version.split()[0],
            "opencv": cv2.__version__,
            "fastapi": fastapi.__version__,
            "numpy": numpy.__version__,
            "uvicorn": uvicorn.__version__,
            "rss_mb": psutil.Process().memory_info().rss / 1048576,
        }

    check("backend_dependencies", runtime)
    check(
        "ffmpeg",
        lambda: (
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, timeout=10)
            .stdout.decode()
            .splitlines()[0]
        ),
    )

    def model():
        from dormguard.config import Settings
        from dormguard.model import load_model

        if not args.model_rights_confirmed:
            raise ValueError("Pass --model-rights-confirmed only for permitted local evaluation")
        settings = Settings(
            args.workspace,
            auth_mode="local",
            demo_mode="offline",
            model_manifest=args.model_manifest,
            model_rights_confirmed=True,
        )
        before = time.monotonic()
        adapter = load_model(settings)
        return {"model": adapter.name, "load_seconds": time.monotonic() - before, "device": adapter.device}

    check("real_model_load_and_digests", model)

    def media():
        nonlocal sources
        sources = read_assets(args.assets.resolve())
        import cv2

        details = []
        for item in sources:
            capture = cv2.VideoCapture(item["original"])
            try:
                capture.set(cv2.CAP_PROP_POS_MSEC, item.get("start_seconds", 0) * 1000)
                ok, frame = capture.read()
                if not ok or frame is None:
                    raise ValueError("Source cannot decode: " + item["name"])
                details.append({"name": item["name"], "scenario": item["scenario"], "decode": True})
            finally:
                capture.release()
        return details

    check("authorized_video_assets", media)
    check(
        "local_frontend",
        lambda: (
            all((ROOT / "dormguard" / p).is_file() for p in ("index.html", "app.js", "style.css"))
            or (_ for _ in ()).throw(ValueError("Frontend assets missing"))
        ),
    )

    def storage():
        import tempfile

        from dormguard.store import Store

        parent = args.workspace.resolve().parent
        if not parent.is_dir():
            raise ValueError("Workspace parent must already exist")
        if shutil.disk_usage(parent).free < 2 * 1024**3:
            raise ValueError("At least 2 GiB free disk required")
        with tempfile.TemporaryDirectory(prefix="dormguard-offline-check-", dir=parent) as temporary:
            store = Store(Path(temporary))
            try:
                with store.db.read() as conn:
                    versions = [
                        r["version"] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
                    ]
                probe = store.evidence_dir / "write-probe"
                probe.write_bytes(b"local readiness probe")
                assert probe.read_bytes() == b"local readiness probe"
            finally:
                store.db.close()
        return {
            "database": "disposable SQLite initialized",
            "schema_versions": versions,
            "evidence_write": True,
        }

    check("storage_database_disk", storage)

    def port():
        if not 1024 <= args.port <= 65535:
            raise ValueError("Select an unprivileged port")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", args.port))
        return {"host": "127.0.0.1", "port": args.port, "available": True}

    check("loopback_port", port)
    check(
        "positive_scenario_coverage",
        lambda: (
            True
            if any(s["scenario"] == "staged_physical_conflict" for s in sources)
            else (_ for _ in ()).throw(
                ValueError("No rights-cleared physical-conflict positive; sports is a negative control")
            )
        ),
    )
    blocked = [c for c in checks if c["status"] != "PASS"]
    return {
        "status": "BLOCKED" if blocked else "PASS",
        "checks": checks,
        "runtime_seconds": round(time.monotonic() - started, 3),
        "scope": "Local prerequisites only; complete demonstration acceptance requires browser/model results",
    }, sources


def child_environment(directory, args):
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "TMPDIR", "LANG", "SYSTEMROOT"}}
    env.update(
        DORMGUARD_DATA=str(directory),
        DORMGUARD_ENV="evaluation",
        DORMGUARD_AUTH="local",
        DORMGUARD_DEMO_MODE="offline",
        DORMGUARD_PUBLIC_URL=f"http://127.0.0.1:{args.port}",
        MODEL_MANIFEST=str(args.model_manifest.resolve()),
        DORMGUARD_MODEL_RIGHTS_CONFIRMED="1",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
    )
    for index, path in enumerate(
        sorted((directory / "test_sources").glob("source-*.mp4"), key=lambda p: int(p.stem.split("-")[1]))
    ):
        env[f"DORMGUARD_CAMERA_OFFLINE_{index}"] = str(path)
    return env


def prepare_sources(root, sources):
    """Cache only digest-keyed derivatives, never modify an original."""
    cache = root / "sources"
    cache.mkdir(exist_ok=True, mode=0o700)
    prepared = []
    for source in sources:
        key = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
        output = cache / (key + ".mp4")
        if not output.is_file():
            pending = cache / (key + ".pending.mp4")
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-ss",
                    str(source.get("start_seconds", 0)),
                    "-i",
                    source["original"],
                    "-t",
                    str(source["end_seconds"] - source.get("start_seconds", 0)),
                    "-vf",
                    "scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-threads",
                    "2",
                    "-y",
                    str(pending),
                ],
                check=True,
                timeout=120,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
            )
            os.chmod(pending, 0o400)
            pending.replace(output)
        prepared.append(dict(source, prepared_file=str(output), prepared_sha256=sha256(output)))
    return prepared


def stop(processes):
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def wait_for_ready(origin, processes, directory, timeout=120):
    """Wait for fresh model/worker readiness; an alive API alone is insufficient."""
    deadline = time.monotonic() + timeout
    while True:
        if any(process.poll() is not None for process in processes):
            raise RuntimeError(
                "A demo service exited during startup; inspect private logs in " + str(directory)
            )
        try:
            with urlopen(origin + "/ready", timeout=2) as response:
                status = json.load(response)
                if response.status == 200 and status.get("model_ready") and status.get("worker_fresh"):
                    return
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("Model/worker readiness timed out; inspect private logs in " + str(directory))
        time.sleep(0.2)


def start(args, sources):
    from dormguard.offline import prepare_generation, write_private

    if args.workspace.is_symlink():
        raise ValueError("Demo workspace must not be a symlink")
    root = args.workspace.resolve()
    if root.exists() and (root.is_symlink() or not (root / "workspace.json").is_file()):
        raise ValueError("Refusing existing unmarked workspace; choose a new private directory")
    root.mkdir(mode=0o700, parents=False, exist_ok=True)
    marker = root / "workspace.json"
    if not marker.exists():
        write_private(marker, {"format": "dormguard-offline-workspace-v1"})
    if json.loads(marker.read_text()).get("format") != "dormguard-offline-workspace-v1":
        raise ValueError("Unrecognized workspace marker")
    lock = (root / "launcher.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ValueError("This demo workspace already has a running launcher") from None
    prepared = prepare_sources(root, sources)
    settings_path = root / "settings.json"
    configuration = {
        "sources": prepared,
        "model_manifest": str(args.model_manifest.resolve()),
        "port": args.port,
    }
    if settings_path.exists():
        old = json.loads(settings_path.read_text())
        if old != configuration:
            raise ValueError(
                "Workspace configuration changed; use a new workspace to preserve prior settings"
            )
    else:
        write_private(settings_path, configuration)
    active = root / "active.json"
    if active.exists():
        name = json.loads(active.read_text())["generation"]
        import uuid

        if str(uuid.UUID(name)) != name:
            raise ValueError("Invalid active generation")
        directory = root / "runs" / name
    else:
        directory = prepare_generation(root, prepared)
        write_private(active, {"generation": directory.name})
    processes = []
    try:
        while True:
            env = child_environment(directory, args)
            for name in ("dormguard.app", "dormguard.worker"):
                log = (directory / (name.split(".")[-1] + ".log")).open("ab")
                try:
                    command = [sys.executable, "-m", name]
                    if name == "dormguard.app":
                        command = [
                            sys.executable,
                            "-m",
                            "uvicorn",
                            "dormguard.app:create_app",
                            "--factory",
                            "--host",
                            "127.0.0.1",
                            "--port",
                            str(args.port),
                            "--no-proxy-headers",
                            "--no-access-log",
                        ]
                    processes.append(
                        subprocess.Popen(
                            command,
                            cwd=ROOT,
                            env=env,
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=log,
                        )
                    )
                finally:
                    log.close()
            wait_for_ready(env["DORMGUARD_PUBLIC_URL"], processes, directory)
            print("Demo ready: " + env["DORMGUARD_PUBLIC_URL"], flush=True)
            print("Persistent private workspace: " + str(root), flush=True)
            print(
                "Real inference; prerecorded sources. No Telegram worker or external delivery. Ctrl-C preserves this run.",
                flush=True,
            )
            request = directory / "reset-request.json"
            while not request.exists():
                if any(p.poll() is not None for p in processes):
                    raise RuntimeError("A demo service exited; inspect private per-run logs")
                time.sleep(0.2)
            stop(processes)
            processes = []
            from dormguard.offline import rotate_generation

            directory = rotate_generation(root, directory, prepared)
            pending = root / "active.pending.json"
            write_private(pending, {"generation": directory.name})
            pending.replace(active)
    finally:
        stop(processes)
        lock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "start"])
    parser.add_argument("--workspace", type=Path, default=ROOT / ".offline-demo")
    parser.add_argument("--assets", type=Path, default=ROOT / "docs/ai/OFFLINE_DEMO_ASSETS.json")
    parser.add_argument(
        "--model-manifest", type=Path, default=ROOT / "docs/ai/OFFLINE_VIDEOMAE_MANIFEST.json"
    )
    parser.add_argument("--model-rights-confirmed", action="store_true")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Allow only missing positive coverage; never missing runtime/model/video prerequisites",
    )
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args(argv)
    report, sources = readiness(args)
    print(json.dumps(report, indent=2), flush=True)
    blocked = [c["name"] for c in report["checks"] if c["status"] != "PASS"]
    if blocked and not (
        args.command == "start" and args.allow_partial and blocked == ["positive_scenario_coverage"]
    ):
        return 2
    if args.command == "check":
        return 0
    try:
        start(args, sources)
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print(json.dumps({"status": "FAIL", "reason": str(error)[:300]}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

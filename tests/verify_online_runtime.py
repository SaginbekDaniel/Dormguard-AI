"""Opt-in actual supervisor/TLS/model/SQLite/browser/restart acceptance on NEW demo data.

Run prepare_online_demo.py first. No hosting provisioning or external requests.
Self-signed TLS is only a local test; this cannot establish public deployment.
"""

import argparse
import hashlib
import http.client
import json
import os
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    directory = args.workspace.resolve()
    access = json.loads((directory / "online-access.json").read_text())
    if access["origin"] != "https://127.0.0.1:9443":
        parser.error("Only the explicitly prepared loopback HTTPS workspace is supported")
    started = time.monotonic()
    checks, memory = [], []
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "TMPDIR", "LANG"}}
    env.update(
        DORMGUARD_MODE="online_demo",
        DORMGUARD_ENV="evaluation",
        DORMGUARD_AUTH="local",
        DORMGUARD_HTTPS="1",
        DORMGUARD_PUBLIC_URL=access["origin"],
        DORMGUARD_DATA=str(directory),
        DORMGUARD_MODEL_RIGHTS_CONFIRMED="1",
        MODEL_MANIFEST=str(directory / "model/manifest.json"),
        DORMGUARD_CAMERA_ONLINE=str(directory / "test_sources/permitted-demo.mp4"),
        DORMGUARD_BIND_HOST="127.0.0.1",
        PORT="8890",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
    )
    cert, key = directory / "test-cert.pem", directory / "test-key.pem"
    if not cert.exists():
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        key.chmod(0o600)

    class Proxy(BaseHTTPRequestHandler):
        def do_GET(self):
            self.forward()

        do_POST = do_PATCH = do_DELETE = do_GET

        def log_message(self, *_):
            pass

        def forward(self):
            upstream = http.client.HTTPConnection("127.0.0.1", 8890, timeout=30)
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                headers = {k: v for k, v in self.headers.items() if k.lower() not in {"connection"}}
                upstream.request(self.command, self.path, body or None, headers)
                response = upstream.getresponse()
                self.send_response(response.status)
                for k, v in response.getheaders():
                    if k.lower() not in {"connection", "transfer-encoding", "server", "date"}:
                        self.send_header(k, v)
                self.end_headers()
                while block := response.read1(65536):
                    self.wfile.write(block)
                    self.wfile.flush()
            except (OSError, http.client.HTTPException):
                pass  # Browser SSE/media disconnects in this disposable test proxy.
            finally:
                upstream.close()

    server = ThreadingHTTPServer(("127.0.0.1", 9443), Proxy)
    server.daemon_threads = True
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log = (directory / "acceptance-service.log").open("ab")
    process = None

    def launch():
        return subprocess.Popen(
            [sys.executable, "-m", "dormguard.service"],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
        )

    def stop():
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def until(operation, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Supervisor exited; inspect private service log")
            try:
                result = operation()
                if result:
                    return result
            except (httpx.TransportError, json.JSONDecodeError):
                pass
            time.sleep(0.2)
        raise RuntimeError("Timed out waiting for actual application observation")

    result = {"status": "FAIL", "scope": "Actual local HTTPS online_demo; public hosting/container NOT_RUN"}
    try:
        with httpx.Client(base_url=access["origin"], verify=False, timeout=10, trust_env=False) as client:
            process = launch()
            until(lambda: client.get("/ready").status_code == 200, 120)
            checks.append("fresh real model/worker/database readiness")
            cold_start = time.monotonic() - started
            assert client.get("/api/v1/incidents").status_code == 401
            assert client.post("/api/v1/demo/enter", json={}).status_code == 404
            login = client.post(
                "/api/v1/auth/login", json={"email": access["email"], "password": access["password"]}
            )
            login.raise_for_status()
            cookie = login.headers["set-cookie"].lower()
            assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
            csrf = {"X-CSRF-Token": login.json()["csrf_token"]}
            checks.append(
                "secure credential login, secure cookie, disabled offline bypass and denied anonymous incidents"
            )
            assert not client.get("/api/v1/incidents").json()["items"], (
                "Use a new prepared acceptance workspace"
            )
            camera = "/api/v1/cameras/" + access["camera_id"]
            detection_started = time.monotonic()
            client.patch(camera, json={"enabled": True}, headers=csrf).raise_for_status()
            incident = until(lambda: next(iter(client.get("/api/v1/incidents").json()["items"]), None))
            latency = time.monotonic() - detection_started
            assert "VideoMAE" in incident["model"]
            detail_path = "/api/v1/incidents/" + incident["id"]
            detail = client.get(detail_path).json()
            evidence = detail["evidence"][0]
            evidence_path = "/api/v1/evidence/" + evidence["id"]
            content = client.get(evidence_path + "/download").content
            assert hashlib.sha256(content).hexdigest() == evidence["sha256"]
            assert client.post(evidence_path + "/verify", json={}, headers=csrf).json()["valid"]
            with httpx.Client(base_url=access["origin"], verify=False, trust_env=False) as anonymous:
                assert anonymous.get(evidence_path + "/download").status_code == 401
            for action in ["acknowledge", "confirm"]:
                client.post(
                    detail_path + "/" + action,
                    json={"reason": "Reviewed staged noncommercial test scene"},
                    headers=csrf,
                ).raise_for_status()
            assert client.get("/api/v1/alerts").json()["unread"] == 1
            checks.append(
                "actual inference, automatic incident, SHA256 evidence, anonymous evidence denial, alert and human confirmation"
            )
            for child in psutil.Process(process.pid).children():
                memory.append(
                    {
                        "role": "worker" if "dormguard.worker" in child.cmdline() else "api",
                        "rss_mb": round(child.memory_info().rss / 1048576, 2),
                    }
                )
            metrics = client.get("/api/v1/system").json()["workers"]
            node_env = dict(
                os.environ,
                DORMGUARD_ONLINE_ACCESS=str(directory / "online-access.json"),
                DORMGUARD_ONLINE_INCIDENT=incident["id"],
            )
            subprocess.run(
                ["node", "tests/online_browser.cjs"], cwd=ROOT, env=node_env, check=True, timeout=90
            )
            client.patch(camera, json={"enabled": False}, headers=csrf).raise_for_status()
            stop()
            process = launch()
            until(lambda: client.get("/ready").status_code == 200, 120)
            assert client.get(detail_path).json()["status"] == "CONFIRMED"
            assert (
                hashlib.sha256(client.get(evidence_path + "/download").content).hexdigest()
                == evidence["sha256"]
            )
            assert client.get("/api/v1/alerts").json()["unread"] == 1
            checks.append(
                "actual supervisor restart preserves secure session, decision, evidence and local notification"
            )
            result.update(
                status="PASS",
                checks=checks,
                cold_start_seconds=cold_start,
                detection_seconds=latency,
                memory=memory,
                workers=metrics,
                evidence_sha256=evidence["sha256"],
                runtime_seconds=time.monotonic() - started,
            )
    finally:
        stop()
        server.shutdown()
        log.close()
        Path("/private/tmp/dormguard-online-runtime-results.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "workers"}, indent=2))


if __name__ == "__main__":
    main()

"""Loopback integration fixture: real hosted Auth, controlled SQL/media.

Local SQL is disposable. Explicitly configured pinned hosted SQL retains its
private media/data directory and audited test rows. No provider double, password
fallback, admin credential or real footage. Incident is not model inference.
"""

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import uvicorn

from dormguard.app import create_app
from dormguard.config import Settings
from dormguard.playback import encode_clip
from dormguard.store import Store
from scripts.verify_hosted import PROJECT_REFERENCE, configuration, pinned_database_url


@contextmanager
def workspace(database_url):
    if database_url:
        root = Path(os.environ["DORMGUARD_HOSTED_E2E_DATA"]).absolute()
        if root.is_symlink():
            raise ValueError("Private data directory cannot be a symlink")
        if root.exists():
            if os.environ.get("DORMGUARD_HOSTED_E2E_REUSE") != "1" or (root.stat().st_mode & 0o077):
                raise ValueError("Existing data directory requires explicit private reuse")
        else:
            root.mkdir(mode=0o700)
        yield root  # Never delete evidence referenced by retained hosted SQL.
    else:
        with tempfile.TemporaryDirectory(prefix="dormguard-hosted-browser-") as directory:
            yield Path(directory)


def main():
    _, _, identities = configuration(os.environ, PROJECT_REFERENCE, True)
    raw_database = os.environ.get("DORMGUARD_DATABASE_URL")
    database_url = (
        pinned_database_url(raw_database, PROJECT_REFERENCE, os.environ.get("DORMGUARD_HOSTED_CA_CERT"))
        if raw_database
        else None
    )
    with workspace(database_url) as root:
        marker = root / "hosted-scope.json"
        settings = Settings(
            root,
            database_url=database_url,
            auth_mode="supabase",
            public_url="http://127.0.0.1:8878",
            supabase_url=os.environ["SUPABASE_URL"],
            supabase_anon_key=os.environ["SUPABASE_ANON_KEY"],
        )
        store = Store(root, database_url)
        if database_url:
            with store.db.read() as conn:
                for identity in identities:
                    present = conn.execute(
                        "SELECT 1 FROM auth.users WHERE id::text=? AND email=?",
                        (identity["id"], identity["email"]),
                    ).fetchone()
                    if not present:
                        raise ValueError("Controlled Auth identity differs from hosted database")
                existing = [
                    dict(row)
                    for row in conn.execute("SELECT id,email,role,enabled FROM public.users").fetchall()
                ]
            expected = {identity["id"]: identity for identity in identities if identity["role"]}
            if bool(existing) != marker.exists():
                raise ValueError(
                    "Retained hosted SQL and private workspace must match; inspect partial setup"
                )
            if existing and (
                len(existing) != len(expected)
                or any(
                    row["id"] not in expected
                    or row["email"] != expected[row["id"]]["email"]
                    or row["role"] != expected[row["id"]]["role"]
                    or not row["enabled"]
                    for row in existing
                )
            ):
                raise ValueError("Refusing to overwrite unexpected hosted account state")
        else:
            existing = []
        for identity in identities:
            if identity["role"] and not existing:
                store.provision_user(
                    identity["email"],
                    identity["role"],
                    user_id=identity["id"],
                    display_name="Controlled hosted " + identity["label"],
                )
        if marker.exists():
            scope = json.loads(marker.read_text())
            if scope.get("project_ref") != PROJECT_REFERENCE:
                raise ValueError("Retained workspace belongs to another target")
            incident = {"id": scope["incident_id"]}
        else:
            camera = store.create_camera(
                "CONTROLLED generated source",
                "No real people or camera",
                "test",
                "DORMGUARD_CAMERA_HOSTED_TEST",
                identities[0]["id"],
            )
            source = root / "generated.mp4"
            frames = [np.full((180, 320, 3), (index * 10, 40, 60), dtype=np.uint8) for index in range(20)]
            encode_clip(frames, 4, source)
            incident = store.create_incident(
                camera["id"],
                0.0,
                "CONTROLLED_HOSTED_AUTHORIZATION_FIXTURE_NOT_INFERENCE",
                event_key="controlled-hosted-authorization",
                evidence_source=source,
                clip_start=time.time() - 5,
                clip_end=time.time(),
            )
            marker.write_text(json.dumps({"project_ref": PROJECT_REFERENCE, "incident_id": incident["id"]}))
            marker.chmod(0o600)
        access = root / "browser-access.json"
        if not access.exists():
            with access.open("x") as target:
                os.chmod(access, 0o600)
                json.dump(
                    {
                        "origin": settings.public_url,
                        "disposable": True,
                        "auth": "real_hosted",
                        "database": "hosted_postgres" if database_url else "local_sqlite",
                        "project_ref": PROJECT_REFERENCE,
                        "identities": identities,
                        "incident_id": incident["id"],
                    },
                    target,
                )
        print(
            "REAL HOSTED AUTH / "
            + ("HOSTED POSTGRES; DATA RETAINED" if database_url else "LOCAL DISPOSABLE SQL")
            + " / CONTROLLED GENERATED INCIDENT",
            flush=True,
        )
        print("Private browser access file: " + str(access), flush=True)
        try:
            uvicorn.run(
                create_app(settings, store),
                host="127.0.0.1",
                port=8878,
                log_level="warning",
                access_log=False,
            )
        finally:
            store.db.close()


if __name__ == "__main__":
    main()

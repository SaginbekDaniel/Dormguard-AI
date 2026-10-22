"""Local demo workspace boundary. Never provisions into an existing application DB."""

import json
import os
import uuid
from pathlib import Path

from .store import DomainError

MARKER = "offline-demo.json"


def write_private(path, value):
    with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def demo_identity(settings, store):
    if settings.demo_mode != "offline":
        raise DomainError("Offline demo is not enabled", 404)
    path = settings.data_dir / MARKER
    try:
        if path.is_symlink() or path.stat().st_size > 4096:
            raise ValueError
        marker = json.loads(path.read_text())
        if marker.get("format") != "dormguard-offline-v1":
            raise ValueError
        uuid.UUID(marker["generation"])
        user = store.user(marker["director_id"])
        if not user or user["role"] != "director" or user["email"] != "director@offline.demo.invalid":
            raise ValueError
        with store.db.read() as conn:
            users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            remote = conn.execute("SELECT COUNT(*) AS n FROM cameras WHERE kind!='test'").fetchone()["n"]
        if users != 1 or remote:
            raise ValueError
        return user
    except (OSError, ValueError, KeyError, TypeError):
        raise DomainError("Offline workspace marker or isolated demo identity is invalid", 409) from None


def request_reset(settings, store, actor_id):
    """Request generation rotation from the launcher; never remove evidence/SQL rows."""
    demo_identity(settings, store)
    store.audit(actor_id, "demo.reset_requested", "offline-workspace")
    target = settings.data_dir / "reset-request.json"
    try:
        write_private(target, {"confirmed": True})
    except FileExistsError:
        pass  # Idempotent request; the supervisor consumes this generation only once.
    return {"reset": "requested", "previous_run": "preserved", "reconnect": True}


def prepare_generation(root: Path, source_configs):
    """New unique private generation, preserving all prior runs and original assets."""
    from .store import Store

    generation = str(uuid.uuid4())
    directory = root / "runs" / generation
    directory.mkdir(parents=True, mode=0o700)
    store = Store(directory)
    try:
        director = store.provision_user(
            "director@offline.demo.invalid", "director", display_name="Demo Dormitory Director"
        )
        (directory / "test_sources").mkdir(mode=0o700)
        import shutil

        for index, item in enumerate(source_configs):
            source = directory / "test_sources" / f"source-{index}.mp4"
            shutil.copyfile(item["prepared_file"], source)
            os.chmod(source, 0o400)
            camera = store.create_camera(
                item["name"],
                "Prerecorded demo · " + item["scenario"],
                "test",
                f"DORMGUARD_CAMERA_OFFLINE_{index}",
                director["id"],
            )
            store.update_camera(camera["id"], {"enabled": False}, director["id"])
        write_private(
            directory / MARKER,
            {
                "format": "dormguard-offline-v1",
                "generation": generation,
                "director_id": director["id"],
            },
        )
        store.audit(director["id"], "demo.run_started", generation, {"prerecorded": True})
    finally:
        store.db.close()
    return directory


def rotate_generation(root, previous_directory, source_configs):
    """Preserve presentation settings through private server metadata, never public rows."""
    from .store import Store

    previous = Store(previous_directory)
    try:
        profile = previous.list_users()[0]["display_name"]
        cameras = previous.cameras(private=True)
    finally:
        previous.db.close()
    directory = prepare_generation(root, source_configs)
    fresh = Store(directory)
    try:
        user = fresh.list_users()[0]
        if profile:
            fresh.update_profile(user["id"], profile)
        before_by_ref = {camera["source_ref"]: camera for camera in cameras}
        for after in fresh.cameras(private=True):
            before = before_by_ref.get(after["source_ref"])
            if before:
                fresh.update_camera(
                    after["id"], {key: before[key] for key in ("name", "location")}, user["id"]
                )
        fresh.audit(user["id"], "demo.reset_completed", directory.name, {"previous_evidence": "preserved"})
    finally:
        fresh.db.close()
    return directory

"""Bounded browser derivatives. Originals are never transcoded in place."""

import fcntl
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_encoders = threading.BoundedSemaphore(2)


class MediaError(Exception):
    pass


@contextmanager
def _publication_lock(path):
    """Serialize a digest's derivative/seal publication across processes."""
    with path.open("a+b") as handle:
        os.chmod(path, 0o600)
        deadline = time.monotonic() + 95
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise MediaError("Derivative generation busy; try again") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _ffmpeg():
    value = shutil.which("ffmpeg")
    if not value:
        raise MediaError("FFmpeg is not installed; browser playback unavailable")
    return value


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def encode_clip(frames, fps, target):
    """Encode bounded BGR frames into a new H.264 MP4, never an original."""
    import cv2

    target = Path(target)
    if not frames or len(frames) > 1800 or not 0 < fps <= 60 or target.exists():
        raise MediaError("Invalid clip frames, frame rate or destination")
    height, width = frames[0].shape[:2]
    if width > 1920 or height > 1080:
        raise MediaError("Clip exceeds supported dimensions")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _encoders, tempfile.TemporaryDirectory(prefix="encode-", dir=target.parent) as directory:
        source = Path(directory) / "capture.avi"
        writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"MJPG"), fps, (width, height))
        if not writer.isOpened():
            raise MediaError("Video encoder unavailable")
        try:
            for frame in frames:
                if frame.shape[:2] != (height, width):
                    raise MediaError("Inconsistent clip dimensions")
                writer.write(frame)
        finally:
            writer.release()
        result = Path(directory) / "clip.mp4"
        _transcode(source, result)
        os.chmod(result, 0o600)
        os.replace(result, target)
    return target


def _transcode(source, target):
    try:
        subprocess.run(
            [
                _ffmpeg(),
                "-nostdin",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(source),
                "-an",
                "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-threads",
                "2",
                "-n",
                str(target),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=90,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        raise MediaError("Browser derivative encoding failed") from None
    if not target.is_file() or target.stat().st_size < 64:
        raise MediaError("Browser derivative is empty")


def browser_derivative(store, item):
    source = store.evidence_path(item)
    if digest_file(source) != item["sha256"]:
        raise MediaError("Original evidence integrity check failed")
    directory = store.data_dir / "playback"
    directory.mkdir(exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    target = directory / (item["sha256"] + ".mp4")
    seal = target.with_suffix(".sha256")
    with _encoders, _publication_lock(target.with_suffix(".lock")):
        if target.exists():
            if not seal.is_file() or digest_file(target) != seal.read_text().strip():
                raise MediaError("Browser derivative integrity check failed")
            return target
        with tempfile.TemporaryDirectory(prefix="playback-", dir=directory) as work:
            temporary = Path(work) / "video.mp4"
            _transcode(source, temporary)
            digest = digest_file(temporary)
            os.chmod(temporary, 0o400)
            os.replace(temporary, target)
            temporary_seal = Path(work) / "video.sha256"
            temporary_seal.write_text(digest + "\n")
            os.chmod(temporary_seal, 0o400)
            os.replace(temporary_seal, seal)
    return target

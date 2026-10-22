"""Shared camera ingestion and bounded single-model suspected-event pipeline.

Run ``python -m dormguard.worker``. Files are explicitly simulated sources;
``--once`` reaches their end and exits (RTSP is bounded by --max-seconds).
No detector is substituted when the configured real model is unavailable.
"""

from __future__ import annotations

import argparse
import fcntl
import math
import os
import queue
import resource
import shutil
import signal
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .model import ModelBlocked, load_model
from .telemetry import HealthPublisher, Metrics

# Set before a decoder/model dependency can import OpenCV. Native FFmpeg errors
# can print credential-bearing source URLs; sanitized health records are the
# operational error channel for this private-camera process.
os.environ["OPENCV_FFMPEG_LOGLEVEL"] = "-8"
os.environ["OPENCV_LOG_LEVEL"] = "SILENT"


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def rss_mb() -> float:
    # Current resident pages on Linux; ru_maxrss is a conservative high-water
    # fallback on macOS. This pauses work; it is not a hard allocator cap.
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 1024**2
    except (OSError, ValueError, IndexError):
        high = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return high / (1024**2 if sys.platform == "darwin" else 1024)


class TemporalGate:
    """At least two positives among three consecutive fresh window decisions."""

    def __init__(self, threshold: float, confirmations: int = 2, cooldown: float = 60, max_gap: float = 30):
        if not 0 <= threshold <= 1 or confirmations not in (2, 3) or cooldown < 0:
            raise ValueError("invalid_temporal_configuration")
        self.threshold, self.confirmations, self.cooldown = threshold, confirmations, cooldown
        self.max_gap = max_gap
        self.history = deque(maxlen=3)
        self.last_timestamp = float("-inf")
        self.last_event = float("-inf")
        self.generation = 0
        self.decisions = deque(maxlen=3)

    def observe(self, score: float, timestamp: float, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if (
            not math.isfinite(timestamp)
            or not math.isfinite(score)
            or not 0 <= score <= 1
            or timestamp <= self.last_timestamp
            or timestamp > now + 2
            or timestamp < now - 120
        ):
            raise ValueError("invalid_inference_timestamp_or_score")
        if math.isfinite(self.last_timestamp) and timestamp - self.last_timestamp > self.max_gap:
            self.reset()
        self.last_timestamp = timestamp
        self.history.append(score >= self.threshold)
        self.decisions.append(
            {
                "score": score,
                "frame_utc": iso(timestamp),
                "decision_observed_utc": iso(now),
                "decision_monotonic": time.monotonic(),
            }
        )
        if score < self.threshold or sum(self.history) < self.confirmations:
            return False
        if timestamp - self.last_event < self.cooldown:
            return False
        self.last_event = timestamp
        self.history.clear()
        return True

    def reset(self):
        self.history.clear()
        self.decisions.clear()
        self.generation += 1

    def checkpoint(self):
        return (
            self.generation,
            tuple(self.history),
            self.last_timestamp,
            self.last_event,
            tuple(self.decisions),
        )

    def rollback(self, checkpoint):
        generation, history, timestamp, last_event, decisions = checkpoint
        # A decoder interruption invalidates pre-gap observations, even if SQL
        # or encoding fails while the decoder is resetting continuity.
        if self.generation == generation:
            self.history = deque(history, maxlen=3)
            self.last_timestamp, self.last_event = timestamp, last_event
            self.decisions = deque(decisions, maxlen=3)


class FrameSampler:
    """Advance a fixed cadence instead of accumulating per-frame rounding drift."""

    def __init__(self, fps, start):
        if not math.isfinite(fps) or fps <= 0 or not math.isfinite(start):
            raise ValueError("invalid_sampling_clock")
        self.interval, self.next = 1 / fps, start

    def accept(self, tick):
        if not math.isfinite(tick):
            raise ValueError("invalid_sampling_timestamp")
        if tick + 1e-6 < self.next:
            return False
        # Skip missed slots after interruption; never emit a backlog of stale
        # copies of the current frame to satisfy the old cadence.
        slots = max(1, math.floor((tick - self.next + 1e-6) / self.interval) + 1)
        self.next += slots * self.interval
        return True


@dataclass
class CameraState:
    camera: dict
    ring: deque
    gate: TemporalGate
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_window: float = float("-inf")
    last_frame: float = float("-inf")
    pending_incident: str | None = None
    post_until: float = 0
    post_frames: list = field(default_factory=list)
    analysis: str = "not_started"
    last_inference_utc: str | None = None
    analysis_started_monotonic: float = field(default_factory=time.monotonic)
    sampled_frames: int = 0
    decoded_frames: int = 0
    reconnects: int = 0
    analyzed_windows: int = 0
    skipped_windows: int = 0


@dataclass
class Window:
    state: CameraState
    frames: tuple
    timestamp: float
    enqueued_monotonic: float = field(default_factory=time.monotonic)
    generation: int = field(init=False)

    def __post_init__(self):
        self.generation = self.state.gate.generation


def open_capture(source: str):
    """Force FFmpeg so RTSP open/read timeout properties are applied at open."""
    # Native decoder logs can include URL credentials. Keep only application
    # error classes in health records, including failed connection attempts.
    os.environ["OPENCV_FFMPEG_LOGLEVEL"] = "-8"
    os.environ["OPENCV_LOG_LEVEL"] = "SILENT"
    import cv2

    if hasattr(cv2, "setLogLevel"):
        cv2.setLogLevel(0)
    if source.lower().startswith(("rtsp://", "rtsps://")):
        return cv2.VideoCapture(
            source,
            cv2.CAP_FFMPEG,
            [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000],
        )
    return cv2.VideoCapture(source, cv2.CAP_FFMPEG)


def write_preview(path: Path, frame) -> None:
    import cv2

    ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
    if not ok:
        raise RuntimeError("preview_encode_failed")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(jpeg.tobytes())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Worker:
    def __init__(self, settings, store, model=None, *, model_loader=load_model):
        self.settings, self.store = settings, store
        self.stop = threading.Event()
        self.queue = queue.Queue(maxsize=settings.max_inference_queue)
        self.admission_lock = threading.Lock()
        self.sources = {}  # one decoder thread per distinct private resolved source
        self.completed = set()
        self.states = {}
        self.active_ids = None
        self.dropped = self.inferred = self.errors = 0
        self.metrics = Metrics()
        self.health_publisher = None  # owned only during run(), under the worker process lock
        import psutil

        self.process_meter = psutil.Process()
        self.process_meter.cpu_percent()
        self.model = model
        self.model_detail = ""
        if self.model is None:
            try:
                self.model = model_loader(settings)
            except ModelBlocked as error:
                self.model_detail = str(error)
        self.frames_dir = Path(settings.data_dir) / "frames"
        if self.frames_dir.is_symlink():
            raise RuntimeError("frames_directory_symlink_refused")
        self.frames_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.frames_dir, 0o700)
        self.temp_dir = Path(settings.data_dir) / "capture"
        if self.temp_dir.is_symlink():
            raise RuntimeError("capture_directory_symlink_refused")
        self.temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.temp_dir, 0o700)

    def _health(self, state, status: str, detail: str = ""):
        last = state.last_frame if math.isfinite(state.last_frame) else None
        fields = {"state": status, "last_frame_at": last, "detail": detail}
        if self.health_publisher is None:
            self.store.set_camera_health(state.camera["id"], **fields)
        else:
            self.health_publisher.submit("camera", state.camera["id"], **fields)

    def _new_state(self, camera):
        s = self.settings
        seconds = max(s.window_seconds, getattr(s, "evidence_pre_seconds", 5)) + 2
        return CameraState(
            camera,
            deque(maxlen=max(2, math.ceil(seconds * s.inference_fps) + 1)),
            TemporalGate(
                s.detection_threshold,
                s.confirmations,
                s.incident_cooldown_seconds,
                max(s.window_seconds, 2.1 * s.window_stride),
            ),
        )

    def _enqueue(self, item):
        # Serialize producers only; the single consumer may dequeue throughout.
        # Replace a pending window for the same camera with its latest window.
        # At capacity retain less-served cameras, then older arrivals on ties.
        # This bounds memory without repeatedly evicting the same cameras.
        with self.admission_lock:
            pending = []
            try:
                while True:
                    pending.append(self.queue.get_nowait())
            except queue.Empty:
                pass
            replaced = [window for window in pending if window.state is item.state]
            pending = [window for window in pending if window.state is not item.state]
            for old in replaced:
                old.state.skipped_windows += 1
                self.dropped += 1
            pending.append(item)
            if len(pending) > self.settings.max_inference_queue:
                dropped = max(
                    pending, key=lambda window: (window.state.analyzed_windows, window.enqueued_monotonic)
                )
                pending = [window for window in pending if window is not dropped]
                dropped.state.skipped_windows += 1
                dropped.state.analysis = "overloaded_queue"
                self.dropped += 1
            for window in pending:
                self.queue.put_nowait(window)

    def ingest(self, state, frame, timestamp):
        now = time.time()
        if (
            not math.isfinite(timestamp)
            or timestamp <= state.last_frame
            or timestamp > now + 2
            or timestamp < now - 10
        ):
            raise ValueError("invalid_frame_timestamp")
        write_preview(self.frames_dir / (state.camera["id"] + ".jpg"), frame)
        with state.lock:
            if state.last_frame > 0 and timestamp - state.last_frame > 2:
                state.ring.clear()
                state.gate.reset()
                if state.pending_incident:
                    # End available posthistory at the interruption; never join
                    # two sides of a missing interval into a continuous clip.
                    state.post_until = state.last_frame
            state.last_frame = timestamp
            state.sampled_frames += 1
            state.ring.append((timestamp, frame))
            if state.pending_incident and timestamp <= state.post_until:
                state.post_frames.append((timestamp, frame))
            if self.model is not None and timestamp - state.last_window >= self.settings.window_stride:
                window = tuple(x for x in state.ring if x[0] >= timestamp - self.settings.window_seconds)
                if (
                    len(window) >= 2
                    and window[-1][0] - window[0][0]
                    >= self.settings.window_seconds - 1 / self.settings.inference_fps
                ):
                    state.last_window = timestamp
                    self._enqueue(Window(state, window, timestamp))
        self._health(
            state, "connected", "simulated_test_source" if state.camera.get("kind") == "test" else ""
        )

    def _decoder(self, source, states, local_stop, once, max_seconds):
        try:
            import cv2
        except ImportError:
            self.errors += 1
            for state in states:
                self._health(state, "blocked", "opencv_runtime_missing")
            self.completed.add(source)
            return
        simulated = all(x.camera.get("kind") == "test" for x in states)
        finite_demo = simulated and getattr(self.settings, "demo_mode", None) == "offline"
        deadline = time.monotonic() + max_seconds if once and not simulated else float("inf")
        backoff = 1
        try:
            while not self.stop.is_set() and not local_stop.is_set() and time.monotonic() < deadline:
                cap = None
                try:
                    cap = open_capture(source)
                    if not cap.isOpened():
                        raise RuntimeError("camera_open_failed")
                    fps = cap.get(cv2.CAP_PROP_FPS)
                    if not math.isfinite(fps) or fps <= 0:
                        fps = 25
                    for state in states:
                        state.source_fps = fps
                    started = time.monotonic()
                    index = 0
                    sampler = FrameSampler(self.settings.inference_fps, started)
                    while not self.stop.is_set() and not local_stop.is_set() and time.monotonic() < deadline:
                        before_decode = time.monotonic()
                        ok, frame = cap.read()
                        self.metrics.observe("decode_seconds", time.monotonic() - before_decode)
                        if not ok:
                            if simulated:
                                for state in states:
                                    self._health(
                                        state,
                                        "completed" if once or finite_demo else "reconnecting",
                                        "simulated_test_video_ended"
                                        if once or finite_demo
                                        else "simulated_test_video_restarting",
                                    )
                                break
                            raise RuntimeError("camera_read_failed")
                        if simulated:
                            wait = started + index / fps - time.monotonic()
                            if wait > 0 and self.stop.wait(wait):
                                break
                        index += 1
                        for state in states:
                            state.decoded_frames += 1
                        tick = time.monotonic()
                        if not sampler.accept(tick):
                            continue
                        h, w = frame.shape[:2]
                        factor = min(640 / w, 360 / h, 1)
                        frame = cv2.resize(frame, (max(2, int(w * factor)), max(2, int(h * factor))))
                        timestamp = time.time()
                        for state in states:
                            self.ingest(state, frame, timestamp)
                        backoff = 1
                    if simulated and (once or finite_demo):
                        break
                except Exception as error:
                    self.errors += 1
                    for state in states:
                        state.reconnects += 1
                        self._health(state, "disconnected", type(error).__name__)
                        with state.lock:
                            state.ring.clear()
                            state.gate.reset()
                            if state.pending_incident:
                                state.post_until = state.last_frame
                    if once:
                        break
                finally:
                    if cap is not None:
                        cap.release()
                if not self.stop.is_set() and not local_stop.is_set():
                    local_stop.wait(backoff)
                    backoff = min(30, backoff * 2)
        finally:
            self.completed.add(source)

    def reconcile(self, camera_id=None, once=False, max_seconds=30):
        from .security import resolve_source

        cameras = [
            x
            for x in self.store.cameras(private=True)
            if x.get("enabled", True) and (not camera_id or x["id"] == camera_id)
        ]
        groups = {}
        for index, camera in enumerate(cameras):
            state = self.states.get(camera["id"])
            if state is None:
                state = self._new_state(camera)
                self.states[camera["id"]] = state
            else:
                state.camera = camera
            if index >= self.settings.max_cameras:
                self._health(state, "blocked", "camera_limit")
                continue
            try:
                source = str(resolve_source(self.settings, camera))
            except Exception:
                self._health(state, "blocked", "source_not_permitted")
                continue
            groups.setdefault(source, []).append(state)
        self.active_ids = {x.camera["id"] for group in groups.values() for x in group}
        for source, (thread, local_stop, ids) in list(self.sources.items()):
            new_ids = tuple(x.camera["id"] for x in groups.get(source, []))
            if new_ids != ids:
                local_stop.set()
                thread.join(timeout=6)
                if thread.is_alive():
                    # Do not open a second decoder while the old one is alive.
                    groups.pop(source, None)
                    continue
                del self.sources[source]
                self.completed.discard(source)
        for source, states in groups.items():
            if source in self.sources:
                continue
            local_stop = threading.Event()
            thread = threading.Thread(
                target=self._decoder, args=(source, states, local_stop, once, max_seconds), daemon=True
            )
            self.sources[source] = (thread, local_stop, tuple(x.camera["id"] for x in states))
            thread.start()

    def _encode(self, frames):
        from .playback import encode_clip

        path = self.temp_dir / (uuid.uuid4().hex + ".mp4")
        if len(frames) < 2:
            raise ValueError("insufficient_evidence_frames")
        duration = frames[-1][0] - frames[0][0]
        if duration <= 0:
            raise ValueError("invalid_evidence_timestamps")
        fps = (len(frames) - 1) / duration
        try:
            encode_clip([x[1] for x in frames], fps, path)
            return path
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def process(self, window):
        state, timestamp = window.state, window.timestamp
        with state.lock:
            if window.generation != state.gate.generation:
                self.dropped += 1
                state.skipped_windows += 1
                state.analysis = "insufficient_continuity"
                return
        if self.active_ids is not None and state.camera["id"] not in self.active_ids:
            self.dropped += 1
            return
        if time.time() - timestamp > self.settings.window_seconds + 10:
            self.dropped += 1
            state.analysis = "overloaded_stale_window"
            return
        if rss_mb() > self.settings.max_rss_mb:
            self.dropped += 1
            self.model_detail = "rss_budget_inference_paused"
            state.analysis = "overloaded_memory"
            return
        submitted_utc, submitted = iso(time.time()), time.monotonic()
        self.metrics.observe("queue_wait_seconds", max(0, submitted - window.enqueued_monotonic))
        self.metrics.observe("frame_age_seconds", max(0, time.time() - timestamp))
        # Freeze prehistory before slow inference can evict it from the ring.
        with state.lock:
            frames = tuple(
                x
                for x in state.ring
                if timestamp - getattr(self.settings, "evidence_pre_seconds", 5) <= x[0] <= timestamp
            )
        try:
            score = self.model.predict([x[1] for x in window.frames])
        except Exception:
            state.analysis = "inference_failed"
            raise
        inferred_utc = iso(time.time())
        inference_seconds = time.monotonic() - submitted
        state.last_inference_seconds = inference_seconds
        self.metrics.observe("inference_seconds", inference_seconds)
        self.inferred += 1
        state.analyzed_windows += 1
        self.model_detail = ""
        with state.lock:
            if window.generation != state.gate.generation:
                self.dropped += 1
                state.skipped_windows += 1
                state.analysis = "insufficient_continuity"
                return
            if state.pending_incident:
                return
            state.last_inference_utc = inferred_utc
            checkpoint = state.gate.checkpoint()
            if not state.gate.observe(score, timestamp):
                state.analysis = (
                    "suspected_below_confirmation"
                    if score >= state.gate.threshold
                    else "no_suspicion_in_last_window"
                )
                return
            first_positive = next(d for d in state.gate.decisions if d["score"] >= state.gate.threshold)
            confirmation_seconds = time.monotonic() - first_positive["decision_monotonic"]
            self.metrics.observe("confirmation_span_seconds", confirmation_seconds)
            context = {
                "T0": None,
                "T1_latest_sample_utc": iso(timestamp),
                "T2": submitted_utc,
                "T3": inferred_utc,
                "T4": first_positive["decision_observed_utc"],
                "T5": iso(time.time()),
                "inference_seconds_monotonic": inference_seconds,
                "confirmation_span_seconds_monotonic": confirmation_seconds,
                "temporal_decisions": list(state.gate.decisions),
                "threshold": state.gate.threshold,
                "window_start_utc": iso(window.frames[0][0]),
                "clock": "UTC wall time + process monotonic durations",
                "clock_sync_uncertainty_seconds": None,
                "event_onset": "unknown; latest sampled frame is not event onset",
                "visibility": "not_assessed",
            }
        # A real positive is persisted only with real available evidence. Encoder
        # failures remain visible and do not invent a clip or a successful event.
        path = None
        persistence_started = time.monotonic()
        try:
            path = self._encode(frames)
            incident = self.store.create_incident(
                state.camera["id"],
                score,
                self.model.name,
                detected_at=iso(timestamp),
                event_key=f"camera:{state.camera['id']}:{timestamp:.6f}",
                evidence_source=path,
                clip_start=frames[0][0],
                clip_end=frames[-1][0],
                cooldown_seconds=self.settings.incident_cooldown_seconds,
                detection_context=context,
            )
        except Exception:
            with state.lock:
                state.gate.rollback(checkpoint)
                state.analysis = "evidence_or_persistence_failed"
            raise
        finally:
            if path:
                path.unlink(missing_ok=True)
        self.metrics.observe("evidence_and_persistence_seconds", time.monotonic() - persistence_started)
        if incident:
            state.analysis = "temporally_confirmed_suspicion"
            with state.lock:
                state.pending_incident = incident["id"]
                state.post_until = timestamp + getattr(self.settings, "evidence_post_seconds", 5)
                state.post_frames = [x for x in state.ring if timestamp < x[0] <= state.post_until]
            # The incident/outbox commit already succeeded. A timing-record
            # failure must not roll back its gate or invent another incident.
            if hasattr(self.store, "record_pipeline_commit"):
                self.store.record_pipeline_commit(
                    incident["id"], iso(time.time()), time.monotonic() - persistence_started
                )

    def flush_post(self, final=False):
        for state in list(self.states.values()):
            with state.lock:
                if not state.pending_incident or not (final or time.time() >= state.post_until):
                    continue
                incident_id, frames = state.pending_incident, tuple(state.post_frames)
                state.pending_incident, state.post_frames = None, []
            if len(frames) < 2:
                self._health(state, "degraded", "post_evidence_unavailable")
                continue
            path = None
            try:
                path = self._encode(frames)
                self.store.add_evidence(incident_id, path, clip_start=frames[0][0], clip_end=frames[-1][0])
            except Exception as error:
                self.errors += 1
                self._health(state, "degraded", "post_evidence_failed_" + type(error).__name__)
            finally:
                if path:
                    path.unlink(missing_ok=True)

    def heartbeat(self, stopped=False):
        degraded = any(
            x.analysis
            in {
                "inference_failed",
                "overloaded_memory",
                "overloaded_queue",
                "overloaded_stale_window",
                "evidence_or_persistence_failed",
            }
            for x in self.states.values()
        )
        telemetry = self.health_publisher.snapshot() if self.health_publisher else None
        degraded = degraded or bool(telemetry and telemetry["degraded"])
        status = (
            "blocked" if self.model is None else ("degraded" if self.model_detail or degraded else "ready")
        )
        fields = dict(
            state="stopped" if stopped else status,
            detail=self.model_detail,
            metrics={
                "rss_mb": round(rss_mb(), 2),
                "cpu_percent_process": self.process_meter.cpu_percent(),
                "cpu_percent_basis": "100 percent equals one logical CPU since previous heartbeat",
                "disk_free_bytes": shutil.disk_usage(self.settings.data_dir).free,
                "gpu_utilization": None,
                "gpu_measurement_status": "NOT_RUN",
                "queue_depth": self.queue.qsize(),
                "dropped": self.dropped,
                "inferred": self.inferred,
                "errors": self.errors,
                "health_persistence": telemetry,
                "observed_at_utc": iso(time.time()),
                "camera_decoders": len(self.sources),
                "model": self.model.name if self.model else None,
                "timings": self.metrics.snapshot(),
                "per_camera": {
                    key: {
                        "sampled_frames": value.sampled_frames,
                        "decoded_frames": value.decoded_frames,
                        "analyzed_windows": value.analyzed_windows,
                        "last_inference_utc": value.last_inference_utc,
                        "analysis_rate_hz_lifetime": round(
                            value.analyzed_windows
                            / max(0.001, time.monotonic() - value.analysis_started_monotonic),
                            4,
                        ),
                        "skipped_windows": value.skipped_windows,
                        "reconnects": value.reconnects,
                        "analysis_state": value.analysis if self.model is not None else "model_unavailable",
                        "last_suspicion_score": value.gate.decisions[-1]["score"]
                        if value.gate.decisions
                        else None,
                        "positive_observations": sum(value.gate.history),
                        "required_confirmations": self.settings.confirmations,
                        "source_fps": getattr(value, "source_fps", None),
                        "last_inference_seconds": getattr(value, "last_inference_seconds", None),
                        "visibility": "not_assessed",
                        "last_frame_utc": iso(value.last_frame) if math.isfinite(value.last_frame) else None,
                    }
                    for key, value in self.states.items()
                },
            },
        )
        if self.health_publisher is None:
            self.store.heartbeat("inference", **fields)
        else:
            self.health_publisher.submit("worker", "inference", **fields)

    def run(self, *, once=False, camera_id=None, max_seconds=30):
        # Multi-host coordination is deliberately unsupported. One process owns
        # shared preview/rings; browser viewers never open camera connections.
        if self.health_publisher is not None:
            raise RuntimeError("camera_worker_cannot_restart")
        lock_path = Path(self.settings.data_dir) / "camera-worker.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("camera_worker_already_running") from None
            publisher = HealthPublisher(self.store)
            self.health_publisher = publisher
            publisher.start()
            try:
                self.reconcile(camera_id, once, max_seconds)
                self.heartbeat()
                last_refresh = time.monotonic()
                while not self.stop.is_set():
                    try:
                        window = self.queue.get(timeout=0.25)
                    except queue.Empty:
                        window = None
                    if window is not None and self.model is not None:
                        try:
                            self.process(window)
                        except Exception as error:
                            self.errors += 1
                            self.model_detail = "inference_or_evidence_failed_" + type(error).__name__
                    self.flush_post()
                    if (
                        once
                        and all(not x[0].is_alive() for x in self.sources.values())
                        and self.queue.empty()
                    ):
                        break
                    if time.monotonic() - last_refresh >= 2:
                        if not once:
                            self.reconcile(camera_id, once, max_seconds)
                        self.heartbeat()
                        last_refresh = time.monotonic()
            finally:
                self.stop.set()
                for thread, local_stop, _ in self.sources.values():
                    local_stop.set()
                    thread.join(timeout=6)
                try:
                    self.flush_post(final=True)
                    self.heartbeat(stopped=True)
                finally:
                    drained = publisher.close()
                if not drained:
                    raise RuntimeError("worker_telemetry_shutdown_incomplete")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--camera")
    parser.add_argument("--max-seconds", type=int, default=30)
    args = parser.parse_args(argv)
    if not 1 <= args.max_seconds <= 3600:
        parser.error("max-seconds must be 1..3600")
    from .config import load_settings
    from .store import Store

    settings = load_settings()
    worker = Worker(settings, Store(settings.data_dir, settings.database_url).init())
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: worker.stop.set())
    worker.run(once=args.once, camera_id=args.camera, max_seconds=args.max_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

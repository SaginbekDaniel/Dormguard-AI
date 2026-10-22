"""Bounded process metrics. No frames, source URLs, identities or provider tokens."""

import json
import logging
import math
import statistics
import threading
import time
from collections import defaultdict, deque


def distribution(values):
    values = sorted(values)
    if not values:
        return {"count": 0, "p50": None, "p90": None, "p95": None, "p99": None, "max": None}

    def percentile(q):
        at = (len(values) - 1) * q
        lower = int(at)
        return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (at - lower)

    return {
        "count": len(values),
        "p50": percentile(0.5),
        "p90": percentile(0.9) if len(values) >= 10 else None,
        "p95": percentile(0.95) if len(values) >= 20 else None,
        "p99": percentile(0.99) if len(values) >= 100 else None,
        "min": values[0],
        "max": values[-1],
        "mean": statistics.mean(values),
        "percentile_method": "linear interpolation; p90/95/99 require n>=10/20/100",
    }


class Metrics:
    def __init__(self, capacity=256):
        self.capacity = capacity
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.series = defaultdict(lambda: deque(maxlen=capacity))
        self.counts = defaultdict(int)

    def observe(self, name, value):
        if not math.isfinite(value) or value < 0:
            raise ValueError("invalid_metric")
        with self.lock:
            self.series[name].append(value)
            self.counts[name] += 1

    def snapshot(self):
        with self.lock:
            return {
                "uptime_seconds": time.monotonic() - self.started,
                "sample_capacity": self.capacity,
                "series": {
                    name: {**distribution(values), "total_observations": self.counts[name]}
                    for name, values in self.series.items()
                },
            }


class HealthPublisher:
    """One bounded writer for replaceable health observations, never incidents.

    Each key retains only its latest pending observation. A failed write is not
    replayed: a later observation can recover health without replaying uncertain
    SQL. Persisted state changes still use the Store's existing audit transaction.
    """

    def __init__(self, store, *, capacity=32, interval=2):
        if capacity < 1 or not math.isfinite(interval) or interval <= 0:
            raise ValueError("invalid_health_publisher_bounds")
        self.store, self.capacity, self.interval = store, capacity, interval
        self.condition = threading.Condition()
        self.pending = {}
        self.in_flight = 0
        self.closing = self.abandoned = False
        self.submitted = self.coalesced = self.dropped = self.failures = self.written = 0
        self.last_error = None
        self.last_batch_dropped = False
        self.thread = threading.Thread(target=self._run, name="camera-health-writer", daemon=True)

    def start(self):
        self.thread.start()

    def submit(self, kind, target, **fields):
        if kind not in {"camera", "worker"}:
            raise ValueError("invalid_health_observation_kind")
        key = (kind, target)
        with self.condition:
            if self.closing:
                self.dropped += 1
                return False
            self.submitted += 1
            if key in self.pending:
                self.coalesced += 1
            elif len(self.pending) == self.capacity:
                del self.pending[next(iter(self.pending))]
                self.dropped += 1
                self.last_batch_dropped = True
            self.pending[key] = (time.monotonic(), fields)
            self.condition.notify_all()
            return True

    def snapshot(self):
        with self.condition:
            return {
                "capacity": self.capacity,
                "pending": len(self.pending),
                "in_flight": self.in_flight,
                "submitted": self.submitted,
                "coalesced": self.coalesced,
                "dropped": self.dropped,
                "write_failures": self.failures,
                "written": self.written,
                "last_error_type": self.last_error,
                "degraded": bool(self.last_error or self.last_batch_dropped or self.abandoned),
                "shutdown_incomplete": self.abandoned,
                "flush_interval_seconds": self.interval,
            }

    def _run(self):
        next_flush = 0
        while True:
            with self.condition:
                while True:
                    if self.abandoned or (self.closing and not self.pending):
                        return
                    wait = max(0, next_flush - time.monotonic())
                    if self.pending and (not wait or self.closing):
                        break
                    self.condition.wait(wait if self.pending else None)
                batch, self.pending = self.pending, {}
                self.in_flight = len(batch)
                drops_before_batch = self.dropped
            batch_failed = False
            for (kind, target), (observed, fields) in batch.items():
                with self.condition:
                    if self.abandoned:
                        self.dropped += self.in_flight
                        self.in_flight = 0
                        return
                try:
                    # Old buffered heartbeats must not masquerade as current
                    # readiness after an outage. Current producers will replace
                    # them; camera last-frame freshness is independently checked.
                    if time.monotonic() - observed > 10:
                        with self.condition:
                            self.dropped += 1
                            self.last_batch_dropped = True
                        continue
                    if kind == "camera":
                        self.store.set_camera_health(target, **fields)
                    else:
                        self.store.heartbeat(target, **fields)
                    with self.condition:
                        self.written += 1
                except Exception as error:
                    batch_failed = True
                    with self.condition:
                        self.failures += 1
                        self.last_error = type(error).__name__
                    logging.getLogger(__name__).warning(
                        json.dumps(
                            {
                                "event": "worker_telemetry_write_failed",
                                "kind": kind,
                                "error_type": type(error).__name__,
                            }
                        )
                    )
                finally:
                    with self.condition:
                        self.in_flight -= 1
            with self.condition:
                if not batch_failed:
                    self.last_error = None
                    if self.dropped == drops_before_batch:
                        self.last_batch_dropped = False
                self.condition.notify_all()
            next_flush = time.monotonic() + self.interval

    def close(self, timeout=5):
        """Drain current observations; bound waiting, not a native SQL call.

        A timed-out daemon may finish its one in-flight call, then exits without
        starting another. Its unfinished drain is explicit in metrics and logs.
        """
        with self.condition:
            self.closing = True
            self.condition.notify_all()
        self.thread.join(timeout=timeout)
        if self.thread.is_alive():
            with self.condition:
                self.abandoned = True
                self.dropped += len(self.pending)
                self.pending.clear()
                self.condition.notify_all()
            logging.getLogger(__name__).warning('{"event":"worker_telemetry_shutdown_incomplete"}')
            return False
        return True

"""Validated shared process configuration. Never loads a real .env implicitly."""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str | None = None
    environment: str = "evaluation"
    auth_mode: str = "supabase"
    public_url: str = "http://127.0.0.1:8000"
    https: bool = False
    supabase_url: str = ""
    supabase_anon_key: str = ""
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    telegram_webhook_secret: str = ""
    camera_networks: str = ""
    inference_fps: float = 4
    max_inference_queue: int = 4
    model_device: str = "cpu"
    detection_threshold: float = 0.7
    incident_cooldown_seconds: float = 60
    model_manifest: Path | None = None
    model_rights_confirmed: bool = False
    max_cameras: int = 4
    max_rss_mb: int = 2048
    window_seconds: float = 5
    window_stride: float = 2.5
    confirmations: int = 2
    evidence_pre_seconds: float = 5
    evidence_post_seconds: float = 5
    retention_days: int = 30
    session_seconds: int = 28800
    demo_mode: str | None = None
    runtime_mode: str = "standard"

    def __post_init__(self):
        if self.runtime_mode == "standard":
            object.__setattr__(
                self, "runtime_mode", "offline_demo" if self.demo_mode == "offline" else self.environment
            )
        if self.runtime_mode not in {"offline_demo", "online_demo", "evaluation", "production"}:
            raise ValueError("DORMGUARD_MODE is invalid")
        if self.runtime_mode == "offline_demo" and self.demo_mode != "offline":
            raise ValueError("offline_demo requires the isolated offline launcher")
        if self.runtime_mode == "online_demo" and (
            self.environment != "evaluation"
            or self.demo_mode is not None
            or not self.https
            or urlsplit(self.public_url).scheme != "https"
        ):
            raise ValueError(
                "online_demo requires HTTPS, secure cookies, evaluation mode and no offline demo entry"
            )
        if self.runtime_mode == "production" and self.environment != "production":
            raise ValueError("Production runtime requires production environment")
        object.__setattr__(self, "data_dir", Path(self.data_dir).expanduser().resolve())
        if self.model_manifest:
            object.__setattr__(self, "model_manifest", Path(self.model_manifest).expanduser().resolve())
        if self.environment not in {"evaluation", "production"}:
            raise ValueError("DORMGUARD_ENV must be evaluation or production")
        if self.auth_mode not in {"local", "supabase"}:
            raise ValueError("DORMGUARD_AUTH must be local or supabase")
        if self.demo_mode is not None and (
            self.demo_mode not in {"fixture", "inference", "offline"}
            or self.environment != "evaluation"
            or self.auth_mode != "local"
            or self.database_url
            or self.telegram_bot_token
        ):
            raise ValueError("Demo mode requires local evaluation authentication and isolated SQLite storage")
        origin = urlsplit(self.public_url)
        if self.demo_mode == "offline" and (
            origin.hostname not in {"127.0.0.1", "localhost", "::1"}
            or self.supabase_url
            or self.supabase_anon_key
            or self.telegram_bot_username
            or self.telegram_webhook_secret
            or self.camera_networks
        ):
            raise ValueError(
                "Offline demo requires loopback origin and no external provider or camera configuration"
            )
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
        ):
            raise ValueError("DORMGUARD_PUBLIC_URL must be an HTTP(S) origin")
        if self.environment == "production":
            if (
                self.auth_mode != "supabase"
                or not self.database_url
                or not self.https
                or origin.scheme != "https"
            ):
                raise ValueError("Production requires Supabase Auth, PostgreSQL, HTTPS and secure cookies")
        if self.supabase_url:
            provider = urlsplit(self.supabase_url)
            if (
                provider.scheme != "https"
                or not provider.hostname
                or provider.username
                or provider.password
                or provider.query
                or provider.fragment
                or provider.path not in {"", "/"}
            ):
                raise ValueError("SUPABASE_URL must be an HTTPS origin")
        bounds = [
            (self.inference_fps, 0.2, 15, "INFERENCE_FPS"),
            (self.max_inference_queue, 1, 16, "MAX_INFERENCE_QUEUE"),
            (self.max_cameras, 1, 8, "DORMGUARD_MAX_CAMERAS"),
            (self.detection_threshold, 0.01, 0.99, "DETECTION_THRESHOLD"),
            (self.incident_cooldown_seconds, 1, 3600, "INCIDENT_COOLDOWN_SECONDS"),
            (self.window_seconds, 1, 30, "DORMGUARD_WINDOW_SECONDS"),
            (self.window_stride, 0.2, 30, "DORMGUARD_WINDOW_STRIDE"),
            (self.confirmations, 2, 3, "DORMGUARD_CONFIRMATIONS"),
            (self.evidence_pre_seconds, 0, 30, "DORMGUARD_EVIDENCE_PRE_SECONDS"),
            (self.evidence_post_seconds, 0, 30, "DORMGUARD_EVIDENCE_POST_SECONDS"),
            (self.max_rss_mb, 256, 65536, "DORMGUARD_MAX_RSS_MB"),
            (self.retention_days, 1, 3650, "DORMGUARD_RETENTION_DAYS"),
            (self.session_seconds, 60, 28800, "DORMGUARD_SESSION_SECONDS"),
        ]
        import math

        for value, lower, upper, name in bounds:
            if not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError(f"{name} out of supported range")
        if self.model_device not in {"cpu", "cuda", "auto"}:
            raise ValueError("MODEL_DEVICE must be cpu, cuda or auto")


def load_settings() -> Settings:
    env = os.environ
    return Settings(
        data_dir=Path(env.get("DORMGUARD_DATA", "data")),
        database_url=env.get("DORMGUARD_DATABASE_URL") or None,
        environment=env.get("DORMGUARD_ENV", "evaluation"),
        auth_mode=env.get("DORMGUARD_AUTH", "supabase"),
        public_url=env.get("DORMGUARD_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/"),
        https=env.get("DORMGUARD_HTTPS", "0") == "1",
        supabase_url=env.get("SUPABASE_URL", "").rstrip("/"),
        supabase_anon_key=env.get("SUPABASE_ANON_KEY", ""),
        telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_bot_username=env.get("TELEGRAM_BOT_USERNAME", ""),
        telegram_webhook_secret=env.get("TELEGRAM_WEBHOOK_SECRET", ""),
        camera_networks=env.get("DORMGUARD_CAMERA_NETWORKS", ""),
        inference_fps=float(env.get("INFERENCE_FPS", "4")),
        max_inference_queue=int(env.get("MAX_INFERENCE_QUEUE", "4")),
        model_device=env.get("MODEL_DEVICE", "cpu"),
        detection_threshold=float(env.get("DETECTION_THRESHOLD", "0.7")),
        incident_cooldown_seconds=float(env.get("INCIDENT_COOLDOWN_SECONDS", "60")),
        model_manifest=Path(env["MODEL_MANIFEST"]) if env.get("MODEL_MANIFEST") else None,
        model_rights_confirmed=env.get("DORMGUARD_MODEL_RIGHTS_CONFIRMED", "0") == "1",
        max_cameras=int(env.get("DORMGUARD_MAX_CAMERAS", "4")),
        max_rss_mb=int(env.get("DORMGUARD_MAX_RSS_MB", "2048")),
        window_seconds=float(env.get("DORMGUARD_WINDOW_SECONDS", "5")),
        window_stride=float(env.get("DORMGUARD_WINDOW_STRIDE", "2.5")),
        confirmations=int(env.get("DORMGUARD_CONFIRMATIONS", "2")),
        evidence_pre_seconds=float(env.get("DORMGUARD_EVIDENCE_PRE_SECONDS", "5")),
        evidence_post_seconds=float(env.get("DORMGUARD_EVIDENCE_POST_SECONDS", "5")),
        retention_days=int(env.get("DORMGUARD_RETENTION_DAYS", "30")),
        session_seconds=int(env.get("DORMGUARD_SESSION_SECONDS", "28800")),
        demo_mode=env.get("DORMGUARD_DEMO_MODE") or None,
        runtime_mode=env.get("DORMGUARD_MODE", "standard"),
    )

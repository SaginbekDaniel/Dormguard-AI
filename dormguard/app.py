"""Same-origin FastAPI transport. Public clients never receive provider or camera secrets."""

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import notifications
from .config import Settings, load_settings
from .database import DatabaseUnavailable
from .playback import MediaError, browser_derivative
from .security import CAMERA_MANAGER_ROLES, OFFICIAL_ROLES, token_hash, verify_password
from .store import DomainError, Store
from .supabase_auth import SupabaseAuth
from .transport import RemoteError

COOKIE = "dormguard_session"
PREFIX = "/api/v1"


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class CameraBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    location: str = Field(min_length=1, max_length=200)
    kind: str
    source_ref: str = Field(pattern=r"^DORMGUARD_CAMERA_[A-Z0-9_]{1,64}$")


class CameraPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=100)
    location: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    source_ref: str | None = Field(default=None, pattern=r"^DORMGUARD_CAMERA_[A-Z0-9_]{1,64}$")


class ReasonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=2000)


class TelegramTestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = False


class ProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=80)


class AccountBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


class DemoResetBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirm: bool


class RateLimiter:
    """Bounded process-local login limiter; reverse-proxy limits are also required."""

    def __init__(self):
        self.buckets = OrderedDict()
        self.lock = threading.Lock()

    def check(self, key, count=10, window=60):
        now = time.monotonic()
        with self.lock:
            times = [stamp for stamp in self.buckets.pop(key, []) if stamp > now - window]
            if len(times) >= count:
                self.buckets[key] = times
                raise HTTPException(429, "Too many attempts. Try again later.")
            self.buckets[key] = times + [now]
            while len(self.buckets) > 2048:
                self.buckets.popitem(last=False)


class BodyLimitMiddleware:
    """Enforce the actual streamed byte count, including chunked JSON requests."""

    def __init__(self, app, maximum=1024 * 1024):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.maximum:
                    raise HTTPException(413, "Request body too large")
            return message

        await self.app(scope, bounded_receive, send)


def create_app(settings: Settings | None = None, store=None, provider=None):
    settings = settings or load_settings()
    owns_store = store is None
    store = store or Store(settings.data_dir, settings.database_url)
    provider = provider or SupabaseAuth(settings)
    if settings.demo_mode == "offline":
        from .offline import demo_identity

        demo_identity(settings, store)

    @asynccontextmanager
    async def lifespan(application):
        try:
            yield
        finally:
            if owns_store:
                store.db.close()

    app = FastAPI(title="DormGuard AI", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(BodyLimitMiddleware)
    app.state.settings, app.state.store, app.state.provider = settings, store, provider
    limiter = RateLimiter()
    assets = Path(__file__).resolve().parent

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(MediaError)
    async def media_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(DatabaseUnavailable)
    async def database_unavailable(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(RemoteError)
    async def provider_error(request, exc):
        code = 401 if exc.status in (400, 401, 403, 422) else 503
        return JSONResponse(
            {
                "detail": "Identity provider rejected authentication"
                if code == 401
                else "Identity provider unavailable"
            },
            status_code=code,
        )

    async def audit_denial(request, response):
        if response.status_code not in {401, 403, 429} or not request.url.path.startswith(PREFIX + "/"):
            return response
        # Canonical route templates exclude query tickets and attacker-controlled
        # path values. A failed password attempt never establishes an actor.
        route = getattr(request.scope.get("route"), "path", PREFIX + "/unmatched")
        actor = getattr(request.state, "audit_actor_id", None)
        action = "auth.denied" if route == PREFIX + "/auth/login" else "authorization.denied"
        try:
            await asyncio.to_thread(
                store.audit,
                actor,
                action,
                route,
                {
                    "method": request.method,
                    "status": response.status_code,
                    "result": "denied",
                    "actor_basis": "local_session" if actor else "unknown",
                },
            )
        except Exception as error:
            # Returning the original denial preserves fail-closed behavior even
            # when SQL is unavailable. Diagnostic values never contain SQL/URLs.
            logging.getLogger(__name__).warning(
                json.dumps({"event": "security_audit_unavailable", "error_type": type(error).__name__})
            )
        return response

    @app.middleware("http")
    async def security_headers(request, call_next):
        if settings.demo_mode == "offline" and (
            request.url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not request.client
            or request.client.host not in {"127.0.0.1", "::1"}
        ):
            return JSONResponse({"detail": "Offline demo is loopback only"}, status_code=403)
        content_length = request.headers.get("content-length", "0")
        try:
            if int(content_length) > 1024 * 1024:
                return JSONResponse({"detail": "Request body too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "Invalid content length"}, status_code=400)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != settings.public_url.rstrip("/"):
                return await audit_denial(
                    request, JSONResponse({"detail": "Cross-origin writes are forbidden"}, status_code=403)
                )
            if request.headers.get("sec-fetch-site") == "cross-site":
                return await audit_denial(
                    request, JSONResponse({"detail": "Cross-site writes are forbidden"}, status_code=403)
                )
        if (
            (settings.environment == "production" or settings.runtime_mode == "online_demo")
            and request.url.hostname != urlsplit(settings.public_url).hostname
            and not (
                request.url.path in {"/health", "/healthz", "/ready"}
                and request.url.hostname in {"127.0.0.1", "localhost", "::1"}
                and request.client
                and request.client.host in {"127.0.0.1", "::1"}
            )
        ):
            return JSONResponse({"detail": "Invalid application host"}, status_code=400)
        response = await call_next(request)
        response = await audit_denial(request, response)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        if settings.https:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def session(request, *, remote=True, write=False, director=False, administrator=False):
        token = request.cookies.get(COOKIE, "")
        if not 30 <= len(token) <= 128:
            raise HTTPException(401, "Sign in to continue")
        hashed = token_hash(token)
        with store.db.read() as conn:
            row = conn.execute(
                "SELECT s.*,u.id,u.email,u.role,u.enabled,u.display_name FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?",
                (hashed, time.time()),
            ).fetchone()
        if not row or not row["enabled"] or row["role"] not in OFFICIAL_ROLES:
            raise HTTPException(401, "Session expired or account unavailable")
        row = dict(row)
        request.state.audit_actor_id = row["user_id"]
        if write and not hmac.compare_digest(row["csrf_token"], request.headers.get("x-csrf-token", "")):
            raise HTTPException(403, "Valid CSRF token required")
        if director and row["role"] not in CAMERA_MANAGER_ROLES:
            raise HTTPException(403, "Dormitory director permission required")
        if administrator and row["role"] != "super_admin":
            raise HTTPException(403, "Super administrator permission required")
        if remote and settings.auth_mode == "supabase":
            if not row["provider_token"]:
                raise HTTPException(401, "Provider session unavailable")
            identity = provider.validate(row["provider_token"])
            if identity.get("id") != row["user_id"]:
                raise HTTPException(401, "Provider identity does not match trusted assignment")
        return row

    def public_user(row):
        return {key: row[key] for key in ("id", "email", "role", "display_name", "csrf_token")}

    def public_camera(row):
        if not row:
            raise HTTPException(404, "Camera not found")
        row["enabled"] = bool(row["enabled"])
        last = row.get("last_frame_at")
        state = row.get("state") or "unconfigured"
        stale = last is None or time.time() - last > 10
        if state == "connected" and stale:
            state = "stale"
        row["health"] = {
            "state": state,
            "last_frame_at": last,
            "detail": row.get("detail") or "Waiting for camera ingest",
            "stale": stale,
        }
        row["state"] = state
        return row

    @app.get("/")
    def shell():
        return FileResponse(assets / "index.html", media_type="text/html")

    @app.get("/assets/{name}")
    def asset(name: str):
        if name not in {"app.js", "style.css"}:
            raise HTTPException(404, "Asset not found")
        return FileResponse(
            assets / name, media_type="text/javascript" if name.endswith(".js") else "text/css"
        )

    @app.get("/healthz")
    @app.get("/health")
    def liveness():
        return {"status": "alive", "scope": "API liveness only"}

    @app.get("/ready")
    def readiness():
        try:
            with store.db.read() as conn:
                conn.execute("SELECT 1").fetchone()
            workers = store.health()
            model = next((row for row in workers if row["name"] == "inference"), None)
            model_ready = bool(
                model and not model["stale"] and model["state"] == "ready" and model["metrics"].get("model")
            )
            active = any(
                camera["enabled"]
                and camera.get("state") == "connected"
                and camera.get("last_frame_at")
                and time.time() - camera["last_frame_at"] < 10
                for camera in store.cameras()
            )
            result = {
                "status": "ready" if model_ready else "blocked",
                "database": "ready",
                "model_ready": model_ready,
                "worker_fresh": bool(model and not model["stale"]),
                "video_source_active": bool(active),
                "runtime_mode": settings.runtime_mode,
                "scope": "Database/model/worker readiness; active video processing is reported separately",
            }
            return JSONResponse(result, status_code=200 if model_ready else 503)
        except Exception as error:
            logging.getLogger("dormguard").warning(
                "readiness_unavailable error_type=%s", type(error).__name__
            )
            return JSONResponse(
                {"status": "blocked", "database": "unavailable", "model_ready": False}, status_code=503
            )

    @app.get(PREFIX + "/auth/config")
    def auth_config():
        return {
            "mode": settings.auth_mode,
            "signup_enabled": False,
            "evaluation": settings.environment == "evaluation",
            "configured": settings.auth_mode == "local" or provider.configured,
            "demo_mode": settings.demo_mode,
            "runtime_mode": settings.runtime_mode,
        }

    @app.post(PREFIX + "/auth/login")
    def login(body: LoginBody, request: Request):
        email = body.email.strip().lower()
        address = request.client.host if request.client else "unknown"
        limiter.check("ip:" + token_hash(address), count=30)
        limiter.check("email:" + token_hash(email), count=8)
        provider_token = None
        expires = settings.session_seconds
        if settings.auth_mode == "supabase":
            result = provider.login(email, body.password)
            user = store.user(result["user"]["id"])
            provider_token = result["access_token"]
            # Provider-issued lifetime never extends the application session cap.
            try:
                expires = min(expires, int(result["expires_in"]))
                if result.get("expires_at") is not None:
                    expires = min(expires, int(result["expires_at"]) - int(time.time()))
            except (ValueError, TypeError, KeyError):
                raise HTTPException(503, "Invalid provider session lifetime") from None
            if expires <= 0:
                raise HTTPException(401, "Provider session expired")
        else:
            with store.db.read() as conn:
                row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            user = dict(row) if row else None
            if not verify_password(body.password, user.get("password_hash") if user else None):
                raise HTTPException(401, "Invalid credentials or unavailable account")
        if (
            not user
            or not user["enabled"]
            or user["role"] not in OFFICIAL_ROLES
            or user["email"].lower() != email
        ):
            raise HTTPException(401, "Invalid credentials or unavailable account")
        return issue_session(user, expires, provider_token)

    def issue_session(user, expires, provider_token=None):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with store.db.transaction() as conn:
            # Recheck availability under the same writer lock as session creation.
            store._actor(conn, user["id"])
            conn.execute("DELETE FROM sessions WHERE expires_at<?", (time.time(),))
            conn.execute(
                "INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at,provider_token) VALUES(?,?,?,?,?)",
                (token_hash(token), user["id"], csrf, time.time() + expires, provider_token),
            )
            store._audit(conn, user["id"], "auth.login", user["id"], {"mode": settings.auth_mode})
        response = JSONResponse(
            {
                "id": user["id"],
                "email": user["email"],
                "role": user["role"],
                "display_name": user["display_name"],
                "csrf_token": csrf,
            }
        )
        response.set_cookie(
            COOKIE, token, max_age=expires, httponly=True, secure=settings.https, samesite="strict", path="/"
        )
        return response

    @app.post(PREFIX + "/demo/enter")
    def demo_enter(request: Request):
        from .offline import demo_identity

        if settings.demo_mode != "offline":
            raise HTTPException(404, "Offline demo is not enabled")
        if request.headers.get("origin") != settings.public_url:
            raise HTTPException(403, "Same-origin demo entry required")
        limiter.check("demo-entry", count=30)
        return issue_session(demo_identity(settings, store), settings.session_seconds)

    @app.post(PREFIX + "/demo/reset")
    def demo_reset(body: DemoResetBody, request: Request):
        from .offline import request_reset

        if settings.demo_mode != "offline":
            raise HTTPException(404, "Offline demo is not enabled")
        user = session(request, write=True, director=True)
        if body.confirm is not True:
            raise HTTPException(422, "Explicit demo reset confirmation required")
        return request_reset(settings, store, user["id"])

    @app.get(PREFIX + "/auth/me")
    def me(request: Request):
        return public_user(session(request))

    @app.patch(PREFIX + "/auth/profile")
    def profile(body: ProfileBody, request: Request):
        user = session(request, write=True)
        store.update_profile(user["id"], body.display_name)
        return public_user(session(request, remote=False))

    @app.get(PREFIX + "/users")
    def users(request: Request):
        session(request, administrator=True)
        return {"items": store.list_users()}

    @app.patch(PREFIX + "/users/{user_id}")
    def account(user_id: str, body: AccountBody, request: Request):
        user = session(request, write=True, administrator=True)
        return store.set_account_availability(user["id"], user_id, body.enabled)

    @app.post(PREFIX + "/auth/logout")
    def logout(request: Request):
        # Local revocation deliberately precedes remote I/O, fixing historical K01 by construction.
        user = session(request, remote=False, write=True)
        with store.db.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (user["token_hash"],))
            conn.execute("DELETE FROM stream_tickets WHERE session_hash=?", (user["token_hash"],))
            store._audit(conn, user["id"], "auth.logout", user["id"])
        provider_status = "not_applicable"
        if settings.auth_mode == "supabase" and user["provider_token"]:
            try:
                provider.logout(user["provider_token"])
                provider_status = "requested"
            except RemoteError:
                provider_status = "unavailable_local_session_revoked"
        response = JSONResponse({"logged_out": True, "provider_signout": provider_status})
        response.delete_cookie(COOKIE, path="/", secure=settings.https, httponly=True, samesite="strict")
        return response

    @app.get(PREFIX + "/cameras")
    def cameras(request: Request):
        session(request)
        return {"items": [public_camera(item) for item in store.cameras()]}

    @app.get(PREFIX + "/cameras/{camera_id}")
    def camera(camera_id: str, request: Request):
        session(request)
        return public_camera(store.camera(camera_id))

    @app.post(PREFIX + "/cameras", status_code=201)
    def register_camera(body: CameraBody, request: Request):
        user = session(request, write=True, director=True)
        if body.kind not in {"rtsp", "test"}:
            raise HTTPException(422, "Camera kind must be rtsp or test")
        return public_camera(
            store.create_camera(
                body.name,
                body.location,
                body.kind,
                body.source_ref,
                user["id"],
                max_cameras=settings.max_cameras,
            )
        )

    @app.patch(PREFIX + "/cameras/{camera_id}")
    def change_camera(camera_id: str, body: CameraPatch, request: Request):
        user = session(request, write=True, director=True)
        return public_camera(
            store.update_camera(
                camera_id, body.model_dump(exclude_none=True), user["id"], max_cameras=settings.max_cameras
            )
        )

    @app.delete(PREFIX + "/cameras/{camera_id}")
    def deactivate_camera(camera_id: str, request: Request):
        user = session(request, write=True, director=True)
        return public_camera(store.delete_camera(camera_id, user["id"]))

    @app.get(PREFIX + "/incidents")
    def incidents(
        request: Request,
        limit: int = 100,
        status: str | None = None,
        camera_id: str | None = None,
        offset: int = 0,
        q: str = "",
        from_date: str | None = None,
        to_date: str | None = None,
        sort: str = "newest",
        min_score: float | None = None,
    ):
        session(request)
        return store.incident_page(
            max(1, min(limit, 500)),
            status,
            camera_id,
            offset=offset,
            q=q,
            from_date=from_date,
            to_date=to_date,
            sort=sort,
            min_score=min_score,
        )

    @app.get(PREFIX + "/incidents/{incident_id}")
    def incident(incident_id: str, request: Request):
        user = session(request)
        result = store.incident(incident_id)
        if not result:
            raise HTTPException(404, "Incident not found")
        with store.db.read() as conn:
            delivery_scope = "" if user["role"] == "super_admin" else " AND o.user_id=?"
            delivery_params = (incident_id,) if user["role"] == "super_admin" else (incident_id, user["id"])
            result["notifications"] = store._all(
                conn,
                "SELECT o.id,o.incident_id,o.user_id,o.state,o.attempts,o.last_error,o.created_at,o.next_attempt,u.role AS recipient_role FROM notification_outbox o JOIN users u ON u.id=o.user_id WHERE o.incident_id=?"
                + delivery_scope
                + " ORDER BY o.created_at,o.id",
                delivery_params,
            )
            for delivery in result["notifications"]:
                delivery["can_retry"] = delivery.pop("user_id") == user["id"]
                delivery["history"] = store._all(
                    conn,
                    "SELECT state,detail,created_at FROM notification_attempts WHERE outbox_id=? ORDER BY created_at,id",
                    (delivery["id"],),
                )
            result["audit_events"] = store._all(
                conn,
                "SELECT action,actor_id,resource_id,detail,created_at FROM audit_logs WHERE resource_id=? OR resource_id IN (SELECT id FROM notification_outbox WHERE incident_id=?) OR resource_id IN (SELECT id FROM evidence WHERE incident_id=?) ORDER BY created_at DESC,id DESC LIMIT 100",
                (incident_id, incident_id, incident_id),
            )
        for event in result["audit_events"]:
            event["detail"] = json.loads(event["detail"])
        return result

    @app.post(PREFIX + "/incidents/{incident_id}/{action}")
    def transition(incident_id: str, action: str, body: ReasonBody, request: Request):
        user = session(request, write=True)
        if action == "opened":
            return store.record_incident_opened(incident_id, user["id"])
        if action == "notes":
            return store.add_note(incident_id, user["id"], body.reason)
        return store.transition(incident_id, action, user["id"], body.reason)

    @app.get(PREFIX + "/evidence")
    def evidence(request: Request):
        session(request)
        return {"items": store.evidence_list(), "retention_days": settings.retention_days}

    @app.post(PREFIX + "/evidence/{evidence_id}/verify")
    def verify(evidence_id: str, request: Request):
        user = session(request, write=True)
        return store.verify_evidence(evidence_id, user["id"])

    def checked_evidence(evidence_id, user):
        result = store.verify_evidence(evidence_id, user["id"])
        if not result["valid"]:
            raise HTTPException(409, "Evidence unavailable or integrity check failed")
        return store.evidence(evidence_id, private=True)

    @app.get(PREFIX + "/evidence/{evidence_id}/video")
    def evidence_video(evidence_id: str, request: Request):
        user = session(request)
        item = checked_evidence(evidence_id, user)
        path = browser_derivative(store, item)
        store.audit(user["id"], "evidence.playback", evidence_id)
        return FileResponse(path, media_type="video/mp4")

    @app.get(PREFIX + "/evidence/{evidence_id}/download")
    def evidence_download(evidence_id: str, request: Request):
        user = session(request)
        item = checked_evidence(evidence_id, user)
        store.audit(user["id"], "evidence.download", evidence_id)
        path = store.evidence_path(item)
        return FileResponse(
            path, media_type="application/octet-stream", filename="evidence-" + item["id"] + path.suffix
        )

    @app.get(PREFIX + "/overview")
    def overview(request: Request):
        session(request)
        result = store.overview()
        result["cameras_total"] = result["total_cameras"]
        result["cameras_active"] = result["connected_cameras"]
        result["configured"] = bool(result["total_cameras"])
        return result

    @app.get(PREFIX + "/system")
    def system(request: Request):
        session(request)
        return {
            "workers": store.health(),
            "operations": store.operational_summary(),
            "auth_mode": settings.auth_mode,
            "evaluation": settings.environment == "evaluation",
            "telegram_configured": bool(settings.telegram_bot_token and settings.telegram_webhook_secret),
            "configuration": {
                key: getattr(settings, key)
                for key in (
                    "inference_fps",
                    "max_inference_queue",
                    "model_device",
                    "detection_threshold",
                    "incident_cooldown_seconds",
                    "model_rights_confirmed",
                    "retention_days",
                    "max_cameras",
                )
            }
            | {"model_configured": bool(settings.model_manifest and settings.model_manifest.is_file())},
        }

    @app.get(PREFIX + "/audit")
    def audit(
        request: Request,
        limit: int = 50,
        offset: int = 0,
        q: str = "",
        category: str = "",
        from_date: str | None = None,
        to_date: str | None = None,
        verify: bool = True,
    ):
        session(request)
        return store.audit_page(
            max(1, min(limit, 500)),
            offset=offset,
            q=q,
            category=category,
            from_date=from_date,
            to_date=to_date,
        ) | {
            "integrity": store.verify_audit()
            if verify
            else {"valid": None, "scope": "Verification not requested"}
        }

    @app.get(PREFIX + "/integrations")
    def integrations(request: Request):
        user = session(request)
        with store.db.read() as conn:
            binding = conn.execute(
                "SELECT created_at FROM telegram_bindings WHERE user_id=?", (user["id"],)
            ).fetchone()
        return {
            "telegram": {
                "configured": bool(settings.telegram_bot_token and settings.telegram_webhook_secret),
                "linked": binding is not None,
                "linked_at": binding["created_at"] if binding else None,
                "bot_username": settings.telegram_bot_username,
            },
            "auth_mode": settings.auth_mode,
        }

    @app.post(PREFIX + "/integrations/telegram/link")
    def telegram_link(request: Request):
        user = session(request, write=True)
        limiter.check("link:" + user["id"], count=5)
        return notifications.create_link(store, user["id"], settings)

    @app.delete(PREFIX + "/integrations/telegram/link")
    def telegram_unlink(request: Request):
        user = session(request, write=True)
        notifications.unlink(store, user["id"])
        return {"linked": False}

    @app.post(PREFIX + "/integrations/telegram/test")
    def telegram_test(body: TelegramTestBody, request: Request):
        user = session(request, write=True)
        if body.confirm is not True:
            raise HTTPException(422, "Explicit test-send confirmation required")
        limiter.check("telegram-test:" + user["id"], count=3)
        return notifications.send_account_test(store, user["id"], settings)

    @app.post(PREFIX + "/telegram/webhook")
    async def telegram_webhook(request: Request):
        # Read at most 64 KiB even when Content-Length is absent (chunked requests).
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > 65536:
                raise HTTPException(413, "Webhook payload too large")
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            raise HTTPException(422, "Invalid webhook payload") from None
        return notifications.bind_webhook(
            store, settings, request.headers.get("x-telegram-bot-api-secret-token"), payload
        )

    @app.get(PREFIX + "/notifications")
    def notification_list(request: Request):
        user = session(request)
        with store.db.read() as conn:
            items = [
                dict(row)
                for row in conn.execute(
                    "SELECT id,incident_id,state,attempts,last_error,provider_id,created_at,next_attempt FROM notification_outbox WHERE user_id=? ORDER BY created_at DESC LIMIT 100",
                    (user["id"],),
                ).fetchall()
            ]
        return {"items": items}

    @app.get(PREFIX + "/alerts")
    def local_alerts(request: Request):
        user = session(request)
        with store.db.read() as conn:
            rows = store._all(
                conn,
                "SELECT o.id,o.incident_id,o.created_at,i.status,i.detected_at,c.name AS camera_name,"
                "CASE WHEN EXISTS (SELECT 1 FROM audit_logs a WHERE a.resource_id=o.id AND a.actor_id=o.user_id "
                "AND a.action='notification.read') THEN 1 ELSE 0 END AS is_read "
                "FROM notification_outbox o JOIN incidents i ON i.id=o.incident_id "
                "JOIN cameras c ON c.id=i.camera_id WHERE o.user_id=? ORDER BY o.created_at DESC,o.id DESC LIMIT 100",
                (user["id"],),
            )
        return {"items": rows, "unread": sum(not row["is_read"] for row in rows), "channel": "local"}

    @app.post(PREFIX + "/alerts/{alert_id}/read")
    def read_alert(alert_id: str, request: Request):
        user = session(request, write=True)
        with store.db.transaction() as conn:
            store._actor(conn, user["id"])
            if not conn.execute(
                "SELECT id FROM notification_outbox WHERE id=? AND user_id=?", (alert_id, user["id"])
            ).fetchone():
                raise HTTPException(404, "Alert not found")
            if not conn.execute(
                "SELECT id FROM audit_logs WHERE actor_id=? AND resource_id=? AND action='notification.read'",
                (user["id"], alert_id),
            ).fetchone():
                store._audit(conn, user["id"], "notification.read", alert_id)
        return {"is_read": True}

    @app.post(PREFIX + "/notifications/{outbox_id}/retry")
    def retry(outbox_id: str, request: Request):
        user = session(request, write=True)
        with store.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM notification_outbox WHERE id=? AND user_id=?", (outbox_id, user["id"])
            ).fetchone()
            if not row:
                raise HTTPException(404, "Delivery not found")
            if row["state"] not in {"blocked", "failed"}:
                raise HTTPException(409, "Only failed or blocked deliveries can be retried")
            conn.execute(
                "UPDATE notification_outbox SET state='queued',attempts=0,next_attempt=?,last_error=NULL WHERE id=?",
                (time.time(), outbox_id),
            )
            store._audit(conn, user["id"], "notification.retry", outbox_id)
        return {"state": "queued"}

    @app.post(PREFIX + "/cameras/{camera_id}/stream-session")
    def stream_session(camera_id: str, request: Request):
        user = session(request, write=True)
        camera = store.camera(camera_id)
        if not camera or not camera["enabled"]:
            raise HTTPException(404, "Enabled camera not found")
        token = secrets.token_urlsafe(32)
        expires = time.time() + 60
        with store.db.transaction() as conn:
            conn.execute("DELETE FROM stream_tickets WHERE expires_at<?", (time.time(),))
            conn.execute(
                "INSERT INTO stream_tickets(token_hash,user_id,camera_id,session_hash,expires_at) VALUES(?,?,?,?,?)",
                (token_hash(token), user["id"], camera_id, user["token_hash"], expires),
            )
        return {"url": PREFIX + "/streams/" + token, "expires_at": expires}

    @app.get(PREFIX + "/streams/{ticket}")
    def stream(ticket: str, request: Request):
        user = session(request)
        with store.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM stream_tickets WHERE token_hash=? AND user_id=? AND session_hash=? AND expires_at>?",
                (token_hash(ticket), user["id"], user["token_hash"], time.time()),
            ).fetchone()
            if not row:
                raise HTTPException(403, "Stream ticket invalid, expired or already used")
            row = dict(row)
            conn.execute("DELETE FROM stream_tickets WHERE token_hash=?", (token_hash(ticket),))
        path = settings.data_dir / "frames" / (row["camera_id"] + ".jpg")
        if not path.is_file() or time.time() - path.stat().st_mtime > 10:
            raise HTTPException(503, "Camera preview unavailable; check camera worker")
        store.audit(user["id"], "camera.stream_opened", row["camera_id"])

        async def frames():
            deadline = time.monotonic() + 300
            last_remote = 0
            while time.monotonic() < deadline and not await request.is_disconnected():
                try:
                    remote = time.monotonic() - last_remote >= 5
                    await asyncio.to_thread(session, request, remote=remote)
                    if remote:
                        last_remote = time.monotonic()
                    camera = await asyncio.to_thread(store.camera, row["camera_id"])
                    if (
                        not camera
                        or not camera["enabled"]
                        or not path.is_file()
                        or time.time() - path.stat().st_mtime > 10
                    ):
                        break
                    data = path.read_bytes()
                    if len(data) > 2 * 1024 * 1024:
                        break
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + data + b"\r\n"
                except (HTTPException, RemoteError, OSError):
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")

    def event_revision_snapshot():
        with store.db.read() as conn:
            latest = conn.execute("SELECT COUNT(*) AS n,MAX(created_at) AS stamp FROM audit_logs").fetchone()
            health = conn.execute("SELECT MAX(updated_at) AS stamp FROM camera_health").fetchone()
            workers = conn.execute("SELECT MAX(updated_at) AS stamp FROM worker_health").fetchone()
        return latest["n"], latest["stamp"], health["stamp"], workers["stamp"]

    @app.get(PREFIX + "/events")
    async def events(request: Request):
        await asyncio.to_thread(session, request)

        async def changes():
            previous = None
            while not await request.is_disconnected():
                try:
                    await asyncio.to_thread(session, request)
                    count, latest, health, workers = await asyncio.to_thread(event_revision_snapshot)
                    revision = hashlib.sha256(
                        json.dumps(
                            [{"n": count, "stamp": latest}, {"stamp": health}, {"stamp": workers}],
                            sort_keys=True,
                        ).encode()
                    ).hexdigest()[:16]
                    if revision != previous:
                        yield "event: invalidate\ndata: " + json.dumps({"revision": revision}) + "\n\n"
                        previous = revision
                    else:
                        yield ": keepalive\n\n"
                except (HTTPException, RemoteError):
                    yield "event: auth-expired\ndata: {}\n\n"
                    break
                await asyncio.sleep(3)

        return StreamingResponse(
            changes(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    return app


def main():
    import uvicorn

    uvicorn.run("dormguard.app:create_app", factory=True, host="127.0.0.1", port=8000, access_log=False)


if __name__ == "__main__":
    main()

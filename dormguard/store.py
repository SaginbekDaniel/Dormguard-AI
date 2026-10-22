"""Transactional domain storage. Scores are uncalibrated suspicion signals.

Trusted server callers read data after HTTP authorization. Human mutations also
check the enabled SQL role here; worker creation/evidence APIs are internal.
"""

import hashlib
import json
import math
import os
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .database import Database
from .security import CAMERA_MANAGER_ROLES, OFFICIAL_ROLES, ROLE_ALIASES


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def _id():
    return str(uuid.uuid4())


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class DomainError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class Store:
    def __init__(self, data_dir: Path, database_url=None):
        self.data_dir = Path(data_dir).resolve()
        self.db = Database(self.data_dir, database_url)
        self.evidence_dir = self.data_dir / "evidence"
        if self.evidence_dir.is_symlink():
            raise DomainError("Evidence directory must not be a symlink", 409)
        self.evidence_dir.mkdir(exist_ok=True, mode=0o700)
        os.chmod(self.evidence_dir, 0o700)

    def init(self):
        self.db.initialize()
        return self

    @staticmethod
    def _one(conn, sql, params=()):
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _all(conn, sql, params=()):
        return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def _actor(self, conn, actor_id, roles=OFFICIAL_ROLES):
        actor = self._one(conn, "SELECT * FROM users WHERE id=?", (actor_id,))
        if not actor or not actor["enabled"] or actor["role"] not in roles:
            raise DomainError("Enabled authorized official required", 403)
        return actor

    @staticmethod
    def _display_name(value):
        name = value.strip()
        if not name or len(name) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise DomainError("Display name must contain 1–80 characters without control characters", 422)
        return name

    def provision_user(self, email, role, password_hash=None, user_id=None, display_name=None):
        role = ROLE_ALIASES.get(role, role)
        if role not in OFFICIAL_ROLES | {"technical_admin"}:
            raise DomainError("Invalid role", 422)
        email = email.strip().lower()
        if not email or "@" not in email:
            raise DomainError("Valid email required", 422)
        identity = user_id or _id()
        name = self._display_name(display_name) if display_name is not None else ""
        with self.db.transaction() as conn:
            if self._one(
                conn, "SELECT id FROM users WHERE email=? OR role=? OR id=?", (email, role, identity)
            ):
                raise DomainError("Email, identity or role already provisioned")
            conn.execute(
                "INSERT INTO users(id,email,role,password_hash,enabled,created_at,display_name) VALUES(?,?,?,?,1,?,?)",
                (identity, email, role, password_hash, utcnow(), name),
            )
            self._audit(conn, None, "user.provisioned", identity, {"role": role})
        return self.user(identity)

    def user(self, user_id):
        with self.db.read() as conn:
            return self._one(conn, "SELECT * FROM users WHERE id=?", (user_id,))

    def list_users(self):
        with self.db.read() as conn:
            return self._all(
                conn, "SELECT id,email,role,enabled,display_name,created_at FROM users ORDER BY role"
            )

    def update_profile(self, actor_id, display_name):
        name = self._display_name(display_name)
        with self.db.transaction() as conn:
            self._actor(conn, actor_id)
            conn.execute("UPDATE users SET display_name=? WHERE id=?", (name, actor_id))
            self._audit(conn, actor_id, "profile.updated", actor_id, {"field": "display_name"})
        return self.user(actor_id)

    def set_account_availability(self, actor_id, user_id, enabled):
        with self.db.transaction() as conn:
            self._actor(conn, actor_id, ("super_admin",))
            if not self._one(conn, "SELECT id FROM users WHERE id=?", (user_id,)):
                raise DomainError("Account not found", 404)
            if actor_id == user_id and not enabled:
                raise DomainError(
                    "Use trusted operator provisioning to disable your own administrator account", 409
                )
            conn.execute("UPDATE users SET enabled=? WHERE id=?", (int(enabled), user_id))
            # Re-enabling never restores previous credentials or media tickets.
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM stream_tickets WHERE user_id=?", (user_id,))
            self._audit(conn, actor_id, "account.availability", user_id, {"enabled": bool(enabled)})
        return next(user for user in self.list_users() if user["id"] == user_id)

    @staticmethod
    def _public_camera(camera, private):
        if camera and not private:
            camera.pop("source_ref", None)
        return camera

    def camera(self, camera_id, private=False):
        with self.db.read() as conn:
            row = self._one(
                conn,
                "SELECT c.*,h.state,h.last_frame_at,h.detail,h.updated_at AS health_updated_at FROM cameras c LEFT JOIN camera_health h ON h.camera_id=c.id WHERE c.id=?",
                (camera_id,),
            )
        return self._public_camera(row, private)

    def cameras(self, private=False):
        with self.db.read() as conn:
            rows = self._all(
                conn,
                "SELECT c.*,h.state,h.last_frame_at,h.detail,h.updated_at AS health_updated_at FROM cameras c LEFT JOIN camera_health h ON h.camera_id=c.id ORDER BY c.created_at",
            )
        return [self._public_camera(row, private) for row in rows]

    @staticmethod
    def _camera_capacity(conn, max_cameras):
        if max_cameras is None:
            return
        if not isinstance(max_cameras, int) or max_cameras < 1:
            raise DomainError("Positive integer camera capacity required", 422)
        enabled = conn.execute("SELECT COUNT(*) AS n FROM cameras WHERE enabled=1").fetchone()["n"]
        if enabled >= max_cameras:
            raise DomainError("Configured camera capacity reached", 409)

    def create_camera(self, name, location, kind, source_ref, actor_id, max_cameras=None):
        if kind not in ("rtsp", "test") or not str(name).strip() or not str(source_ref).strip():
            raise DomainError("Name, valid kind and private source required", 422)
        camera_id = _id()
        with self.db.transaction() as conn:
            self._actor(conn, actor_id, CAMERA_MANAGER_ROLES)
            self._camera_capacity(conn, max_cameras)
            conn.execute(
                "INSERT INTO cameras(id,name,location,kind,source_ref,enabled,created_at) VALUES(?,?,?,?,?,1,?)",
                (camera_id, name.strip(), location.strip(), kind, source_ref, utcnow()),
            )
            conn.execute(
                "INSERT INTO camera_health(camera_id,state,detail,updated_at) VALUES(?,?,?,?)",
                (camera_id, "unconfigured", "Awaiting ingest worker", time.time()),
            )
            self._audit(conn, actor_id, "camera.created", camera_id, {"kind": kind})
        return self.camera(camera_id)

    def update_camera(self, camera_id, fields, actor_id, max_cameras=None):
        allowed = {"name", "location", "kind", "source_ref", "enabled"}
        if not fields or set(fields) - allowed:
            raise DomainError("Unsupported camera fields", 422)
        if "kind" in fields and fields["kind"] not in ("rtsp", "test"):
            raise DomainError("Invalid camera kind", 422)
        if "enabled" in fields and fields["enabled"] not in (0, 1, False, True):
            raise DomainError("Invalid enabled value", 422)
        if any(key in fields and not str(fields[key]).strip() for key in ("name", "source_ref")):
            raise DomainError("Camera name and source cannot be blank", 422)
        with self.db.transaction() as conn:
            self._actor(conn, actor_id, CAMERA_MANAGER_ROLES)
            current = self._one(conn, "SELECT id,enabled FROM cameras WHERE id=?", (camera_id,))
            if not current:
                raise DomainError("Camera not found", 404)
            if fields.get("enabled") and not current["enabled"]:
                self._camera_capacity(conn, max_cameras)
            keys = sorted(fields)
            conn.execute(
                "UPDATE cameras SET " + ",".join(key + "=?" for key in keys) + " WHERE id=?",
                tuple(int(fields[key]) if key == "enabled" else fields[key] for key in keys) + (camera_id,),
            )
            self._audit(conn, actor_id, "camera.updated", camera_id, {"fields": keys})
        return self.camera(camera_id)

    def delete_camera(self, camera_id, actor_id):
        # Deactivation preserves incident/evidence provenance and does not delete sources.
        return self.update_camera(camera_id, {"enabled": 0}, actor_id)

    def set_camera_health(self, camera_id, state, last_frame_at=None, detail=""):
        with self.db.transaction() as conn:
            previous = self._one(conn, "SELECT state FROM camera_health WHERE camera_id=?", (camera_id,))
            if not previous or previous["state"] != state:
                self._audit(
                    conn,
                    None,
                    "camera." + state,
                    camera_id,
                    {"previous_state": previous["state"] if previous else None, "state": state},
                )
            conn.execute(
                "INSERT INTO camera_health(camera_id,state,last_frame_at,detail,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(camera_id) DO UPDATE SET state=excluded.state,last_frame_at=excluded.last_frame_at,detail=excluded.detail,updated_at=excluded.updated_at",
                (camera_id, state, last_frame_at, detail, time.time()),
            )

    def heartbeat(self, name, state, detail="", metrics=None):
        with self.db.transaction() as conn:
            previous = self._one(conn, "SELECT state FROM worker_health WHERE name=?", (name,))
            if not previous or previous["state"] != state:
                self._audit(
                    conn,
                    None,
                    "worker.state_changed",
                    name,
                    {"previous_state": previous["state"] if previous else None, "state": state},
                )
            conn.execute(
                "INSERT INTO worker_health(name,state,detail,metrics,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET state=excluded.state,detail=excluded.detail,metrics=excluded.metrics,updated_at=excluded.updated_at",
                (name, state, detail, _json(metrics or {}), time.time()),
            )

    def health(self):
        with self.db.read() as conn:
            rows = self._all(conn, "SELECT * FROM worker_health ORDER BY name")
        for row in rows:
            row["metrics"] = json.loads(row["metrics"])
            row["stale"] = time.time() - row["updated_at"] > 30
            if "observed_at_utc" in row["metrics"]:
                try:
                    observed = datetime.fromisoformat(row["metrics"]["observed_at_utc"])
                    if observed.tzinfo is None:
                        raise ValueError("observation_timezone_required")
                    age = time.time() - observed.timestamp()
                    row["stale"] = row["stale"] or not -2 <= age <= 30
                except (TypeError, ValueError, OverflowError):
                    row["stale"] = True
        return rows

    def _event(self, conn, incident_id, kind, actor_id=None, previous=None, new=None, reason="", detail=None):
        conn.execute(
            "INSERT INTO events(id,incident_id,actor_id,kind,previous_state,new_state,reason,detail,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (_id(), incident_id, actor_id, kind, previous, new, reason, _json(detail or {}), utcnow()),
        )

    @staticmethod
    def _detected_time(value):
        try:
            if value is None:
                dt = datetime.now(timezone.utc)
            elif isinstance(value, (int, float)):
                dt = datetime.fromtimestamp(value, timezone.utc)
            else:
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    raise DomainError("Detection timestamp must include timezone", 422)
                dt = dt.astimezone(timezone.utc)
        except (ValueError, OverflowError, OSError) as error:
            raise DomainError("Invalid detection timestamp", 422) from error
        if dt.timestamp() > time.time() + 60:
            raise DomainError("Detection timestamp is in the future", 422)
        return dt.isoformat(), dt.timestamp()

    def create_incident(
        self,
        camera_id,
        score,
        model,
        detected_at=None,
        event_key=None,
        evidence_source=None,
        clip_start=None,
        clip_end=None,
        cooldown_seconds=60,
        detection_context=None,
    ):
        if (
            not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not 0 <= score <= 1
            or not model
        ):
            raise DomainError("Finite suspicion score and model identifier required", 422)
        if not math.isfinite(cooldown_seconds) or cooldown_seconds < 0:
            raise DomainError("Nonnegative finite cooldown required", 422)
        stamp, epoch = self._detected_time(detected_at)
        prepared = self._prepare_evidence(evidence_source, clip_start, clip_end) if evidence_source else None
        incident_id = _id()
        try:
            with self.db.transaction() as conn:
                if not self._one(conn, "SELECT id FROM cameras WHERE id=? AND enabled=1", (camera_id,)):
                    raise DomainError("Enabled camera required", 404)
                existing = (
                    self._one(
                        conn, "SELECT incident_id AS id FROM incident_keys WHERE event_key=?", (event_key,)
                    )
                    if event_key
                    else None
                )
                if not existing and cooldown_seconds:
                    existing = self._one(
                        conn,
                        "SELECT id FROM incidents WHERE camera_id=? AND detected_epoch>=? AND detected_epoch<=? ORDER BY detected_epoch DESC LIMIT 1",
                        (camera_id, epoch - cooldown_seconds, epoch + cooldown_seconds),
                    )
                if existing:
                    incident_id = existing["id"]
                    # Preserve every event key permanently, including correlated detections.
                    if event_key:
                        conn.execute(
                            "INSERT INTO incident_keys(event_key,incident_id) VALUES(?,?) ON CONFLICT(event_key) DO NOTHING",
                            (event_key, incident_id),
                        )
                else:
                    if not existing:
                        now = utcnow()
                        conn.execute(
                            "INSERT INTO incidents(id,camera_id,status,score,model,detected_at,detected_epoch,event_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (
                                incident_id,
                                camera_id,
                                "DETECTED",
                                score,
                                model,
                                stamp,
                                epoch,
                                event_key,
                                now,
                                now,
                            ),
                        )
                        if event_key:
                            conn.execute(
                                "INSERT INTO incident_keys(event_key,incident_id) VALUES(?,?)",
                                (event_key, incident_id),
                            )
                        self._event(
                            conn,
                            incident_id,
                            "detected",
                            new="DETECTED",
                            detail={
                                "score": score,
                                "model": model,
                                "uncalibrated": True,
                                "pipeline": detection_context or {},
                                "correlation_id": incident_id,
                            },
                        )
                        recipients = self._all(
                            conn, "SELECT id FROM users WHERE role IN ('director','deputy_director')"
                        )
                        for recipient in recipients:
                            conn.execute(
                                "INSERT INTO notification_outbox(id,incident_id,user_id,state,attempts,next_attempt,created_at) VALUES(?,?,?,'queued',0,?,?)",
                                (_id(), incident_id, recipient["id"], time.time(), now),
                            )
                        if len(recipients) < 2:
                            self._event(
                                conn,
                                incident_id,
                                "recipient_configuration_incomplete",
                                detail={"provisioned": len(recipients), "expected": 2},
                            )
                        self._audit(
                            conn,
                            None,
                            "incident.detected",
                            incident_id,
                            {"camera_id": camera_id, "score": score, "model": model},
                        )
                if prepared and not existing:
                    self._attach_evidence(conn, incident_id, prepared)
                    prepared = None
        finally:
            if prepared:
                # Only the exclusive copy made by this invocation is cleaned up.
                (self.data_dir / prepared["path"]).unlink(missing_ok=True)
        return self.incident(incident_id)

    def record_pipeline_commit(self, incident_id, observed_utc, elapsed_seconds):
        """Post-commit observation; deliberately not an exact database commit timestamp."""
        with self.db.transaction() as conn:
            self._event(
                conn,
                incident_id,
                "pipeline.persisted",
                detail={
                    "correlation_id": incident_id,
                    "T6_commit_observed_utc": observed_utc,
                    "T7_queue_commit_observed_utc": observed_utc,
                    "evidence_and_persistence_seconds_monotonic": elapsed_seconds,
                    "commit_observation": "upper bound observed after commit and incident read; incident and outbox share one transaction",
                },
            )

    def operational_summary(self):
        with self.db.read() as conn:
            counts = self._all(conn, "SELECT state,COUNT(*) AS n FROM notification_outbox GROUP BY state")
            oldest = conn.execute(
                "SELECT MIN(created_at) AS first FROM notification_outbox WHERE state IN ('queued','sending')"
            ).fetchone()["first"]
            attempts = conn.execute("SELECT COUNT(*) AS n FROM notification_attempts").fetchone()["n"]
        age = max(0, time.time() - datetime.fromisoformat(oldest).timestamp()) if oldest else None
        try:
            storage = {
                "state": "available" if self.evidence_dir.is_dir() else "offline",
                "free_bytes": shutil.disk_usage(self.evidence_dir).free,
            }
        except OSError:
            storage = {"state": "offline", "free_bytes": None}
        return {
            "database": "connected",
            "observed_at": utcnow(),
            "evidence_storage": storage,
            "outbox_counts": {r["state"]: r["n"] for r in counts},
            "oldest_pending_seconds_wall_estimate": age,
            "recorded_delivery_attempts": attempts,
            "clock_sync_uncertainty_seconds": None,
        }

    def record_incident_opened(self, incident_id, actor_id):
        received = utcnow()
        with self.db.transaction() as conn:
            self._actor(conn, actor_id)
            if not self._one(conn, "SELECT id FROM incidents WHERE id=?", (incident_id,)):
                raise DomainError("Incident not found", 404)
            self._audit(
                conn,
                actor_id,
                "incident.opened",
                incident_id,
                {
                    "correlation_id": incident_id,
                    "T10_client_report_received_utc": received,
                    "observation": "Authenticated client reports rendered incident; not proof of human reading or acknowledgement",
                },
            )
        return {"recorded": True, "server_received_utc": received}

    def incident(self, incident_id):
        with self.db.read() as conn:
            row = self._one(
                conn,
                "SELECT i.*,c.name AS camera_name,c.location AS camera_location,c.location AS location FROM incidents i JOIN cameras c ON c.id=i.camera_id WHERE i.id=?",
                (incident_id,),
            )
            if not row:
                return None
            row["events"] = self._all(
                conn, "SELECT * FROM events WHERE incident_id=? ORDER BY created_at,id", (incident_id,)
            )
            row["evidence"] = self._all(
                conn,
                "SELECT id,incident_id,original_name,sha256,size_bytes,clip_start,clip_end,created_at FROM evidence WHERE incident_id=? ORDER BY created_at",
                (incident_id,),
            )
        for event in row["events"]:
            event["detail"] = json.loads(event["detail"])
        row["type"] = "suspected_violence"
        return row

    @staticmethod
    def _date_filters(from_date, to_date):
        for value in (from_date, to_date):
            if value:
                try:
                    if datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") != value:
                        raise ValueError
                except ValueError:
                    raise DomainError("Dates must use YYYY-MM-DD in UTC", 422) from None
        if from_date and to_date and from_date > to_date:
            raise DomainError("Start date must not follow end date", 422)
        return from_date, to_date

    def incident_page(
        self,
        limit=100,
        status=None,
        camera_id=None,
        *,
        offset=0,
        q="",
        from_date=None,
        to_date=None,
        sort="newest",
        min_score=None,
    ):
        orders = {
            "newest": "i.detected_epoch DESC,i.id DESC",
            "oldest": "i.detected_epoch ASC,i.id ASC",
            "score": "i.score DESC,i.detected_epoch DESC,i.id DESC",
        }
        if sort not in orders or offset < 0 or len(q) > 200:
            raise DomainError("Invalid incident query", 422)
        if min_score is not None and (not math.isfinite(min_score) or not 0 <= min_score <= 1):
            raise DomainError("Signal threshold must be between zero and one", 422)
        self._date_filters(from_date, to_date)
        where, params = [], []
        for key, value in (("status", status), ("camera_id", camera_id)):
            if value:
                where.append("i." + key + "=?")
                params.append(value)
        for value, operator in ((from_date, ">="), (to_date, "<=")):
            if value:
                where.append("SUBSTR(i.detected_at,1,10)" + operator + "?")
                params.append(value)
        if q.strip():
            # Literal substring matching; user input never becomes SQL or a wildcard.
            where.append(
                "(LOWER(i.id) LIKE ? ESCAPE '!' OR LOWER(c.name) LIKE ? ESCAPE '!' OR LOWER(c.location) LIKE ? ESCAPE '!')"
            )
            literal = q.strip().lower().replace("!", "!!").replace("%", "!%").replace("_", "!_")
            params.extend(["%" + literal + "%"] * 3)
        if min_score is not None:
            where.append("i.score>=?")
            params.append(min_score)
        source = " FROM incidents i JOIN cameras c ON c.id=i.camera_id" + (
            " WHERE " + " AND ".join(where) if where else ""
        )
        limit = max(1, min(int(limit), 1000))
        with self.db.read() as conn:
            total = conn.execute("SELECT COUNT(*) AS n" + source, tuple(params)).fetchone()["n"]
            rows = self._all(
                conn,
                "SELECT i.*,c.name AS camera_name,c.location AS camera_location,c.location AS location,(SELECT COUNT(*) FROM evidence e WHERE e.incident_id=i.id) AS evidence_count"
                + source
                + " ORDER BY "
                + orders[sort]
                + " LIMIT ? OFFSET ?",
                tuple(params + [limit, offset]),
            )
        return {
            "items": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None,
            "time_basis": "UTC",
        }

    def incidents(self, limit=100, status=None, camera_id=None):
        return self.incident_page(limit, status, camera_id)["items"]

    def transition(self, incident_id, action, actor_id, reason=""):
        targets = {
            "review": "PENDING_REVIEW",
            "acknowledge": "ACKNOWLEDGED",
            "respond": "RESPONDING",
            "confirm": "CONFIRMED",
            "resolve": "RESOLVED",
            "close": "CLOSED",
            "false-positive": "FALSE_POSITIVE",
        }
        edges = {
            "DETECTED": {"PENDING_REVIEW", "ACKNOWLEDGED", "FALSE_POSITIVE"},
            "PENDING_REVIEW": {"ACKNOWLEDGED", "FALSE_POSITIVE"},
            "ACKNOWLEDGED": {"RESPONDING", "CONFIRMED", "FALSE_POSITIVE"},
            "RESPONDING": {"RESOLVED", "FALSE_POSITIVE"},
            "CONFIRMED": {"RESOLVED"},
            "RESOLVED": {"CLOSED"},
        }
        target = targets.get(action)
        if not target:
            raise DomainError("Unknown incident action", 422)
        if target in ("CLOSED", "FALSE_POSITIVE") and not reason.strip():
            raise DomainError("Reason required", 422)
        with self.db.transaction() as conn:
            self._actor(conn, actor_id)
            row = self._one(conn, "SELECT * FROM incidents WHERE id=?", (incident_id,))
            if not row:
                raise DomainError("Incident not found", 404)
            if target not in edges.get(row["status"], set()):
                raise DomainError("Illegal incident transition")
            conn.execute(
                "UPDATE incidents SET status=?,updated_at=? WHERE id=?", (target, utcnow(), incident_id)
            )
            self._event(conn, incident_id, action, actor_id, row["status"], target, reason.strip())
            self._audit(
                conn,
                actor_id,
                "incident." + action,
                incident_id,
                {"previous": row["status"], "new": target, "reason": reason.strip()},
            )
        return self.incident(incident_id)

    def add_note(self, incident_id, actor_id, reason):
        if not reason.strip():
            raise DomainError("Note text required", 422)
        with self.db.transaction() as conn:
            self._actor(conn, actor_id)
            if not self._one(conn, "SELECT id FROM incidents WHERE id=?", (incident_id,)):
                raise DomainError("Incident not found", 404)
            self._event(conn, incident_id, "note", actor_id, reason=reason.strip())
            self._audit(conn, actor_id, "incident.note", incident_id, {"reason": reason.strip()})
        return self.incident(incident_id)

    def _prepare_evidence(self, source_path, clip_start, clip_end):
        try:
            start, end = float(clip_start or 0), float(clip_end or 0)
        except (ValueError, TypeError) as error:
            raise DomainError("Valid evidence clip boundaries required", 422) from error
        if not all(math.isfinite(x) for x in (start, end)) or start < 0 or end < start:
            raise DomainError("Valid evidence clip boundaries required", 422)
        source = Path(source_path)
        if not source.is_file():
            raise DomainError("Evidence source unavailable", 422)
        evidence_id = _id()
        suffix = source.suffix.lower()
        if suffix not in (".mp4", ".avi", ".mov", ".mkv", ".webm", ".ts"):
            suffix = ".bin"
        destination = self.evidence_dir / (evidence_id + suffix)
        digest, size = hashlib.sha256(), 0
        try:
            with source.open("rb") as src, destination.open("xb") as dest:
                os.chmod(destination, 0o600)
                while chunk := src.read(1024 * 1024):
                    dest.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                dest.flush()
                os.fsync(dest.fileno())
            os.chmod(destination, 0o400)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return {
            "id": evidence_id,
            "path": str(destination.relative_to(self.data_dir)),
            "original_name": source.name,
            "sha256": digest.hexdigest(),
            "size_bytes": size,
            "clip_start": start,
            "clip_end": end,
            "created_at": utcnow(),
        }

    def _attach_evidence(self, conn, incident_id, item):
        conn.execute(
            "INSERT INTO evidence(id,incident_id,path,original_name,sha256,size_bytes,clip_start,clip_end,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                item["id"],
                incident_id,
                item["path"],
                item["original_name"],
                item["sha256"],
                item["size_bytes"],
                item["clip_start"],
                item["clip_end"],
                item["created_at"],
            ),
        )
        self._event(
            conn,
            incident_id,
            "evidence.preserved",
            detail={
                "evidence_id": item["id"],
                "sha256": item["sha256"],
                "clip_start": item["clip_start"],
                "clip_end": item["clip_end"],
            },
        )
        self._audit(
            conn,
            None,
            "evidence.preserved",
            item["id"],
            {"incident_id": incident_id, "sha256": item["sha256"], "size_bytes": item["size_bytes"]},
        )

    def add_evidence(self, incident_id, source_path, clip_start, clip_end):
        item = self._prepare_evidence(source_path, clip_start, clip_end)
        try:
            with self.db.transaction() as conn:
                if not self._one(conn, "SELECT id FROM incidents WHERE id=?", (incident_id,)):
                    raise DomainError("Incident not found", 404)
                self._attach_evidence(conn, incident_id, item)
        except BaseException:
            (self.data_dir / item["path"]).unlink(missing_ok=True)
            raise
        return self.evidence(item["id"])

    def evidence(self, evidence_id, private=False):
        with self.db.read() as conn:
            row = self._one(conn, "SELECT * FROM evidence WHERE id=?", (evidence_id,))
        if row and not private:
            row.pop("path")
        return row

    def evidence_list(self):
        with self.db.read() as conn:
            return self._all(
                conn,
                "SELECT id,incident_id,original_name,sha256,size_bytes,clip_start,clip_end,created_at FROM evidence ORDER BY created_at DESC",
            )

    def evidence_path(self, item):
        relative = Path(item["path"])
        path = self.data_dir / relative
        resolved = path.resolve()
        if (
            relative.is_absolute()
            or not resolved.is_relative_to(self.evidence_dir.resolve())
            or path.is_symlink()
        ):
            raise DomainError("Unsafe evidence path", 409)
        return resolved

    def verify_evidence(self, evidence_id, actor_id):
        with self.db.read() as conn:
            self._actor(conn, actor_id)
        item = self.evidence(evidence_id, private=True)
        if not item:
            raise DomainError("Evidence not found", 404)
        path = self.evidence_path(item)
        digest, size = hashlib.sha256(), 0
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            valid = size == item["size_bytes"] and digest.hexdigest() == item["sha256"]
            result = {
                "id": evidence_id,
                "valid": valid,
                "status": "verified" if valid else "mismatch",
                "sha256": digest.hexdigest(),
            }
        except FileNotFoundError:
            result = {"id": evidence_id, "valid": False, "status": "missing"}
        self.audit(actor_id, "evidence.verified", evidence_id, result)
        return result

    def _audit(self, conn, actor_id, action, resource_id, detail=None):
        last = self._one(
            conn, "SELECT hash,created_at FROM audit_logs ORDER BY created_at DESC,id DESC LIMIT 1"
        )
        stamp = utcnow()
        if last and stamp <= last["created_at"]:
            stamp = (datetime.fromisoformat(last["created_at"]) + timedelta(microseconds=1)).isoformat()
        row = {
            "id": _id(),
            "actor_id": actor_id,
            "action": action,
            "resource_id": str(resource_id),
            "detail": _json(detail or {}),
            "created_at": stamp,
            "previous_hash": last["hash"] if last else "0" * 64,
        }
        row["hash"] = hashlib.sha256(_json(row).encode()).hexdigest()
        conn.execute(
            "INSERT INTO audit_logs(id,actor_id,action,resource_id,detail,created_at,previous_hash,hash) VALUES(?,?,?,?,?,?,?,?)",
            tuple(
                row[key]
                for key in (
                    "id",
                    "actor_id",
                    "action",
                    "resource_id",
                    "detail",
                    "created_at",
                    "previous_hash",
                    "hash",
                )
            ),
        )

    def audit(self, actor_id, action, resource_id, detail=None):
        with self.db.transaction() as conn:
            self._audit(conn, actor_id, action, resource_id, detail)

    def audit_page(self, limit=100, *, offset=0, q="", category="", from_date=None, to_date=None):
        self._date_filters(from_date, to_date)
        if offset < 0 or len(q) > 200 or category not in ("", "security", "operations"):
            raise DomainError("Invalid journal query", 422)
        where, params = [], []
        security = "(action LIKE ? OR action LIKE ? OR action LIKE ? OR action LIKE ?)"
        if category:
            where.append(security if category == "security" else "NOT " + security)
            params.extend(["auth.%", "user.%", "telegram.%", "evidence.%"])
        if q.strip():
            where.append(
                "(LOWER(action) LIKE ? ESCAPE '!' OR LOWER(resource_id) LIKE ? ESCAPE '!' OR LOWER(COALESCE(actor_id,'')) LIKE ? ESCAPE '!')"
            )
            literal = q.strip().lower().replace("!", "!!").replace("%", "!%").replace("_", "!_")
            params.extend(["%" + literal + "%"] * 3)
        for value, operator in ((from_date, ">="), (to_date, "<=")):
            if value:
                where.append("SUBSTR(created_at,1,10)" + operator + "?")
                params.append(value)
        source = " FROM audit_logs" + (" WHERE " + " AND ".join(where) if where else "")
        limit = max(1, min(int(limit), 1000))
        with self.db.read() as conn:
            total = conn.execute("SELECT COUNT(*) AS n" + source, tuple(params)).fetchone()["n"]
            rows = self._all(
                conn,
                "SELECT *" + source + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                tuple(params + [limit, offset]),
            )
        for row in rows:
            row["detail"] = json.loads(row["detail"])
        return {
            "items": rows,
            "total": total,
            "offset": offset,
            "limit": limit,
            "next_offset": offset + limit if offset + limit < total else None,
            "time_basis": "UTC",
        }

    def audits(self, limit=100):
        return self.audit_page(limit)["items"]

    def verify_audit(self):
        with self.db.read() as conn:
            rows = self._all(conn, "SELECT * FROM audit_logs ORDER BY created_at,id")
        previous = "0" * 64
        for row in rows:
            expected = row.pop("hash")
            if (
                row["previous_hash"] != previous
                or hashlib.sha256(_json(row).encode()).hexdigest() != expected
            ):
                return {"valid": False, "count": len(rows), "broken_at": row["id"]}
            previous = expected
        return {
            "valid": True,
            "count": len(rows),
            "head": previous,
            "scope": "Application hash chain; database owner can bypass controls",
        }

    def overview(self):
        today = utcnow()[:10]
        with self.db.read() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM incidents").fetchone()["n"]
            opened = conn.execute(
                "SELECT COUNT(*) AS n FROM incidents WHERE status NOT IN ('CLOSED','FALSE_POSITIVE')"
            ).fetchone()["n"]
            states = self._all(conn, "SELECT status,COUNT(*) AS count FROM incidents GROUP BY status")
            recent_activity = self._all(
                conn,
                "SELECT action,actor_id,resource_id,detail,created_at FROM audit_logs WHERE (action LIKE ? AND action<>?) OR action LIKE ? OR action LIKE ? ORDER BY created_at DESC,id DESC LIMIT 4",
                ("incident.%", "incident.opened", "notification.%", "pipeline.%"),
            )
            for event in recent_activity:
                event["detail"] = json.loads(event["detail"])

            days = self._all(
                conn,
                "SELECT SUBSTR(detected_at,1,10) AS day,COUNT(*) AS count FROM incidents GROUP BY SUBSTR(detected_at,1,10) ORDER BY day",
            )
            today_states = self._all(
                conn,
                "SELECT status,COUNT(*) AS count FROM incidents WHERE SUBSTR(detected_at,1,10)=? GROUP BY status",
                (today,),
            )
            outbox = self._all(conn, "SELECT state,COUNT(*) AS count FROM notification_outbox GROUP BY state")
        cameras = self.cameras()
        return {
            "total_incidents": total,
            "incidents_today": sum(row["count"] for row in today_states),
            "today_states": today_states,
            "today_utc": today,
            "open_incidents": opened,
            "total_cameras": len(cameras),
            "active_cameras": sum(bool(c["enabled"]) for c in cameras),
            "connected_cameras": sum(
                c["state"] == "connected"
                and bool(c["last_frame_at"])
                and time.time() - c["last_frame_at"] < 30
                for c in cameras
            ),
            "incident_states": states,
            "trends": days,
            "notifications": outbox,
            "workers": self.health(),
            "recent_incidents": self.incidents(5),
            "recent_activity": recent_activity,
            "time_basis": "UTC",
        }

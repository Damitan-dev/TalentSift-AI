"""Single-recruiter beta access, invitation admission and durable usage quotas.

All admission decisions use BEGIN IMMEDIATE: two workers cannot spend the same
invitation or take the last quota slot together. Tokens are random bearer
credentials; only their SHA-256 digests are persisted.
"""
import base64
import binascii
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import urlsplit
from uuid import uuid4

import database


class AccessError(Exception):
    def __init__(self, message, status=403):
        super().__init__(message)
        self.status = status


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def password_hash(password):
    if not 15 <= len(password) <= 128:
        raise ValueError("Use a password or passphrase of 15 to 128 characters.")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=3,
                             maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt$32768$8$3${salt.hex()}${derived.hex()}"


def validate_hash(encoded):
    try:
        algorithm, n, r, p, salt, derived = encoded.split("$")
        return (algorithm, n, r, p) == ("scrypt", "32768", "8", "3") and \
            len(bytes.fromhex(salt)) == 16 and len(bytes.fromhex(derived)) == 32
    except (ValueError, AttributeError):
        return False


def verify_password(password, encoded):
    if not validate_hash(encoded) or not isinstance(password, str) or len(password) > 128:
        return False
    _, _, _, _, salt, expected = encoded.split("$")
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768,
                            r=8, p=3, maxmem=64 * 1024 * 1024, dklen=32)
    return hmac.compare_digest(actual.hex(), expected)


@dataclass(frozen=True)
class AccessSettings:
    public_url: str = ""
    daily_limit: int = 10
    concurrent_limit: int = 2
    interview_seconds: int = 600

    @property
    def hard_seconds(self):
        # Covers startup, opening, closing and scoring even if a malicious client
        # never acknowledges opening playback and the normal timer never starts.
        return self.interview_seconds + 120

    @classmethod
    def from_env(cls):
        url = os.getenv("TALENTSIFT_PUBLIC_URL", "").rstrip("/")
        if url:
            parts = urlsplit(url)
            if (parts.scheme != "https" or not parts.netloc or parts.path or
                    parts.query or parts.fragment or parts.username or parts.password):
                raise ValueError("TALENTSIFT_PUBLIC_URL must be the HTTPS origin, without a path.")
        def number(name, default, high):
            value = int(os.getenv(name, str(default)))
            if not 1 <= value <= high:
                raise ValueError(f"{name} must be between 1 and {high}.")
            return value
        return cls(url, number("TALENTSIFT_DAILY_INTERVIEWS", 10, 10000),
                   number("TALENTSIFT_CONCURRENT_INTERVIEWS", 2, 100),
                   number("TALENTSIFT_INTERVIEW_SECONDS", 600, 3600))

    def origin(self, connection):
        if self.public_url:
            if connection.headers.get("host", "").lower() != urlsplit(self.public_url).netloc.lower():
                raise AccessError("Unrecognized site address.", 400)
            return self.public_url
        host = connection.headers.get("host", "")
        # Without an explicit public HTTPS origin, only loopback development is allowed.
        try:
            parsed = urlsplit("http://" + host)
            valid = parsed.hostname in ("localhost", "127.0.0.1", "::1") and not parsed.username
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid or parsed.path or parsed.query or parsed.fragment:
            raise AccessError("Public access is not configured.", 503)
        return "http://" + host

    def check_origin(self, connection):
        expected = self.origin(connection)
        source = connection.headers.get("origin")
        if not source and connection.scope["type"] == "http":
            referrer = urlsplit(connection.headers.get("referer", ""))
            source = f"{referrer.scheme}://{referrer.netloc}"
        if source != expected:
            raise AccessError("This request must come from the TalentSift site.")

    def cookie_name(self, kind):
        prefix = "__Host-" if self.public_url else ""
        return f"{prefix}ts_{kind}"


class AccessStore:
    def __init__(self, settings=None, clock=time.time):
        self.settings = settings or AccessSettings.from_env()
        self.clock = clock

    @contextmanager
    def transaction(self):
        connection = database.get_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        with self.transaction() as db:
            statements = [
                """CREATE TABLE IF NOT EXISTS recruiter_account (
                    id INTEGER PRIMARY KEY CHECK (id=1), username TEXT NOT NULL,
                    password_hash TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS invitations (
                    id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
                    job_id TEXT NOT NULL REFERENCES jobs(id), candidate_name TEXT NOT NULL,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL,
                    revoked_at REAL, started_at REAL,
                    session_id TEXT UNIQUE REFERENCES sessions(id))""",
                """CREATE TABLE IF NOT EXISTS access_sessions (
                    token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, csrf TEXT NOT NULL,
                    invitation_id TEXT REFERENCES invitations(id),
                    created_at REAL NOT NULL, expires_at REAL NOT NULL,
                    last_seen REAL NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS interview_usage (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
                    started_at REAL NOT NULL, lease_until REAL NOT NULL, ended_at REAL)""",
                "CREATE INDEX IF NOT EXISTS usage_started ON interview_usage(started_at)",
                """CREATE TABLE IF NOT EXISTS access_rate_limits (
                    key TEXT PRIMARY KEY, window_start REAL NOT NULL, count INTEGER NOT NULL)""",
            ]
            for statement in statements:
                db.execute(statement)

    def configure_recruiter(self, username, encoded, replace=False):
        username = username.strip().casefold()
        if not username or len(username) > 120 or not validate_hash(encoded):
            raise ValueError("A username and a valid TalentSift password hash are required.")
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM recruiter_account WHERE id=1").fetchone()
            if existing and existing["username"] == username and existing["password_hash"] == encoded:
                return
            if existing and not replace:
                raise ValueError("An account already exists. Use reset-recruiter to replace it.")
            db.execute("INSERT OR REPLACE INTO recruiter_account VALUES (1, ?, ?)", (username, encoded))
            db.execute("DELETE FROM access_sessions WHERE kind IN ('recruiter', 'login')")

    def bootstrap_from_env(self):
        username = os.getenv("TALENTSIFT_RECRUITER_USERNAME")
        encoded = os.getenv("TALENTSIFT_RECRUITER_PASSWORD_HASH")
        if username or encoded:
            self.configure_recruiter(username or "", encoded or "", replace=True)

    def account(self):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM recruiter_account WHERE id=1").fetchone()
            return dict(row) if row else None

    def rate_limit(self, key, limit, seconds):
        now = self.clock()
        with self.transaction() as db:
            db.execute("DELETE FROM access_rate_limits WHERE window_start < ?", (now - 86400,))
            row = db.execute("SELECT * FROM access_rate_limits WHERE key=?", (key,)).fetchone()
            count = row["count"] if row and row["window_start"] > now - seconds else 0
            start = row["window_start"] if count else now
            if count >= limit:
                raise AccessError("Too many attempts. Please wait and try again.", 429)
            db.execute("INSERT OR REPLACE INTO access_rate_limits VALUES (?, ?, ?)", (key, start, count + 1))

    def issue_session(self, kind, invitation_id=None, previous=None):
        now = self.clock()
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        lifetime = 600 if kind == "login" else 8 * 3600
        with self.transaction() as db:
            db.execute("DELETE FROM access_sessions WHERE expires_at <= ?", (now,))
            if previous:
                db.execute("DELETE FROM access_sessions WHERE token_hash=? AND kind=?", (digest(previous), kind))
            db.execute("INSERT INTO access_sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (digest(token), kind, csrf, invitation_id, now, now + lifetime, now))
        return token, csrf, lifetime

    def get_session(self, token, kind):
        if not token or len(token) > 100:
            return None
        now = self.clock()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM access_sessions WHERE token_hash=? AND kind=?",
                             (digest(token), kind)).fetchone()
            if not row or row["expires_at"] <= now or (kind == "recruiter" and row["last_seen"] <= now - 3600):
                return None
            if row["last_seen"] < now - 60:
                db.execute("UPDATE access_sessions SET last_seen=? WHERE token_hash=?", (now, row["token_hash"]))
            result = dict(row)
            if kind == "recruiter":
                account = db.execute("SELECT username FROM recruiter_account WHERE id=1").fetchone()
                if not account:
                    return None
                result["username"] = account["username"]
            return result

    def logout(self, token):
        with self.transaction() as db:
            db.execute("DELETE FROM access_sessions WHERE token_hash=?", (digest(token or ""),))

    def create_invitation(self, job_id, name, hours):
        name = " ".join(name.split())
        if not name or len(name) > 120 or not 1 <= hours <= 168:
            raise AccessError("Enter a name (up to 120 characters) and expiry from 1 to 168 hours.", 422)
        token, iid, now = secrets.token_urlsafe(32), str(uuid4()), self.clock()
        with self.transaction() as db:
            if not db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                raise AccessError("Job not found.", 404)
            db.execute("INSERT INTO invitations (id, token_hash, job_id, candidate_name, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (iid, digest(token), job_id, name, now, now + hours * 3600))
        return iid, token

    def _invitation(self, db, *, invitation_id=None, token=None, allow_started=False):
        row = db.execute("SELECT i.*, j.title FROM invitations i JOIN jobs j ON j.id=i.job_id WHERE " +
                         ("i.token_hash=?" if token is not None else "i.id=?"),
                         (digest(token) if token is not None else invitation_id,)).fetchone()
        if not row:
            raise AccessError("This invitation is invalid. Ask the recruiter for your link.", 404)
        if row["revoked_at"] is not None:
            raise AccessError("This invitation was cancelled. Contact the recruiter.", 410)
        if row["started_at"] is not None and not allow_started:
            raise AccessError("This invitation has already been used. Contact the recruiter if you need another interview.", 409)
        if row["expires_at"] <= self.clock():
            raise AccessError("This invitation has expired. Ask the recruiter for a new link.", 410)
        return dict(row)

    def invitation(self, **kwargs):
        with self.transaction() as db:
            return self._invitation(db, **kwargs)

    def list_invitations(self, job_id):
        with self.transaction() as db:
            rows = db.execute("SELECT id, candidate_name, expires_at, revoked_at, started_at, session_id FROM invitations WHERE job_id=? ORDER BY created_at DESC", (job_id,)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["status"] = ("used" if row["started_at"] else "cancelled" if row["revoked_at"] else
                                  "expired" if row["expires_at"] <= self.clock() else "available")
                item["expiry_label"] = datetime.fromtimestamp(row["expires_at"], timezone.utc).strftime("%d %b %Y, %H:%M UTC")
                result.append(item)
            return result

    def revoke(self, iid, job_id):
        with self.transaction() as db:
            changed = db.execute("UPDATE invitations SET revoked_at=? WHERE id=? AND job_id=? AND started_at IS NULL AND revoked_at IS NULL", (self.clock(), iid, job_id)).rowcount
            if not changed:
                raise AccessError("This invitation cannot be cancelled; it may already be used.", 409)
            db.execute("DELETE FROM access_sessions WHERE invitation_id=?", (iid,))

    def _quota(self, db):
        now = self.clock()
        # After a process crash, the lease eventually expires without resetting
        # the daily count or making the consumed invitation available again.
        stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        db.execute("UPDATE sessions SET status='failed', ended_at=?, failure_reason='Interview server connection expired.' WHERE status='in_progress' AND id IN (SELECT session_id FROM interview_usage WHERE ended_at IS NULL AND lease_until<=?)", (stamp, now))
        db.execute("UPDATE interview_usage SET ended_at=? WHERE ended_at IS NULL AND lease_until<=?", (now, now))
        day_start = int(now // 86400) * 86400  # One documented UTC reset for every worker.
        daily = db.execute("SELECT count(*) FROM interview_usage WHERE started_at>=?", (day_start,)).fetchone()[0]
        active = db.execute("SELECT count(*) FROM interview_usage WHERE ended_at IS NULL AND lease_until>?", (now,)).fetchone()[0]
        return {"daily": daily, "active": active, "daily_limit": self.settings.daily_limit,
                "concurrent_limit": self.settings.concurrent_limit}

    def usage(self):
        with self.transaction() as db:
            return self._quota(db)

    def _check_quota(self, db):
        quota = self._quota(db)
        if quota["daily"] >= quota["daily_limit"]:
            raise AccessError("Today's interview limit has been reached. Contact the recruiter to arrange another time.", 429)
        if quota["active"] >= quota["concurrent_limit"]:
            raise AccessError("All interview slots are busy. Please try again shortly; your invitation is still available.", 429)

    def prepare_interview(self, iid, language):
        now = self.clock()
        stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        with self.transaction() as db:
            invite = self._invitation(db, invitation_id=iid)
            self._check_quota(db)
            if invite["session_id"]:
                row = db.execute("SELECT * FROM sessions WHERE id=?", (invite["session_id"],)).fetchone()
                if row["status"] != "pending":
                    raise AccessError("This interview is no longer available. Contact the recruiter.", 409)
                db.execute("UPDATE sessions SET language=?, consented_at=? WHERE id=?", (language, stamp, row["id"]))
                db.execute("UPDATE candidates SET preferred_language=? WHERE id=?", (language, row["candidate_id"]))
                return row["id"], row["candidate_id"]
            cid, sid = str(uuid4()), str(uuid4())
            db.execute("INSERT INTO candidates (id, full_name, preferred_language, created_at) VALUES (?, ?, ?, ?)", (cid, invite["candidate_name"], language, stamp))
            db.execute("INSERT INTO sessions (id, job_id, candidate_id, status, language, consent_given, created_at, consented_at) VALUES (?, ?, ?, 'pending', ?, 1, ?, ?)", (sid, invite["job_id"], cid, language, stamp, stamp))
            db.execute("UPDATE invitations SET session_id=? WHERE id=?", (sid, iid))
            return sid, cid

    def claim_interview(self, iid, sid):
        now = self.clock()
        with self.transaction() as db:
            invite = self._invitation(db, invitation_id=iid)
            if invite["session_id"] != sid:
                raise AccessError("This interview does not belong to your invitation.")
            session = db.execute("SELECT status, consent_given FROM sessions WHERE id=?", (sid,)).fetchone()
            if not session or session["status"] != "pending" or not session["consent_given"]:
                raise AccessError("This interview cannot be started again.", 409)
            self._check_quota(db)
            db.execute("UPDATE invitations SET started_at=? WHERE id=?", (now, iid))
            db.execute("INSERT INTO interview_usage VALUES (?, ?, ?, NULL)", (sid, now, now + self.settings.hard_seconds + 30))
            db.execute("UPDATE sessions SET status='in_progress', started_at=? WHERE id=?",
                       (datetime.fromtimestamp(now, timezone.utc).isoformat(), sid))

    def finish_interview(self, sid):
        with self.transaction() as db:
            db.execute("UPDATE interview_usage SET ended_at=? WHERE session_id=? AND ended_at IS NULL", (self.clock(), sid))

    def authorize_recording(self, iid, sid):
        with self.transaction() as db:
            invite = self._invitation(db, invitation_id=iid, allow_started=True)
            if invite["session_id"] != sid:
                raise AccessError("This recording does not belong to your invitation.")


class AudioBudget:
    """Bound PCM16 input to real-time speed with ten seconds of network tolerance.

    A wall-clock interview limit alone would still let a modified client upload
    hours of audio quickly. This token bucket checks bytes, not browser claims.
    """
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.previous = clock()
        self.credit = 48000 * 10  # 24,000 samples/sec x two bytes/sample.

    def accept(self, encoded):
        if not isinstance(encoded, str) or len(encoded) > 128000:
            raise AccessError("Invalid microphone audio.")
        try:
            pcm = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise AccessError("Invalid microphone audio.") from None
        if not pcm or len(pcm) % 2:
            raise AccessError("Invalid PCM16 microphone audio.")
        now = self.clock()
        self.credit = min(48000 * 10, self.credit + max(0, now - self.previous) * 48000)
        self.previous = now
        if len(pcm) > self.credit:
            raise AccessError("Microphone audio exceeded the allowed streaming rate.")
        self.credit -= len(pcm)

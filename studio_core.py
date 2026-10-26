"""Non-Jitsi recording reliability and production workflow extension.
Installed by server.py after application routes have been registered.
Single-process deployment: filesystem commits + SQLite WAL, bounded workers.
"""
from __future__ import annotations
import asyncio
import copy
import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import wave
import zipfile
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs
from recording_profiles import normalize_profile, resolve_profile, expected_streams
from recording_processing import build_recording
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.background import BackgroundTask

PROFILE_BASE = dict(sample_rate=48000, channels=1, width=1280, height=720, fps=30,
                    audio_bitrate=192000, video_bitrate=5000000, chunk_ms=5000,
                    max_duration_s=14400, container="pcm", codec="pcm_s16le", video=False)
PROFILES = [dict(PROFILE_BASE, id="audio-standard", name="Audio Standard"),
            dict(PROFILE_BASE, id="audio-high", name="Audio High Quality", sample_rate=96000, channels=2)]
DEFAULTS = dict(profiles=PROFILES, default_profile="audio-standard",
                marker_categories=[dict(id="ad", label="Werbung", color="#f59e0b"),
                                   dict(id="cut_in", label="Schnitt Anfang", color="#e5484d"),
                                   dict(id="cut_out", label="Schnitt Ende", color="#30a46c")],
                consent_text="Ich stimme der Aufnahme, lokalen Zwischenspeicherung und Übertragung meiner Audio-/Videodaten für diese Produktion zu. Live-Call-Daten werden durch den gewählten Jitsi-Anbieter verarbeitet.",
                consent_text_en="I consent to recording, local storage and transfer of my audio/video data for this production. Live-call data is processed by the selected Jitsi provider.", privacy_policy="", retention_days=30, diagnostic_days=14,
                storage_region="Nicht konfiguriert", backup_policy="Nicht konfiguriert",
                collect_device_metadata=False, automatic_chapters=False,
                max_guests=12, max_duration_s=14400, max_chunk_bytes=67108864,
                max_track_bytes=21474836480, min_free_bytes=1073741824,
                requests_per_minute=240, token_requests_per_minute=180,
                admission_minutes=1440, upload_kbps=512, pause_uploads_during_recording=False,
                upload_throttle_enabled=True, pause_uploads_during_call=True, upload_min_kbps=0)
IDENT = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
HASH = re.compile(r"^[0-9a-f]{64}$")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".commit-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if hasattr(os, "O_DIRECTORY"):
            d = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(d)
            finally:
                os.close(d)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode()


def number(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise HTTPException(422, f"{name}: number required")
    if value != int(value) or not low <= value <= high:
        raise HTTPException(422, f"{name}: expected integer {low}..{high}")
    return int(value)


class Studio:
    def __init__(self, api):
        self.a = api
        self.root = api["DATA_DIR"]
        self.uploads = api["UPLOADS"]
        self.path = self.root / "studio.sqlite3"
        self.lock = threading.RLock()
        self.limit_lock = threading.Lock()
        self.guest_sockets = {}
        self.guest_socket_rooms = {}
        self.rate = OrderedDict()
        self.jobs = asyncio.Semaphore(2)
        self.production = os.getenv("STUDIO_PRODUCTION", "0") == "1"
        self.origins = set(filter(None, os.getenv("STUDIO_ALLOWED_ORIGINS", "").split(",")))
        self.init_db()
        self.config = self.load_config()
        self.repair_deleted_room_data()
        self.restore_rooms()

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def init_db(self):
        with self.db() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS security (id INTEGER PRIMARY KEY, epoch TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS takes (room TEXT, session TEXT, title TEXT DEFAULT '',
              description TEXT DEFAULT '', episode TEXT DEFAULT '', editorial TEXT DEFAULT 'draft',
              profile TEXT NOT NULL, created REAL NOT NULL, deleted INTEGER DEFAULT 0,
              PRIMARY KEY(room,session));
            CREATE TABLE IF NOT EXISTS tracks (room TEXT, session TEXT, guest TEXT,
              owner TEXT NOT NULL, meta TEXT NOT NULL, expected INTEGER, interrupted INTEGER DEFAULT 0,
              finalized INTEGER DEFAULT 0, report TEXT, telemetry TEXT DEFAULT '{}', updated REAL,
              PRIMARY KEY(room,session,guest));
            CREATE TABLE IF NOT EXISTS chunks (room TEXT, session TEXT, guest TEXT, idx INTEGER,
              size INTEGER NOT NULL, sha TEXT NOT NULL, ext TEXT NOT NULL, attempts INTEGER DEFAULT 1,
              PRIMARY KEY(room,session,guest,idx));
            CREATE TABLE IF NOT EXISTS room_state (room TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS marker_extra (id TEXT PRIMARY KEY, label TEXT DEFAULT '',
              color TEXT DEFAULT '#30a46c', locked INTEGER DEFAULT 0, approved INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, ts REAL, actor TEXT,
              action TEXT, target TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS privacy_requests (id TEXT PRIMARY KEY, owner TEXT, room TEXT,
              kind TEXT, state TEXT DEFAULT 'open', created REAL);
            CREATE TABLE IF NOT EXISTS guest_consents(owner TEXT PRIMARY KEY, room TEXT NOT NULL, digest TEXT NOT NULL, accepted REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS admissions (owner TEXT PRIMARY KEY, first_seen REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS guest_claims (owner TEXT PRIMARY KEY, client_hash TEXT NOT NULL,
              parent_owner TEXT, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS guest_claim_parent ON guest_claims(parent_owner);
            CREATE TABLE IF NOT EXISTS guest_sessions(owner TEXT PRIMARY KEY,room TEXT NOT NULL,
              credential_hash TEXT NOT NULL UNIQUE,client_hash TEXT NOT NULL,invitation_id TEXT NOT NULL,
              created REAL NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
            CREATE UNIQUE INDEX IF NOT EXISTS guest_session_browser ON guest_sessions(room,client_hash) WHERE revoked=0;
            """)
            c.execute('BEGIN IMMEDIATE')
            # Transactional, repeatable upgrade: historical chunks retain the "main" namespace.
            if 'logical' not in {r[1] for r in c.execute('PRAGMA table_info(chunks)')}:
                c.execute('ALTER TABLE chunks RENAME TO chunks_v1')
                c.execute("CREATE TABLE chunks(room TEXT,session TEXT,guest TEXT,logical TEXT NOT NULL DEFAULT 'main',idx INTEGER,size INTEGER NOT NULL,sha TEXT NOT NULL,ext TEXT NOT NULL,attempts INTEGER DEFAULT 1,PRIMARY KEY(room,session,guest,logical,idx))")
                c.execute("INSERT INTO chunks SELECT room,session,guest,'main',idx,size,sha,ext,attempts FROM chunks_v1")
                c.execute('DROP TABLE chunks_v1')
            if 'stream_expected' not in {r[1] for r in c.execute('PRAGMA table_info(tracks)')}:
                c.execute('ALTER TABLE tracks ADD COLUMN stream_expected TEXT')
            c.execute("INSERT OR IGNORE INTO security VALUES (1,?)", (secrets.token_hex(16),))

    def load_config(self):
        with self.db() as c:
            row = c.execute("SELECT value FROM settings WHERE id=1").fetchone()
        stored=json.loads(row[0]) if row else {}
        if any(k not in DEFAULTS for k in stored):
            raise RuntimeError("Stored configuration contains unsupported settings; use a new DATA_DIR")
        config = copy.deepcopy(DEFAULTS)
        config.update(copy.deepcopy(stored))
        # Default consent wording is a localized resource, not a second editor.
        for code,key in [('de','consent_text'),('en','consent_text_en')]:
            if key not in stored:
                wording=self.a.get('_load_locale',lambda _: {})(code).get('ux',{}).get('consent_default_text')
                if isinstance(wording,str) and wording.strip():config[key]=wording
        # Global audio-quality presets; historical takes keep their snapshots.
        config["profiles"] = [normalize_profile(p) for p in config["profiles"]]
        # Persist the upgrade once; never rewrite historical take/track snapshots.
        if config != stored:
            with self.db() as c:
                c.execute("INSERT OR REPLACE INTO settings VALUES(1,?)", (json.dumps(config),))
        return config

    def audit(self, actor, action, target="", detail=None):
        # Never persist credentials, full request bodies, IPs or device labels.
        with self.db() as c:
            c.execute("INSERT INTO audit(ts,actor,action,target,detail) VALUES(?,?,?,?,?)",
                      (time.time(), actor[:80], action[:100], target[:240], json.dumps(detail or {})[:4000]))

    def ident(self, *parts):
        if any(not isinstance(x, str) or not IDENT.fullmatch(x) for x in parts):
            raise HTTPException(400, "Invalid identifier")

    def dest(self, room, guest, session):
        self.ident(room, guest, session)
        p = self.uploads / room / guest / session
        if not p.resolve().is_relative_to(self.uploads.resolve()):
            raise HTTPException(400, "Invalid path")
        return p

    def role(self, request, admin=False):
        role = self.a["_session_role"](request.cookies.get(self.a["COOKIE_NAME"], ""))
        if not role:
            raise HTTPException(401, "Authentication required")
        if admin and role != "admin":
            raise HTTPException(403, "Admin required")
        return role

    def derived_token(self, owner):
        with self.db() as c:
            row = c.execute("SELECT parent_owner FROM guest_claims WHERE owner=?", (owner,)).fetchone()
        return bool(row and row[0])

    def token_parent_valid(self, token):
        """Derived credentials never survive revocation/deletion/expiry of the invitation."""
        with self.db() as c:
            row = c.execute("SELECT parent_owner FROM guest_claims WHERE owner=?", (token["id"],)).fetchone()
        parent_id = token.get("parent_id") or (row[0] if row else None)
        visited={token['id']}
        with self.a['_DB_LOCK'],self.a['_db_conn']() as c:
            while parent_id:
                if parent_id in visited or len(visited)>32:return False
                visited.add(parent_id)
                parent=c.execute('SELECT room,expires_at,revoked,parent_id FROM guest_tokens WHERE id=?',(parent_id,)).fetchone()
                if not parent or parent['revoked'] or parent['expires_at']<=time.time() or parent['room']!=token['room']:return False
                parent_id=parent['parent_id']
        return True

    def resolve_guest(self, value):
        if not isinstance(value,str) or len(value)>256:return None
        if not value.startswith('gs_'):
            # Recovery-only compatibility: legacy claims still need their browser proof.
            return self.a['_token_resolve'](value)
        digest=hashlib.sha256(value.encode()).hexdigest()
        with self.db() as c:
            row=c.execute('SELECT * FROM guest_sessions WHERE credential_hash=? AND revoked=0',(digest,)).fetchone()
        if not row or row['expires']<=time.time():return None
        with self.a['_DB_LOCK'],self.a['_db_conn']() as c:
            invitation=c.execute('SELECT revoked,expires_at FROM guest_tokens WHERE id=?',(row['invitation_id'],)).fetchone()
        if not invitation or invitation['revoked'] or invitation['expires_at']<=time.time():return None
        return dict(id=row['owner'],room=row['room'],token=value,expires_at=row['expires'],client_hash=row['client_hash'],session_kind='guest')

    def check_guest_client(self, token, client):
        if not isinstance(client,str) or not re.fullmatch(r'[0-9a-f]{64}',client):
            raise HTTPException(403,'ux.guest_identity_required')
        if token.get('session_kind')=='guest':expected=token['client_hash']
        else:
            with self.db() as c:row=c.execute('SELECT client_hash FROM guest_claims WHERE owner=?',(token['id'],)).fetchone()
            expected=row[0] if row else ''
        if not expected or not hmac.compare_digest(expected,hashlib.sha256(client.encode()).hexdigest()):
            raise HTTPException(403,'ux.guest_identity_required')

    async def join_guest(self, room, request: Request):
        """Invitation authorizes admission only; the server owns independent guest sessions."""
        self.ident(room)
        value=request.headers.get('authorization','')
        invitation=self.a['_token_resolve'](value[7:]) if value.startswith('Bearer ') else None
        client=request.headers.get('x-guest-client','')
        if not re.fullmatch(r'[0-9a-f]{64}',client):raise HTTPException(422,'ux.guest_identity_required')
        p=await self.payload(request)
        if set(p)-{'resume_token'}:raise HTTPException(422,'ux.invalid_settings')
        if not invitation or invitation['room']!=room:raise HTTPException(403,'ux.guest_invitation_invalid')
        cfg=self.a['_cfg_load']()
        if room in set(cfg.get('archived_rooms',[])):raise HTTPException(403,'ux.guest_invitation_invalid')
        self.rate_check('join:'+invitation['id'],60)
        with self.lock:
            canonical=self.a['_canonical_invitation'](room,create=False)
            if not canonical:raise HTTPException(403,'ux.guest_invitation_invalid')
            resume=p.get('resume_token','')
            current=self.resolve_guest(resume) if isinstance(resume,str) else None
            if current and current['room']==room:
                try:self.check_guest_client(current,client)
                except HTTPException:current=None
            if current and current.get('session_kind')!='guest':
                with self.db() as c:pending=c.execute('SELECT 1 FROM tracks WHERE owner=? AND finalized=0 LIMIT 1',(current['id'],)).fetchone()
                if not pending:current=None
            if current and current.get('session_kind')!='guest':
                # Keep historical owner and bearer fingerprint for pending pre-update chunks.
                return dict(ok=True,token=resume,guest='g_'+current['id'],invitation_token=canonical['token'],legacy=True)
            digest=hashlib.sha256(client.encode()).hexdigest()
            expires=time.time()+max(1,min(365,int(cfg.get('token_days',7))))*86400
            with self.db() as c:
                row=c.execute('SELECT * FROM guest_sessions WHERE room=? AND client_hash=? AND revoked=0',(room,digest)).fetchone()
                if row:
                    owner=row['owner']
                    if current and current['id']==owner:credential=resume
                    else:
                        if owner in self.guest_sockets:raise HTTPException(409,'ux.guest_identity_busy')
                        credential='gs_'+secrets.token_urlsafe(32)
                    c.execute('UPDATE guest_sessions SET credential_hash=?,expires=?,invitation_id=? WHERE owner=?',
                              (hashlib.sha256(credential.encode()).hexdigest(),expires,canonical['id'],owner))
                else:
                    owner=secrets.token_hex(16);credential='gs_'+secrets.token_urlsafe(32)
                    c.execute('INSERT INTO guest_sessions VALUES(?,?,?,?,?,?,?,0)',
                              (owner,room,hashlib.sha256(credential.encode()).hexdigest(),digest,canonical['id'],time.time(),expires))
                c.execute('INSERT OR REPLACE INTO admissions VALUES(?,?)',(owner,time.time()))
            self.audit('guest','session.admit',room,{'owner':owner})
        return dict(ok=True,token=credential,guest='g_'+owner,invitation_token=canonical['token'],expires_at=expires,legacy=False)

    def revoke_guest_owner(self, owner):
        with self.db() as c:c.execute('UPDATE guest_sessions SET revoked=1 WHERE owner=?',(owner,))
        self.a['_token_revoke'](owner)

    def reserve_guest_socket(self, token, socket):
        with self.lock:
            if token["id"] in self.guest_sockets:
                return False
            self.guest_sockets[token["id"]] = socket
            self.guest_socket_rooms[token["id"]] = token["room"]
        return True

    def release_guest_socket(self, token, socket):
        with self.lock:
            if self.guest_sockets.get(token["id"]) is socket:
                del self.guest_sockets[token["id"]]
                self.guest_socket_rooms.pop(token["id"],None)

    def guest(self, request, room, guest=None, admission=False):
        header = request.headers.get("authorization", "")
        value = header[7:] if header.startswith("Bearer ") else ""
        t = self.resolve_guest(value) if value else None
        if not t or t["room"] != room:
            raise HTTPException(403, "Invalid or revoked invitation")
        self.check_guest_client(t, request.headers.get("x-guest-client", ""))
        if guest and guest != "g_" + t["id"]:
            raise HTTPException(403, "Track identity does not belong to invitation")
        if admission:
            with self.db() as c:
                c.execute("INSERT OR IGNORE INTO admissions VALUES(?,?)", (t["id"], time.time()))
                first = c.execute("SELECT first_seen FROM admissions WHERE owner=?", (t["id"],)).fetchone()[0]
            if time.time() - first > self.config["admission_minutes"] * 60:
                raise HTTPException(403, "Admission expired; request a new invitation. Existing uploads remain recoverable.")
        self.rate_check("token:" + t["id"], self.config["token_requests_per_minute"])
        return t

    def rate_check(self, key, limit):
        now = time.monotonic()
        with self.limit_lock:
            value = self.rate.pop(key, (now, 0))
            start, count = value if now - value[0] < 60 else (now, 0)
            self.rate[key] = (start, count + 1)
            while len(self.rate) > 20000:
                self.rate.popitem(last=False)
        if count >= limit:
            raise HTTPException(429, "Rate limit", headers={"Retry-After": "60"})

    def profile(self, room):
        base = next(p for p in self.config["profiles"] if p["id"] == self.config["default_profile"])
        return resolve_profile(base, not self.a["_room"](room)["settings"].get("audio_only", True))

    def take(self, room, session, create=False):
        self.ident(room, session)
        with self.db() as c:
            if create:
                c.execute("INSERT OR IGNORE INTO takes(room,session,profile,created) VALUES(?,?,?,?)",
                          (room, session, json.dumps(self.profile(room)), time.time()))
            row = c.execute("SELECT * FROM takes WHERE room=? AND session=?", (room, session)).fetchone()
        if row and row["deleted"]:
            raise HTTPException(410, "Session deleted; recovery uploads are blocked")
        return dict(row) if row else None

    def require_session(self, room, session):
        self.ident(room, session)
        with self.a["_db_conn"]() as c:
            exists = c.execute("SELECT 1 FROM recording_sessions WHERE room=? AND session=?", (room, session)).fetchone()
        if not exists:
            raise HTTPException(404, "Unknown recording session")
        return self.take(room, session, create=True)

    def purge_room_data(self, room):
        """Delete the local incarnation, not pending provider-cleanup conferences."""
        with self.lock:
            with self.a['_DB_LOCK'],self.a['_db_conn']() as c:
                owners={r[0] for r in c.execute('SELECT id FROM guest_tokens WHERE room=?',(room,))}
                markers=[r[0] for r in c.execute('SELECT id FROM markers WHERE room=?',(room,))]
                for table in ('room_invitations','guest_tokens','markers','recording_sessions','clip_events','guest_logs','rooms'):
                    c.execute('DELETE FROM '+table+' WHERE room=?',(room,))
                c.commit()
            with self.db() as c:
                for table in ('guest_sessions','guest_consents','tracks'):
                    owners.update(r[0] for r in c.execute('SELECT owner FROM '+table+' WHERE room=?',(room,)))
                for owner in owners:
                    for table in ('admissions','guest_claims'):
                        c.execute('DELETE FROM '+table+' WHERE owner=?',(owner,))
                for marker in markers:c.execute('DELETE FROM marker_extra WHERE id=?',(marker,))
                for table in ('guest_sessions','guest_consents','privacy_requests','chunks','tracks','takes','room_state'):
                    c.execute('DELETE FROM '+table+' WHERE room=?',(room,))
            for owner in owners:
                self.guest_sockets.pop(owner,None)
                self.guest_socket_rooms.pop(owner,None)

    def repair_deleted_room_data(self):
        """Repair only demonstrably orphaned pre-fix data; keep current takes/files."""
        with self.a['_DB_LOCK'],self.a['_db_conn']() as c:
            registry={r['room']:r['created_at'] for r in c.execute('SELECT room,created_at FROM rooms')}
            sessions={(r['room'],r['session']) for r in c.execute('SELECT room,session FROM recording_sessions')}
            token_ids={r['id'] for r in c.execute('SELECT id FROM guest_tokens')}
            marker_ids={r['id'] for r in c.execute('SELECT id FROM markers')}
        with self.lock,self.db() as c:
            orphan_takes=[(r['room'],r['session']) for r in c.execute('SELECT room,session FROM takes') if (r['room'],r['session']) not in sessions]
            for room,session in orphan_takes:
                # Missing registry/session alone must not delete recoverable physical media.
                physical=self.uploads/room
                if physical.exists() and any((g/session).exists() for g in physical.iterdir()):continue
                for table in ('chunks','tracks','takes'):c.execute('DELETE FROM '+table+' WHERE room=? AND session=?',(room,session))
            stale_owners=[r['owner'] for r in c.execute('SELECT owner,invitation_id FROM guest_sessions') if r['invitation_id'] not in token_ids]
            for owner in stale_owners:
                for table in ('guest_sessions','guest_consents','admissions','guest_claims'):c.execute('DELETE FROM '+table+' WHERE owner=?',(owner,))
            for row in c.execute('SELECT id FROM marker_extra').fetchall():
                if row['id'] not in marker_ids:c.execute('DELETE FROM marker_extra WHERE id=?',(row['id'],))
            for row in c.execute('SELECT room,value FROM room_state').fetchall():
                room=row['room'];value=json.loads(row['value']);session=value.get('rec_session') or value.get('command',{}).get('session')
                gone=room not in registry and not (self.uploads/room).exists()
                stale=session and (room,session) not in sessions and not (self.uploads/room).exists()
                if gone or stale:c.execute('DELETE FROM room_state WHERE room=?',(room,))
        # An orphaned canonical invitation must never appear in the management UI.
        with self.a['_DB_LOCK'],self.a['_db_conn']() as c:
            c.execute('DELETE FROM room_invitations WHERE token_id NOT IN (SELECT id FROM guest_tokens)')
            c.commit()

    def restore_rooms(self):
        with self.db() as c:
            rows = c.execute("SELECT * FROM room_state").fetchall()
        for row in rows:
            value = json.loads(row["value"])
            r = self.a["_room"](row["room"])
            r.update(value)

    def persist_room(self, room):
        r = self.a["ROOMS"].get(room)
        if r is None:return
        value = {k: r[k] for k in ("command", "settings", "rec_state", "rec_session", "rec_started_at") if k in r}
        with self.db() as c:
            c.execute("INSERT OR REPLACE INTO room_state VALUES(?,?)", (room, json.dumps(value)))

    def track(self, room, guest, session, owner=None):
        self.take(room, session)
        with self.db() as c:
            row = c.execute("SELECT * FROM tracks WHERE room=? AND session=? AND guest=?", (room, session, guest)).fetchone()
        if not row:
            raise HTTPException(409, "Commit recording metadata before uploading chunks")
        if owner and row["owner"] != owner:
            raise HTTPException(403, "Track ownership mismatch")
        return dict(row)

    async def body(self, request, limit=65536):
        data = bytearray()
        async for block in request.stream():
            if len(data) + len(block) > limit:
                raise HTTPException(413, "Request too large")
            data.extend(block)
        return bytes(data)

    async def payload(self, request):
        try:
            value = json.loads(await self.body(request))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise HTTPException(400, "JSON object required")

    async def meta(self, room, guest, session, request: Request):
        token = self.guest(request, room, guest)
        take = self.require_session(room, session)
        data = await self.payload(request)
        if data.get("consent") is not True:
            raise HTTPException(403, "Recording consent required")
        effective = json.loads(take["profile"])
        sr = number(data.get("sample_rate", 48000), 8000, 192000, "sample_rate")
        ch = number(data.get("channels", 1), 1, 2, "channels")
        if (sr, ch) != (effective["sample_rate"], effective["channels"]):
            raise HTTPException(409, "ux.profile_changed")
        with self.db() as c:
            receipt=c.execute("SELECT digest FROM guest_consents WHERE owner=? AND room=?",(token["id"],room)).fetchone()
        if not receipt:raise HTTPException(403,"ux.consent_required")
        meta = dict(sample_rate=sr, channels=ch, profile=json.loads(take["profile"]),
                    consent=True, consent_sha256=receipt[0],
                    started_at=number(data.get("started_at", int(time.time()*1000)), 0, 9999999999999, "started_at"),
                    diagnostics_consent=bool(data.get("diagnostics_consent")))
        if self.config["collect_device_metadata"] and meta["diagnostics_consent"]:
            meta["browser"] = str(data.get("browser", "unknown"))[:160]
            meta["os"] = str(data.get("os", "unknown"))[:80]
        else:
            meta["browser"] = meta["os"] = "not collected"
        if effective.get("schema_version") == 2:
            streams = data.get("streams")
            if not isinstance(streams, dict) or set(streams) != expected_streams(effective):
                raise HTTPException(422, "ux.stream_manifest_invalid")
            clean = {}
            for logical, stream in streams.items():
                if not isinstance(stream, dict): raise HTTPException(422, "ux.stream_manifest_invalid")
                ext=stream.get("ext");mime=stream.get("mime","")
                lossless_audio=logical=='audio' and effective['audio_quality']=='lossless'
                allowed={'pcm'} if lossless_audio else {'webm','mp4'} if logical in ('video','media') else {'webm','ogg','mp4'}
                if ext not in allowed or not isinstance(mime,str) or len(mime)>200:
                    raise HTTPException(422, "ux.stream_manifest_invalid")
                base_mime=mime.split(';')[0].strip().lower()
                valid_mimes={'pcm':{'audio/pcm'},'webm':{'audio/webm','video/webm'},'mp4':{'audio/mp4','video/mp4'},'ogg':{'audio/ogg'}}
                if base_mime not in valid_mimes[ext] or logical in ('video','media') and not base_mime.startswith('video/'):
                    raise HTTPException(422, "ux.stream_manifest_invalid")
                clean[logical]=dict(ext=ext,mime=mime,requested=str(stream.get('requested',''))[:200],
                    fallback=bool(stream.get('fallback')),started_at=number(stream.get('started_at',meta['started_at']),0,9999999999999,'started_at'))
            meta['streams']=clean
        dest = self.dest(room, guest, session)
        with self.lock, self.db() as c:
            old = c.execute("SELECT meta,owner,guest FROM tracks WHERE room=? AND session=? AND guest=?", (room, session, guest)).fetchone()
            owned = c.execute("SELECT guest FROM tracks WHERE room=? AND session=? AND owner=?", (room, session, token["id"])).fetchone()
            if owned and owned["guest"] != guest:
                raise HTTPException(409, "One invitation can own only one track per session")
            if old:
                if old["owner"] != token["id"]:
                    raise HTTPException(403, "Track ownership mismatch")
                before = json.loads(old["meta"])
                if (before["sample_rate"], before["channels"]) != (sr, ch) or before.get('streams') != meta.get('streams'):
                    raise HTTPException(409, "Track format is immutable")
                return {"ok": True, "meta": before}
            c.execute("INSERT INTO tracks(room,session,guest,owner,meta,updated) VALUES(?,?,?,?,?,?)",
                      (room, session, guest, token["id"], json.dumps(meta), time.time()))
            atomic_bytes(dest / "meta.json", json_bytes(meta))
        return {"ok": True, "meta": meta}

    async def upload(self, room, guest, session, chunk, request: Request, ext: str = "pcm", logical: str = "main"):
        token = self.guest(request, room, guest)
        if not re.fullmatch(r"\d{6}", chunk) or ext not in ("pcm", "webm", "mp4", "ogg") or logical not in ("main","audio","video","media"):
            raise HTTPException(400, "Invalid chunk index or container")
        sha = request.headers.get("x-chunk-sha256", "")
        if not HASH.fullmatch(sha):
            raise HTTPException(422, "SHA-256 required")
        track = self.track(room, guest, session, token["id"])
        streams=json.loads(track['meta']).get('streams')
        if streams and (logical not in streams or ext!=streams[logical]['ext']) or not streams and logical!='main':
            raise HTTPException(422,'ux.stream_manifest_invalid')
        idx = int(chunk)
        data = await self.body(request, self.config["max_chunk_bytes"])
        if not data or hashlib.sha256(data).hexdigest() != sha:
            raise HTTPException(422, "Chunk checksum mismatch or empty chunk")
        if ext == "pcm" and len(data) % (2 * json.loads(track["meta"])["channels"]):
            raise HTTPException(422, "Partial PCM frame")
        with self.lock, self.db() as c:
            self.take(room, session)
            track=self.track(room,guest,session,token['id'])
            old = c.execute("SELECT * FROM chunks WHERE room=? AND session=? AND guest=? AND logical=? AND idx=?", (room,session,guest,logical,idx)).fetchone()
            path = self.chunk_path(room,guest,session,logical,idx,ext)
            if old:
                if (old["sha"], old["size"], old["ext"]) != (sha, len(data), ext):
                    raise HTTPException(409, "Conflicting duplicate chunk; original preserved")
                c.execute("UPDATE chunks SET attempts=attempts+1 WHERE room=? AND session=? AND guest=? AND logical=? AND idx=?", (room,session,guest,logical,idx))
                if not path.exists() or digest(path) != sha:
                    atomic_bytes(path, data)
                return dict(ok=True, logical=logical, idx=idx, bytes=len(data), sha256=sha, duplicate=True)
            if track["finalized"] or track["expected"] is not None:
                raise HTTPException(409, "Track is sealed")
            total = c.execute("SELECT COALESCE(SUM(size),0) FROM chunks WHERE room=? AND session=? AND guest=?", (room,session,guest)).fetchone()[0]
            if total + len(data) > self.config["max_track_bytes"]:
                raise HTTPException(413, "Track storage limit reached")
            if shutil.disk_usage(self.root).free < self.config["min_free_bytes"] + len(data):
                raise HTTPException(507, "Storage reserve reached; preserve local recording")
            atomic_bytes(path, data)
            c.execute("INSERT INTO chunks(room,session,guest,logical,idx,size,sha,ext) VALUES(?,?,?,?,?,?,?,?)",
                      (room,session,guest,logical,idx,len(data),sha,ext))
            c.execute("UPDATE tracks SET updated=? WHERE room=? AND session=? AND guest=?", (time.time(),room,session,guest))
        return dict(ok=True, logical=logical, idx=idx, bytes=len(data), sha256=sha, duplicate=False)

    def chunk_path(self,room,guest,session,logical,idx,ext):
        prefix='chunk-' if logical=='main' else 'chunk-'+logical+'-'
        return self.dest(room,guest,session)/f"{prefix}{idx:06d}.{ext}"

    def manifest(self, room, guest, session, request: Request):
        token = self.guest(request,room,guest)
        t = self.track(room,guest,session,token["id"])
        with self.db() as c:
            rows = c.execute("SELECT logical,idx,size,sha,ext,attempts FROM chunks WHERE room=? AND session=? AND guest=? ORDER BY logical,idx", (room,session,guest)).fetchall()
        # Only acknowledge bytes that still exist and pass validation, not stale DB receipts.
        valid=[]
        for row in rows:
            path=self.chunk_path(room,guest,session,row['logical'],row['idx'],row['ext'])
            if path.exists() and path.stat().st_size==row['size'] and digest(path)==row['sha']:valid.append(dict(row))
        return dict(ok=True, expected=t["expected"],stream_expected=json.loads(t['stream_expected']) if t['stream_expected'] else None,
                    finalized=bool(t["finalized"]),chunks=valid,report=json.loads(t["report"]) if t["report"] else None)

    def build(self, room, guest, session, repair=False):
        return build_recording(self,room,guest,session,repair)

    def ffmpeg(self,args):
        try:
            result=subprocess.run([self.a["FFMPEG"],"-y","-nostdin","-v","error",*args],capture_output=True,timeout=1800)
        except (OSError,subprocess.TimeoutExpired):
            raise HTTPException(503,"Media processor unavailable or timed out")
        if result.returncode:
            raise HTTPException(422,"Media conversion failed; original chunks preserved")

    async def finish(self, room, guest, session, request: Request):
        token=self.guest(request,room,guest)
        data=await self.payload(request)
        with self.lock:
            t=self.track(room,guest,session,token["id"])
            meta=json.loads(t['meta'])
            names=set(meta.get('streams',{'main':{}}))
            counts=data.get('expected_streams') if meta.get('streams') else {'main':data.get('expected_chunks')}
            if not isinstance(counts,dict) or set(counts)!=names:raise HTTPException(422,'ux.stream_manifest_invalid')
            counts={k:number(v,1,999999,'expected_chunks') for k,v in counts.items()}
            count=sum(counts.values())
            if t['finalized']:
                before=json.loads(t['stream_expected']) if t['stream_expected'] else {'main':t['expected']}
                if counts!=before:raise HTTPException(409,'ux.stream_manifest_conflict')
                return dict(ok=True,finalized=True,report=json.loads(t['report']))
            if t['stream_expected'] and json.loads(t['stream_expected'])!=counts:
                raise HTTPException(409,'ux.stream_manifest_conflict')
            with self.db() as c:
                for logical,n in counts.items():
                    got=c.execute('SELECT idx FROM chunks WHERE room=? AND session=? AND guest=? AND logical=? ORDER BY idx',(room,session,guest,logical)).fetchall()
                    if [r[0] for r in got]!=list(range(n)):
                        raise HTTPException(409,dict(reason='missing_chunks',logical=logical,expected=n,received=len(got)))
                c.execute('UPDATE tracks SET expected=?,stream_expected=?,interrupted=? WHERE room=? AND session=? AND guest=?',
                    (count,json.dumps(counts),int(bool(data.get('interrupted'))),room,session,guest))
        async with self.jobs:
            report=await asyncio.to_thread(self.build,room,guest,session)
        await self.a["_broadcast_host_status"](room)
        return dict(ok=True,finalized=True,report=report)

    async def telemetry(self,room,guest,session,request: Request):
        t=self.guest(request,room,guest)
        self.track(room,guest,session,t["id"])
        p=await self.payload(request)
        allowed=("local_bytes","uploaded_bytes","backlog_bytes","retry_count","eta_s","peak_dbfs","clip_count","state")
        clean={k:p[k] for k in allowed if k in p and (isinstance(p[k],str) and len(p[k])<100 or isinstance(p[k],(float,int)) and math.isfinite(p[k]))}
        clean["events"]=[dict(type=str(e.get("type",""))[:50],at=e.get("at")) for e in p.get("events",[])[:200]]
        clean["seen_at"]=time.time()
        with self.db() as c:
            c.execute("UPDATE tracks SET telemetry=? WHERE room=? AND session=? AND guest=?",(json.dumps(clean),room,session,guest))
        return {"ok":True}

    def markers(self,room,session):
        rows=self.a["_marker_list"](room,session)
        with self.db() as c:
            extra={r["id"]:dict(r) for r in c.execute("SELECT * FROM marker_extra")}
        return [dict(r,**{k:v for k,v in extra.get(r["id"],{}).items() if k!="id"}) for r in rows]

    def marker_locked(self,id_):
        with self.db() as c:
            row=c.execute("SELECT locked FROM marker_extra WHERE id=?",(id_,)).fetchone()
        return bool(row and row[0])

    async def marker_edit(self,id_,request: Request):
        role=self.role(request)
        p=await self.payload(request)
        with self.a["_db_conn"]() as c:
            m=c.execute("SELECT * FROM markers WHERE id=?",(id_,)).fetchone()
        if not m:
            raise HTTPException(404,"Marker not found")
        if self.a["_session_in_use"](m["room"],m["session"]) and any(k in p for k in ("locked","approved")):
            raise HTTPException(409,"Stop recording before approval/locking")
        if self.marker_locked(id_) and not (role=="admin" and p.get("locked") is False):
            raise HTTPException(423,"Marker locked")
        with self.lock,self.db() as c:
            row=c.execute("SELECT * FROM marker_extra WHERE id=?",(id_,)).fetchone()
            value=dict(row) if row else dict(id=id_,label="",color="#30a46c",locked=0,approved=0)
            for key in ("label","color","locked","approved"):
                if key in p:
                    value[key]=p[key]
            value["label"]=str(value["label"])[:200]
            if not re.fullmatch(r"#[0-9a-fA-F]{6}",str(value["color"])):
                raise HTTPException(422,"Invalid marker color")
            c.execute("INSERT OR REPLACE INTO marker_extra VALUES(?,?,?,?,?)",(id_,value["label"],value["color"],int(bool(value["locked"])),int(bool(value["approved"]))))
        self.audit(role,"marker.edit",id_)
        return {"ok":True}

    def marker_export_bytes(self,room,session,fmt):
        rows=self.markers(room,session)
        if fmt=="json":
            return json_bytes(rows),"application/json"
        if fmt=="csv":
            s=io.StringIO();w=csv.writer(s);w.writerow(["offset_ms","category","label","note","color","approved"])
            def cell(v):
                v=str(v)
                return "'"+v if v.startswith(("=","+","-","@","\t","\r")) else v
            for m in rows:
                w.writerow([m["offset_ms"],cell(m["kind"]),cell(m.get("label","")),cell(m.get("note","")),m.get("color",""),bool(m.get("approved"))])
            return ("\ufeff"+s.getvalue()).encode(),"text/csv"
        if fmt=="audacity":
            return "".join(f"{m['offset_ms']/1000:.3f}\t{m['offset_ms']/1000:.3f}\t{str(m.get('label') or m.get('note') or m['kind']).replace(chr(10),' ').replace(chr(9),' ')}\n" for m in rows).encode(),"text/plain"
        if fmt=="chapters":
            def esc(s):
                return re.sub(r"([\\=;#])",r"\\\1",str(s)).replace("\n"," ")
            with self.a["_db_conn"]() as c:
                t=c.execute("SELECT start_at,stop_at FROM recording_sessions WHERE room=? AND session=?",(room,session)).fetchone()
            end=max(0,t["stop_at"]-t["start_at"]) if t and t["stop_at"] and t["start_at"] else 0
            out=[";FFMETADATA1\n"]
            for i,m in enumerate(rows):
                stop=rows[i+1]["offset_ms"] if i+1<len(rows) else end
                if stop>m["offset_ms"]:
                    out.append(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={m['offset_ms']}\nEND={stop}\ntitle={esc(m.get('label') or m.get('note') or m['kind'])}\n")
            return "".join(out).encode(),"text/plain"
        raise HTTPException(422,"Unknown export format")

    def export_markers(self,room,session,request: Request,format: str="json"):
        self.role(request);self.ident(room,session)
        data,mime=self.marker_export_bytes(room,session,format)
        return Response(data,media_type=mime,headers={"Content-Disposition":f'attachment; filename="markers-{session}.{format if format in ("csv","json") else "txt"}"'})

    def overview(self,request: Request,room: str=""):
        self.role(request)
        if room:self.ident(room)
        with self.db() as c:
            takes=[dict(r) for r in c.execute("SELECT * FROM takes WHERE deleted=0 AND (?='' OR room=?) ORDER BY created DESC LIMIT 500",(room,room))]
            tracks=[dict(r) for r in c.execute("SELECT t.* FROM tracks t JOIN takes s ON s.room=t.room AND s.session=t.session WHERE s.deleted=0 AND (?='' OR t.room=?)",(room,room))]
        for s in takes:
            s["profile"]=json.loads(s["profile"])
            s["tracks"]=[]
            for t in tracks:
                if (t["room"],t["session"])==(s["room"],s["session"]):
                    s["tracks"].append(dict(guest=t["guest"],expected=t["expected"],finalized=bool(t["finalized"]),
                                            report=json.loads(t["report"]) if t["report"] else None,telemetry=json.loads(t["telemetry"])))
            states=[t["report"]["status"] if t["report"] else "incomplete" for t in s["tracks"]]
            s["status"]="incomplete" if not states or "incomplete" in states else "complete_with_warnings" if "complete_with_warnings" in states else "complete"
            # Offline invitees with no track are not silently reported as complete.
            s["markers"]=self.markers(s["room"],s["session"])
        return {"ok":True,"sessions":takes,"config":{k:self.config[k] for k in ("profiles","marker_categories","default_profile")}}

    async def session_edit(self,room,session,request: Request):
        role=self.role(request);self.take(room,session,create=True)
        p=await self.payload(request)
        fields={k:str(p[k])[:(4000 if k=="description" else 200)] for k in ("title","description","episode","editorial") if k in p}
        if fields.get("editorial","draft") not in ("draft","review","approved","published"):
            raise HTTPException(422,"Invalid editorial status")
        if fields:
            with self.db() as c:
                c.execute("UPDATE takes SET "+",".join(k+"=?" for k in fields)+" WHERE room=? AND session=?",(*fields.values(),room,session))
        self.audit(role,"session.edit",f"{room}/{session}")
        return {"ok":True}

    def recording_file(self,room,guest,session,filename,request: Request):
        self.role(request,True)
        t=self.track(room,guest,session)
        report=json.loads(t['report']) if t['report'] else {}
        if filename=='quality.json' and t['finalized']:pass
        elif filename not in report.get('files',{}):raise HTTPException(404)
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+',filename):raise HTTPException(404)
        path=self.dest(room,guest,session)/filename
        if not path.is_file() or path.is_symlink():raise HTTPException(404)
        return FileResponse(path,filename=f'{room}_{guest}_{session}_{filename}')

    def package(self,room,session):
        with self.lock:
            take=self.take(room,session,create=True)
            if self.a["_session_in_use"](room,session):
                raise HTTPException(409,"Session still active")
            paths=[]
            root=self.uploads/room
            for guest in root.iterdir() if root.exists() else []:
                d=guest/session
                if not guest.is_dir() or not d.is_dir():continue
                for p in sorted(d.iterdir()):
                    if p.is_file() and not p.is_symlink() and not p.name.startswith("."):
                        paths.append((p,f"tracks/{guest.name}/{p.name}"))
            if not paths:raise HTTPException(404,"No recording data")
            folder=self.root/"exports";folder.mkdir(exist_ok=True)
            tmp=folder/(secrets.token_hex(12)+".zip")
            sums={}
            with zipfile.ZipFile(tmp,"w",zipfile.ZIP_STORED,allowZip64=True) as z:
                for p,name in paths:
                    sums[name]=digest(p);z.write(p,name)
                take["profile"]=json.loads(take["profile"])
                contents={"metadata.json":json_bytes(take)}
                for fmt in ("json","csv","audacity","chapters"):
                    contents["markers."+fmt]=self.marker_export_bytes(room,session,fmt)[0]
                mix=self.a["_ensure_session_mixdown"](room,session)
                if mix:
                    sums["mixdown.mp3"]=digest(mix);z.write(mix,"mixdown.mp3")
                for name,data in contents.items():
                    sums[name]=hashlib.sha256(data).hexdigest();z.writestr(name,data)
                z.writestr("SHA256SUMS", "".join(f"{sha}  {name}\n" for name,sha in sorted(sums.items())))
            sha=digest(tmp)
            self.audit("admin","session.export",f"{room}/{session}",dict(sha256=sha,bytes=tmp.stat().st_size))
            return tmp,sha

    async def export_session(self,room,session,request: Request):
        self.role(request,True)
        async with self.jobs:
            path,sha=await asyncio.to_thread(self.package,room,session)
        return FileResponse(path,filename=f"{room}-{session}.zip",headers={"X-Checksum-SHA256":sha},background=BackgroundTask(path.unlink,missing_ok=True))

    def delete_take(self,room,session,actor):
        self.ident(room,session)
        with self.lock:
            self.take(room,session,create=True)
            if self.a["_session_in_use"](room,session):
                raise HTTPException(409,"Cannot delete an active or uploading session")
            # Durable tombstone is the atomic visibility/authorization boundary.
            # A crash during physical cleanup is completed by maintenance on restart.
            with self.db() as c:
                c.execute("UPDATE takes SET deleted=1 WHERE room=? AND session=?",(room,session))
            self.purge_take(room,session)
            self.audit(actor,"session.delete",f"{room}/{session}")

    def purge_take(self,room,session):
        root=self.uploads/room
        for g in list(root.iterdir()) if root.exists() else []:
            d=g/session
            if g.is_dir() and d.exists():shutil.rmtree(d)
        self.a["_mixdown_path"](room,session).unlink(missing_ok=True)
        with self.a["_db_conn"]() as c:
            ids=[r[0] for r in c.execute("SELECT id FROM markers WHERE room=? AND session=?",(room,session))]
            for table in ("markers","recording_sessions","guest_logs","clip_events"):
                c.execute(f"DELETE FROM {table} WHERE room=? AND session=?",(room,session))
            c.commit()
        with self.db() as c:
            for id_ in ids:c.execute("DELETE FROM marker_extra WHERE id=?",(id_,))
            c.execute("DELETE FROM chunks WHERE room=? AND session=?",(room,session))
            c.execute("DELETE FROM tracks WHERE room=? AND session=?",(room,session))
            c.execute("UPDATE takes SET title='',description='',episode='' WHERE room=? AND session=?",(room,session))

    async def action(self,room,session,action,request: Request):
        role=self.role(request,True);self.ident(room,session)
        self.take(room,session)
        if action=="delete":
            await asyncio.to_thread(self.delete_take,room,session,role)
        elif action in ("rebuild","mixdown"):
            if self.a["_session_in_use"](room,session):raise HTTPException(409,"Session active")
            async with self.jobs:
                if action=="rebuild":
                    with self.db() as c: guests=[r[0] for r in c.execute("SELECT guest FROM tracks WHERE room=? AND session=?",(room,session))]
                    if not guests:raise HTTPException(404,"No manifested tracks")
                    for guest in guests:await asyncio.to_thread(self.build,room,guest,session,True)
                def mix():
                    with self.lock:return self.a["_ensure_session_mixdown"](room,session,force=True)
                path=await asyncio.to_thread(mix)
                if not path:raise HTTPException(422,"Mixdown failed; tracks preserved")
                if self.config["automatic_chapters"]:
                    chapter=path.with_suffix(".ffmetadata")
                    atomic_bytes(chapter,self.marker_export_bytes(room,session,"chapters")[0])
                    out=path.with_name("chapters.tmp.mp3")
                    await asyncio.to_thread(self.ffmpeg,["-i",str(path),"-i",str(chapter),"-map_metadata","1","-map_chapters","1","-codec","copy",str(out)])
                    os.replace(out,path)
                self.audit(role,"session."+action,f"{room}/{session}",dict(sha256=digest(path)))
        else:raise HTTPException(404,"Unknown action")
        return {"ok":True}

    def settings_get(self,request: Request):
        self.role(request,True)
        return {"ok":True,"config":self.config}

    async def settings_set(self,request: Request):
        role=self.role(request,True);p=await self.payload(request)
        upload_keys=set(self.upload_policy())
        if set(p)-upload_keys and any(r.get("rec_state") == "recording" for r in self.a["ROOMS"].values()):
            raise HTTPException(409, "ux.recording_busy")
        cfg=json.loads(json.dumps(self.config))
        if any(k not in DEFAULTS for k in p):raise HTTPException(422,"Unknown setting")
        cfg.update(p)
        bounds=dict(retention_days=(0,3650),diagnostic_days=(1,365),max_guests=(1,100),max_duration_s=(60,86400),
                    max_chunk_bytes=(65536,268435456),max_track_bytes=(1048576,1099511627776),min_free_bytes=(0,1099511627776),
                    requests_per_minute=(30,10000),token_requests_per_minute=(30,10000),admission_minutes=(5,43200))
        for k,(lo,hi) in bounds.items():cfg[k]=number(cfg[k],lo,hi,k)
        try:
            for k,lo,hi in (("upload_kbps",32,100000),("upload_min_kbps",0,80000)):
                cfg[k]=number(cfg[k],lo,hi,k)
            for k in ("upload_throttle_enabled","pause_uploads_during_call","pause_uploads_during_recording"):
                if not isinstance(cfg[k],bool):raise ValueError(k)
        except (HTTPException,ValueError):
            raise HTTPException(422,"ux.upload_invalid_settings") from None
        if cfg["upload_throttle_enabled"] and not cfg["pause_uploads_during_call"] and cfg["upload_min_kbps"]*1.25>cfg["upload_kbps"]:
            raise HTTPException(422,"ux.upload_invalid_threshold")
        for k in ("collect_device_metadata","automatic_chapters"):
            if not isinstance(cfg[k],bool):raise HTTPException(422,f"{k}: boolean required")
        for k in ("consent_text","consent_text_en","privacy_policy","storage_region","backup_policy"):
            if not isinstance(cfg[k],str) or len(cfg[k])>12000:raise HTTPException(422,f"Invalid {k}")
        if not cfg["consent_text"].strip() or not cfg["consent_text_en"].strip():raise HTTPException(422,"ux.consent_empty")
        for k in ("consent_text","consent_text_en"):cfg[k]=cfg[k].strip()
        if not isinstance(cfg["profiles"],list) or not 1<=len(cfg["profiles"])<=20:raise HTTPException(422,"1..20 profiles required")
        ids=set()
        try:cfg['profiles']=[normalize_profile(pro) for pro in cfg['profiles']]
        except ValueError:raise HTTPException(422,'ux.profile_invalid') from None
        for pro in cfg['profiles']:
            self.ident(pro.get('id',''))
            if pro['id'] in ids:raise HTTPException(422,'ux.profile_invalid')
            ids.add(pro['id'])
            if not isinstance(pro.get('name'),str) or not 1<=len(pro['name'])<=100:raise HTTPException(422,'ux.profile_invalid')
        if cfg["default_profile"] not in ids:raise HTTPException(422,"Unknown default profile")
        cats=cfg["marker_categories"]
        if not isinstance(cats,list) or not 1<=len(cats)<=30:raise HTTPException(422,"1..30 marker categories required")
        seen=set()
        for cat in cats:
            self.ident(cat.get("id",""))
            if cat["id"] in seen or not re.fullmatch(r"#[0-9a-fA-F]{6}",str(cat.get("color",""))):raise HTTPException(422,"Invalid category")
            seen.add(cat["id"])
            if not isinstance(cat.get("label"),str) or not 1<=len(cat["label"])<=100:raise HTTPException(422,"Invalid category label")
        with self.lock,self.db() as c:
            if set(p)-upload_keys and any(r.get("rec_state") == "recording" for r in self.a["ROOMS"].values()):
                raise HTTPException(409, "ux.recording_busy")
            c.execute("INSERT OR REPLACE INTO settings VALUES(1,?)",(json.dumps(cfg),))
            self.config=cfg
            self.a["MARKER_KINDS"]=set(x["id"] for x in cats)
        self.audit(role,"settings.update",detail={"fields":list(p)})
        return {"ok":True,"config":cfg}

    def upload_policy(self):
        return {k:self.config[k] for k in ("upload_kbps","upload_min_kbps","upload_throttle_enabled",
                "pause_uploads_during_call","pause_uploads_during_recording")}

    def consent_policy(self,room):
        """A single authoritative localized text/digest snapshot for display and receipt."""
        config=self.config
        locale="en" if self.a["_room_locale"](room)=="en" else "de"
        text=config["consent_text_en" if locale=="en" else "consent_text"]
        return dict(consent_text=text,consent_locale=locale,consent_digest=hashlib.sha256(text.encode()).hexdigest())

    async def consent(self, room, request: Request):
        token = self.guest(request, room, admission=True)
        p = await self.payload(request)
        if p.get("accepted") is not True:
            raise HTTPException(422, "ux.consent_required")
        with self.lock,self.db() as c:
            policy=self.consent_policy(room)
            supplied=p.get("digest")
            if not isinstance(supplied,str) or not HASH.fullmatch(supplied) or not hmac.compare_digest(supplied,policy["consent_digest"]):
                raise HTTPException(409,"ux.consent_changed")
            c.execute("INSERT OR REPLACE INTO guest_consents VALUES(?,?,?,?)",
                      (token["id"], room, policy["consent_digest"], time.time()))
        return {"ok": True}

    def bootstrap(self,room,request: Request):
        token=self.guest(request,room,admission=True)
        r=self.a["ROOMS"].get(room,{})
        pro=self.profile(room)
        if r.get("rec_session") and r.get("rec_state")=="recording":
            t=self.take(room,r["rec_session"])
            if t:pro=json.loads(t["profile"])
        with self.lock:
            policy={k:self.config[k] for k in ("privacy_policy","retention_days","storage_region","backup_policy","collect_device_metadata","upload_kbps","pause_uploads_during_recording","max_duration_s")}
            policy.update(self.upload_policy())
            policy.update(self.consent_policy(room))
        return dict(ok=True,guest="g_"+token["id"],profile=pro,policy=policy)


    def operations(self,request: Request):
        self.role(request,True)
        with self.db() as c:
            audit=[dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200")]
            privacy=[dict(r) for r in c.execute("SELECT * FROM privacy_requests ORDER BY created DESC LIMIT 200")]
            failed=[dict(r) for r in c.execute("SELECT room,session,guest,updated FROM tracks WHERE finalized=0 AND updated<?",(time.time()-300,))]
        free=shutil.disk_usage(self.root).free
        return dict(ok=True,free_bytes=free,storage_alert=free<self.config["min_free_bytes"]*2,stalled_uploads=failed,audit=audit,privacy_requests=privacy,production=self.production)

    async def privacy_resolve(self,id_,request: Request):
        role=self.role(request,True);p=await self.payload(request)
        with self.db() as c:r=c.execute("SELECT * FROM privacy_requests WHERE id=?",(id_,)).fetchone()
        if not r:raise HTTPException(404,"Request not found")
        with self.db() as c:tracks=[dict(x) for x in c.execute("SELECT room,session,guest FROM tracks WHERE owner=?",(r["owner"],))]
        if r["kind"]=="delete":
            if p.get("confirm_delete_sessions") is not True:raise HTTPException(409,"Deletion includes shared mixes and full sessions; explicit approval required")
            sessions=sorted({(t["room"],t["session"]) for t in tracks})
            if any(self.a["_session_in_use"](*s) for s in sessions):raise HTTPException(409,"A recording is active")
            for room,s in sessions:await asyncio.to_thread(self.delete_take,room,s,role)
            self.revoke_guest_owner(r["owner"])
        else:
            # Admin downloads identified session packages and supplies them securely to requester.
            if p.get("delivered") is not True:
                return {"ok":True,"state":"awaiting_delivery","sessions":tracks}
        with self.db() as c:c.execute("UPDATE privacy_requests SET state='completed' WHERE id=?",(id_,))
        self.audit(role,"privacy.complete",id_)
        return {"ok":True}

    async def security_action(self,action,request: Request):
        self.role(request,True)
        if action not in ("invalidate","rotate"):raise HTTPException(404,"Unknown action")
        with self.lock:
            if action=="rotate":
                if os.getenv("SESSION_SECRET"):
                    raise HTTPException(409,"Externally managed secret: rotate in your secret manager and restart")
                directory=self.root/"secret-backups";directory.mkdir(mode=0o700,exist_ok=True);os.chmod(directory,0o700)
                backup=directory/(str(int(time.time()))+"-"+secrets.token_hex(4)+".key")
                atomic_bytes(backup,self.a["SESSION_SECRET"].encode());os.chmod(backup,0o600)
                value=secrets.token_urlsafe(48)
                atomic_bytes(self.root/"session_secret",value.encode());os.chmod(self.root/"session_secret",0o600)
                self.a["SESSION_SECRET"]=value
                self.a["_SIGNER"]=self.a["TimestampSigner"](value,salt="podcast-session")
            with self.db() as c:c.execute("UPDATE security SET epoch=? WHERE id=1",(secrets.token_hex(16),))
        self.audit("admin","security."+action)
        return {"ok":True,"reauthenticate":True}

    async def maintenance(self):
        last_cleanup=0
        while True:
            try:
                for room,r in list(self.a["ROOMS"].items()):
                    if r.get("rec_state")=="recording":
                        t=self.take(room,r["rec_session"])
                        pro=json.loads(t["profile"]) if t else self.profile(room)
                        deadline=r.get("rec_started_at",time.time()*1000)/1000+min(pro["max_duration_s"],self.config["max_duration_s"])
                        if time.time()>=deadline:
                            self.a["_apply_trigger"](room,"stop",force=True)
                            await self.a["_broadcast_guests"](room)
                    self.persist_room(room)
                if time.time()-last_cleanup>3600:
                    await asyncio.to_thread(self.cleanup)
                    last_cleanup=time.time()
            except Exception:
                self.a["_error_record"]("studio","Maintenance failed; inspect storage and database")
            await asyncio.sleep(5)

    def cleanup(self):
        with self.lock:
            with self.db() as c:
                deleted=[tuple(r) for r in c.execute("SELECT room,session FROM takes WHERE deleted=1")]
                stale=[tuple(r) for r in c.execute("SELECT room,session FROM takes WHERE deleted=0 AND created<?",(time.time()-self.config["retention_days"]*86400,))] if self.config["retention_days"] else []
                cutoff=time.time()-self.config["diagnostic_days"]*86400
                c.execute("UPDATE tracks SET telemetry='{}' WHERE updated<?",(cutoff,))
                c.execute("DELETE FROM audit WHERE ts<?",(cutoff,))
                c.execute("DELETE FROM privacy_requests WHERE state='completed' AND created<?",(cutoff,))
            for room,s in deleted:self.purge_take(room,s)
            for room,s in stale:
                if not self.a["_session_in_use"](room,s):self.delete_take(room,s,"retention")
            for p in (self.root/"exports").glob("*.zip") if (self.root/"exports").exists() else []:
                if p.stat().st_mtime<time.time()-86400:p.unlink(missing_ok=True)


class SecurityMiddleware:
    """ASGI origin, TLS, revocation, resource and tombstone enforcement."""
    def __init__(self, app, studio):self.app=app;self.s=studio
    async def __call__(self,scope,receive,send):
        if scope["type"] not in ("http","websocket"):return await self.app(scope,receive,send)
        s=self.s;headers=dict(scope.get("headers",[]));path=scope.get("path","");ws=scope["type"]=="websocket"
        async def deny(code,text):
            if ws:await send({"type":"websocket.close","code":4403})
            else:await JSONResponse({"detail":text},status_code=code)(scope,receive,send)
        if s.production and scope.get("scheme") not in ("https","wss"):
            return await deny(426,"HTTPS/WSS required")
        host=headers.get(b"host",b"").decode()
        origin=headers.get(b"origin",b"").decode()
        allowed=s.origins or {("https" if scope.get("scheme") in ("https","wss") else "http")+"://"+host}
        if origin and origin not in allowed and path!='/api/jitsi/webhook':return await deny(403,"Origin denied")
        if s.production and not s.origins:return await deny(503,"Configure STUDIO_ALLOWED_ORIGINS")
        if scope.get("method") in ("POST","PUT","PATCH","DELETE") and not origin and b"cookie" in headers and headers.get(b"sec-fetch-site")==b"cross-site":
            return await deny(403,"Cross-site write denied")
        try:
            s.rate_check("ip:"+str(scope.get("client",("unknown",))[0]),s.config["requests_per_minute"])
        except HTTPException as e:return await deny(e.status_code,e.detail)
        auth_role=None;token=None;value=""
        if ws:
            from http.cookies import SimpleCookie
            jar=SimpleCookie();jar.load(headers.get(b"cookie",b"").decode())
            cookie=jar.get(s.a["COOKIE_NAME"])
            if path.startswith("/ws/host/"):
                auth_role=s.a["_session_role"](cookie.value if cookie else "")
                if not auth_role:return await deny(401,"Authentication required")
            elif path.startswith("/ws/guest/"):
                q=parse_qs(scope.get("query_string",b"").decode());value=q.get("token",[""])[0]
                token=s.resolve_guest(value)
                parts=path.split("/")
                if value and (not token or token["room"]!=parts[-2] or parts[-1]!="g_"+token["id"]):return await deny(403,"Invalid guest identity")
                if not IDENT.fullmatch(parts[-2]) or not IDENT.fullmatch(parts[-1]):return await deny(403,"Invalid guest identity")
                guests=s.a["ROOMS"].get(parts[-2],{}).get("guests",{})
                active=sum(time.time()-g.get("last_seen",0)<120 for g in guests.values())
                if parts[-1] not in guests and active>=s.config["max_guests"]:return await deny(429,"Room full")
        # Deny legacy writes to immutable approved markers and destructive bypasses.
        request=Request(scope) if not ws else None
        if not ws:
            match=re.fullmatch(r"/host/marker/([0-9a-f]{8})(?:/(?:note|shift))?",path)
            if match and scope["method"]!="GET" and s.marker_locked(match[1]):return await deny(423,"Marker locked")
            if path.startswith(("/uploads/","/download/")):
                try:s.role(request,True)
                except HTTPException as e:return await deny(e.status_code,e.detail)
            parts=path.split("/")
            candidates=[]
            if len(parts)>=5 and parts[1] in ("download","uploads","meta","finish","upload"):
                candidates=[(parts[2],parts[4])]
            if len(parts)>=6 and parts[1:3]==["admin","preview"]:candidates=[(parts[3],parts[5])]
            if len(parts)>=5 and parts[1:3] in (["host","mixdown"],["admin","session-export"]):candidates=[(parts[3],parts[4])]
            for room,session in candidates:
                try:s.take(room,session)
                except HTTPException as e:return await deny(e.status_code,e.detail)
        original_receive=receive
        count=0;window=time.monotonic();body_bytes=0
        async def guarded_receive():
            nonlocal count,window,body_bytes,token,value
            msg=await original_receive()
            if msg["type"]=="http.request":
                body_bytes+=len(msg.get("body",b""))
                cap=s.config["max_chunk_bytes"] if path.startswith("/upload/") else 8*1024*1024
                if body_bytes>cap:raise HTTPException(413,"Request too large")
            if msg["type"]=="websocket.receive":
                size=len(msg.get("text","") or "")+len(msg.get("bytes",b"") or b"")
                if time.monotonic()-window>=1:window=time.monotonic();count=0
                count+=1
                if size>65536:
                    from starlette.websockets import WebSocketDisconnect
                    await send({'type':'websocket.close','code':4403});raise WebSocketDisconnect(4403)
                if path.startswith('/ws/guest/') and token is None:
                    try:
                        frame=json.loads(msg.get('text') or msg.get('bytes') or b'{}')
                        value=frame.get('token','') if frame.get('type')=='hello' else ''
                        token=s.resolve_guest(value)
                    except (ValueError,TypeError,AttributeError):token=None
                    if not token or token['room']!=path.split('/')[-2] or 'g_'+token['id']!=path.split('/')[-1]:
                        from starlette.websockets import WebSocketDisconnect
                        await send({'type':'websocket.close','code':4403});raise WebSocketDisconnect(4403)
                revoked=token and not s.resolve_guest(value)
                expired=auth_role and not s.a["_session_role"](cookie.value if cookie else "")
                if size>65536 or count>40 or revoked or expired:
                    from starlette.websockets import WebSocketDisconnect
                    await send({"type":"websocket.close","code":4403})
                    raise WebSocketDisconnect(4403)
            return msg
        async def guarded_send(msg):
            if msg["type"]=="http.response.start":
                h=list(msg.get("headers",[]))
                policy="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self'"
                extra = " ".join(getattr(s,"jitsi_connect_sources",[]))
                if extra: policy=policy.replace("connect-src 'self'", "connect-src 'self' "+extra)
                permissions = "camera=(self), microphone=(self), geolocation=()"
                # Delegate only to the selected HTTPS origin on call pages; never on Admin.
                public_origin = getattr(s, "jitsi_embed_origin", lambda: "")()
                sdk_url = getattr(s, "jitsi_sdk_url", lambda: public_origin + "/external_api.js")()
                if public_origin and sdk_url and path in ("/host", "/host.html", "/recorder.html"):
                    policy = policy.replace("script-src 'self'", "script-src 'self' " + sdk_url)
                    policy += "; frame-src " + public_origin
                    permissions = 'camera=(self "' + public_origin + '"), microphone=(self "' + public_origin + '"), geolocation=()'
                else:
                    policy += "; frame-src 'none'"
                h.extend([(b"x-content-type-options",b"nosniff"),(b"referrer-policy",b"no-referrer"),(b"x-frame-options",b"SAMEORIGIN"),(b"content-security-policy",policy.encode()),(b"cache-control",b"private, no-store"),(b"permissions-policy",permissions.encode())])
                if s.production:h.append((b"strict-transport-security",b"max-age=31536000"))
                for i,(key,val) in enumerate(h):
                    if key.lower()==b"set-cookie" and s.production and b"secure" not in val.lower():h[i]=(key,val+b"; Secure")
                msg=dict(msg,headers=h)
                if scope.get("method") in ("POST","PUT","PATCH","DELETE") and path.startswith(("/admin/","/host/","/rooms")) and msg["status"]<400:
                    s.audit("authenticated","http."+scope["method"],path)
            await send(msg)
        return await self.app(scope,guarded_receive,guarded_send)


def install(api):
    s=Studio(api);app=api["app"]
    resolve_token = api["_token_resolve"]
    def resolve_with_parent(value):
        token = resolve_token(value)
        return token if token and s.token_parent_valid(token) else None
    api["_token_resolve"] = resolve_with_parent
    # Epoch-based cookie invalidation also covers already-open host sockets.
    def make_cookie(role="admin"):
        with s.db() as c:epoch=c.execute("SELECT epoch FROM security WHERE id=1").fetchone()[0]
        return api["_SIGNER"].sign(f"role:{role}:{epoch}").decode()
    def role(token):
        try:
            raw=api["_SIGNER"].unsign(token,max_age=api["SESSION_MAX_AGE"]).decode().split(":")
            with s.db() as c:epoch=c.execute("SELECT epoch FROM security WHERE id=1").fetchone()[0]
            return raw[1] if len(raw)==3 and raw[0]=="role" and raw[1] in ("host","admin") and hmac.compare_digest(raw[2],epoch) else None
        except Exception:return None
    api["_make_session_cookie"]=make_cookie;api["_session_role"]=role
    original_trigger=api["_apply_trigger"]
    def trigger(room,action,issued_at=None,force=False):
        if action=="start":
            if shutil.disk_usage(s.root).free<s.config["min_free_bytes"]:
                return dict(ok=False,blocked=True,reason="storage_low",detail="Storage reserve reached",command=None)
            r=api["_room"](room)

        with s.lock:
            result=original_trigger(room,action,issued_at,force)
        if result.get("ok"):
            cmd=result.get("command") or {}
            if cmd.get("session"):s.take(room,cmd["session"],create=True)
            s.persist_room(room)
            s.audit("host","recording."+action,room)
        return result
    api["_apply_trigger"]=trigger
    api["MARKER_KINDS"]=set(c["id"] for c in s.config["marker_categories"])
    app.add_middleware(SecurityMiddleware,studio=s)
    app.add_api_route("/upload/{room}/{guest}/{session}/{chunk}",s.upload,methods=["PUT"])
    app.add_api_route("/meta/{room}/{guest}/{session}",s.meta,methods=["POST"])
    app.add_api_route("/finish/{room}/{guest}/{session}",s.finish,methods=["POST"])
    app.add_api_route("/admin/session-export/{room}/{session}",s.export_session,methods=["GET"])
    prefix="/api/studio"
    routes=[("/file/{room}/{guest}/{session}/{filename}",s.recording_file,["GET"]),("/join/{room}",s.join_guest,["POST"]),("/consent/{room}",s.consent,["POST"]),("/bootstrap/{room}",s.bootstrap,["GET"]),("/manifest/{room}/{guest}/{session}",s.manifest,["GET"]),
            ("/telemetry/{room}/{guest}/{session}",s.telemetry,["POST"]),("/sessions",s.overview,["GET"]),
            ("/session/{room}/{session}",s.session_edit,["PATCH"]),("/session/{room}/{session}/{action}",s.action,["POST"]),
            ("/markers/{room}/{session}/export",s.export_markers,["GET"]),("/marker/{id_}",s.marker_edit,["PATCH"]),
            ("/settings",s.settings_get,["GET"]),("/settings",s.settings_set,["PUT"]),
            ("/privacy-request/{id_}",s.privacy_resolve,["POST"]),
            ("/operations",s.operations,["GET"]),("/security/{action}",s.security_action,["POST"])]
    for path,handler,methods in routes:app.add_api_route(prefix+path,handler,methods=methods)
    @app.get("/assets/{name}")
    def client_asset(name: str):
        if name not in ("audio-profiles.js", "recording-media.js", "recording-formats.js", "ux.js"):
            raise HTTPException(404)
        return FileResponse(api["BASE"]/name, media_type="application/javascript")
    @app.on_event("startup")
    async def startup():
        if s.production and (not s.origins or api["_check_password"]("CHANGEME!")):
            raise RuntimeError("Production requires explicit allowed origins and non-default passwords")
        s.task=asyncio.create_task(s.maintenance())
    @app.on_event("shutdown")
    async def shutdown():
        if getattr(s,"task",None):
            s.task.cancel()
            try:await s.task
            except asyncio.CancelledError:pass
    return s

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
                consent_text="Ich stimme der Aufnahme, lokalen Zwischenspeicherung und Übertragung meiner Audio-/Videodaten für diese Produktion zu.",
                consent_text_en="I consent to recording, local storage and transfer of my audio/video data for this production. Live-call data is processed by the selected Jitsi provider.", privacy_policy="", retention_days=30, diagnostic_days=14,
                storage_region="Nicht konfiguriert", backup_policy="Nicht konfiguriert",
                collect_device_metadata=False, automatic_chapters=False,
                max_guests=12, max_duration_s=14400, max_chunk_bytes=67108864,
                max_track_bytes=21474836480, min_free_bytes=1073741824,
                requests_per_minute=240, token_requests_per_minute=180,
                admission_minutes=1440, upload_kbps=512, pause_uploads_during_recording=False)
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
        self.rate = OrderedDict()
        self.jobs = asyncio.Semaphore(2)
        self.production = os.getenv("STUDIO_PRODUCTION", "0") == "1"
        self.origins = set(filter(None, os.getenv("STUDIO_ALLOWED_ORIGINS", "").split(",")))
        self.init_db()
        self.config = self.load_config()
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
            """)
            c.execute("INSERT OR IGNORE INTO security VALUES (1,?)", (secrets.token_hex(16),))

    def load_config(self):
        with self.db() as c:
            row = c.execute("SELECT value FROM settings WHERE id=1").fetchone()
        stored=json.loads(row[0]) if row else {}
        if any(k not in DEFAULTS for k in stored):
            raise RuntimeError("Stored configuration contains unsupported settings; use a new DATA_DIR")
        config = copy.deepcopy(DEFAULTS)
        config.update(copy.deepcopy(stored))
        # Global audio-quality presets; historical takes keep their snapshots.
        for profile in config["profiles"]:
            profile.update(video=False, container="pcm", codec="pcm_s16le")
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

    def guest(self, request, room, guest=None, admission=False):
        header = request.headers.get("authorization", "")
        value = header[7:] if header.startswith("Bearer ") else ""
        t = self.a["_token_resolve"](value) if value else None
        if not t or t["room"] != room:
            raise HTTPException(403, "Invalid or revoked invitation")
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
        base = next(dict(p) for p in self.config["profiles"] if p["id"] == self.config["default_profile"])
        # Audio quality is global; recording audio/video remains a separate host control.
        video = not self.a["_room"](room)["settings"].get("audio_only", True)
        base.update(video=video, container="webm" if video else "pcm", codec="vp8,opus" if video else "pcm_s16le")
        return base

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

    def restore_rooms(self):
        with self.db() as c:
            rows = c.execute("SELECT * FROM room_state").fetchall()
        for row in rows:
            value = json.loads(row["value"])
            r = self.a["_room"](row["room"])
            r.update(value)

    def persist_room(self, room):
        r = self.a["ROOMS"].get(room, {})
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
                if (before["sample_rate"], before["channels"]) != (sr, ch):
                    raise HTTPException(409, "Track format is immutable")
                return {"ok": True, "meta": before}
            c.execute("INSERT INTO tracks(room,session,guest,owner,meta,updated) VALUES(?,?,?,?,?,?)",
                      (room, session, guest, token["id"], json.dumps(meta), time.time()))
            atomic_bytes(dest / "meta.json", json_bytes(meta))
        return {"ok": True, "meta": meta}

    async def upload(self, room, guest, session, chunk, request: Request, ext: str = "pcm"):
        token = self.guest(request, room, guest)
        if not re.fullmatch(r"\d{6}", chunk) or ext not in ("pcm", "webm"):
            raise HTTPException(400, "Invalid chunk index or container")
        sha = request.headers.get("x-chunk-sha256", "")
        if not HASH.fullmatch(sha):
            raise HTTPException(422, "SHA-256 required")
        track = self.track(room, guest, session, token["id"])
        idx = int(chunk)
        data = await self.body(request, self.config["max_chunk_bytes"])
        if not data or hashlib.sha256(data).hexdigest() != sha:
            raise HTTPException(422, "Chunk checksum mismatch or empty chunk")
        if ext == "pcm" and len(data) % (2 * json.loads(track["meta"])["channels"]):
            raise HTTPException(422, "Partial PCM frame")
        with self.lock, self.db() as c:
            self.take(room, session)
            old = c.execute("SELECT * FROM chunks WHERE room=? AND session=? AND guest=? AND idx=?", (room,session,guest,idx)).fetchone()
            path = self.dest(room,guest,session) / f"chunk-{idx:06d}.{ext}"
            if old:
                if (old["sha"], old["size"], old["ext"]) != (sha, len(data), ext):
                    raise HTTPException(409, "Conflicting duplicate chunk; original preserved")
                c.execute("UPDATE chunks SET attempts=attempts+1 WHERE room=? AND session=? AND guest=? AND idx=?", (room,session,guest,idx))
                if not path.exists() or digest(path) != sha:
                    atomic_bytes(path, data)
                return dict(ok=True, idx=idx, bytes=len(data), sha256=sha, duplicate=True)
            if track["finalized"] or track["expected"] is not None:
                raise HTTPException(409, "Track is sealed")
            total = c.execute("SELECT COALESCE(SUM(size),0) FROM chunks WHERE room=? AND session=? AND guest=?", (room,session,guest)).fetchone()[0]
            if total + len(data) > self.config["max_track_bytes"]:
                raise HTTPException(413, "Track storage limit reached")
            if shutil.disk_usage(self.root).free < self.config["min_free_bytes"] + len(data):
                raise HTTPException(507, "Storage reserve reached; preserve local recording")
            atomic_bytes(path, data)
            c.execute("INSERT INTO chunks(room,session,guest,idx,size,sha,ext) VALUES(?,?,?,?,?,?,?)",
                      (room,session,guest,idx,len(data),sha,ext))
            c.execute("UPDATE tracks SET updated=? WHERE room=? AND session=? AND guest=?", (time.time(),room,session,guest))
        return dict(ok=True, idx=idx, bytes=len(data), sha256=sha, duplicate=False)

    def manifest(self, room, guest, session, request: Request):
        token = self.guest(request,room,guest)
        t = self.track(room,guest,session,token["id"])
        with self.db() as c:
            rows = c.execute("SELECT idx,size,sha,attempts FROM chunks WHERE room=? AND session=? AND guest=? ORDER BY idx", (room,session,guest)).fetchall()
        return dict(ok=True, expected=t["expected"], finalized=bool(t["finalized"]),
                    chunks=[dict(r) for r in rows], report=json.loads(t["report"]) if t["report"] else None)

    def build(self, room, guest, session, repair=False):
        # Serialize finalization/rebuild/deletion across all tracks. No event-loop I/O.
        with self.lock:
            t = self.track(room,guest,session)
            with self.db() as c:
                rows = [dict(r) for r in c.execute("SELECT * FROM chunks WHERE room=? AND session=? AND guest=? ORDER BY idx", (room,session,guest))]
            expected = t["expected"]
            indices = {r["idx"] for r in rows}
            missing = sorted(set(range(expected or (max(indices, default=-1)+1))) - indices)
            if not rows or missing or expected is not None and len(rows) != expected:
                raise HTTPException(409, dict(reason="missing_chunks", missing=missing[:1000], expected=expected, received=len(rows)))
            dest = self.dest(room,guest,session)
            paths = [dest / f"chunk-{r['idx']:06d}.{r['ext']}" for r in rows]
            for row,path in zip(rows,paths):
                if not path.exists() or path.stat().st_size != row["size"] or digest(path) != row["sha"]:
                    raise HTTPException(409, dict(reason="corrupt_chunk", idx=row["idx"]))
            meta = json.loads(t["meta"])
            sr,ch = meta["sample_rate"],meta["channels"]
            warnings = []
            if expected is None:
                warnings.append("No sealed client manifest; completeness cannot be proven")
            if t["interrupted"]:
                warnings.append("Client recording interrupted; unpersisted tail may be missing")
            with self.a["_db_conn"]() as c:
                timeline = c.execute("SELECT start_at,stop_at FROM recording_sessions WHERE room=? AND session=?", (room,session)).fetchone()
            duration = ((timeline["stop_at"]-timeline["start_at"])/1000
                        if timeline and timeline["stop_at"] and timeline["start_at"] else None)
            if duration is None:
                warnings.append("Session end boundary unavailable")
            elif duration <= 0:
                raise HTTPException(409, "Invalid session boundaries")
            working = Path(tempfile.mkdtemp(prefix=".build-",dir=dest))
            try:
                wav = working / "full.wav"
                ext = {r["ext"] for r in rows}
                if ext == {"pcm"}:
                    with wave.open(str(wav), "wb") as w:
                        w.setparams((ch,2,sr,0,"NONE","not compressed"))
                        for path in paths:
                            with path.open("rb") as f:
                                for block in iter(lambda:f.read(1024*1024),b""):
                                    w.writeframesraw(block)
                    raw_duration = sum(r["size"] for r in rows)/(sr*ch*2)
                elif ext == {"webm"}:
                    source = working / "source.webm"
                    with source.open("wb") as out:
                        for path in paths:
                            with path.open("rb") as f:
                                shutil.copyfileobj(f,out,1024*1024)
                    self.ffmpeg(["-fflags","+genpts","-i",str(source),"-vn","-acodec","pcm_s16le","-ar",str(sr),"-ac",str(ch),str(wav)])
                    with wave.open(str(wav)) as w:
                        raw_duration = w.getnframes()/w.getframerate()
                    try:
                        self.ffmpeg(["-fflags","+genpts","-i",str(source),"-c:v","libx264","-preset","fast","-c:a","aac","-movflags","+faststart",str(working/"full.mp4")])
                    except HTTPException:
                        warnings.append("MP4 conversion failed; audio and source chunks preserved")
                else:
                    raise HTTPException(409, "Mixed chunk formats cannot be merged")
                start_deviation = ((meta.get("started_at",0)-timeline["start_at"])/1000 if timeline and timeline["start_at"] else None)
                if duration and abs(raw_duration-duration) > max(0.25,duration*0.005):
                    warnings.append("Source duration differs from session duration")
                if start_deviation is not None and abs(start_deviation)>0.25:
                    warnings.append("Track started outside synchronization tolerance")
                # Normalize to absolute boundaries only after preserving raw timing evidence.
                if duration:
                    normalized = working / "normalized.wav"
                    offset = start_deviation or 0
                    af = (f"adelay={round(offset*1000)}:all=1," if offset > 0 else f"atrim=start={-offset},asetpts=PTS-STARTPTS," if offset < 0 else "")
                    self.ffmpeg(["-i",str(wav),"-af",af+f"apad,atrim=duration={duration}","-ar",str(sr),"-ac",str(ch),"-c:a","pcm_s16le",str(normalized)])
                    os.replace(normalized,wav)
                self.a["_wav_add_markers"](wav,self.markers(room,session))
                with wave.open(str(wav)) as w:
                    frames=w.getnframes(); rate=w.getframerate(); channels=w.getnchannels(); width=w.getsampwidth()
                if rate!=sr or channels!=ch or width!=2 or frames<1 or wav.stat().st_size<44+frames*ch*2:
                    raise HTTPException(422,"WAV structure validation failed")
                if duration and abs(frames-round(duration*sr))>1:
                    raise HTTPException(422,"WAV frame count differs from normalized timeline")
                files={"full.wav":dict(sha256=digest(wav),bytes=wav.stat().st_size)}
                if (working/"full.mp4").exists():
                    files["full.mp4"]=dict(sha256=digest(working/"full.mp4"),bytes=(working/"full.mp4").stat().st_size)
                telemetry=json.loads(t["telemetry"])
                if telemetry.get("events"):
                    warnings.append("Client reported device, permission, sleep or connection events; review timeline")
                state="incomplete" if t["interrupted"] or expected is None else "complete_with_warnings" if warnings else "complete"
                report=dict(status=state,validated_at=time.time(),warnings=warnings,expected_duration_s=duration,
                            raw_duration_s=raw_duration,duration_s=frames/rate,start_deviation_s=start_deviation,
                            end_deviation_s=((start_deviation or 0)+raw_duration-duration if duration else None),
                            sample_rate=rate,channels=channels,frames=frames,size_bytes=wav.stat().st_size,
                            missing_chunks=missing,retried_chunks=sum(r["attempts"]>1 for r in rows),
                            duplicate_requests=sum(r["attempts"]-1 for r in rows),chunks=len(rows),
                            profile=meta["profile"],browser=meta.get("browser"),os=meta.get("os"),telemetry=telemetry,files=files)
                for filename in files:
                    os.replace(working/filename,dest/filename)
                atomic_bytes(dest/"quality.json",json_bytes(report))
                with self.db() as c:
                    c.execute("UPDATE tracks SET finalized=1,report=?,updated=? WHERE room=? AND session=? AND guest=?",
                              (json.dumps(report),time.time(),room,session,guest))
                self.a["_mixdown_path"](room,session).unlink(missing_ok=True)
                self.audit("admin" if repair else "guest:"+t["owner"],"track.rebuild" if repair else "track.finalize",f"{room}/{session}/{guest}",dict(status=state))
                return report
            finally:
                shutil.rmtree(working,ignore_errors=True)

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
        count=number(data.get("expected_chunks"),1,999999,"expected_chunks")
        with self.lock:
            t=self.track(room,guest,session,token["id"])
            if t["finalized"]:
                if count != t["expected"]:
                    raise HTTPException(409,"Finalized manifest differs")
                return dict(ok=True,finalized=True,report=json.loads(t["report"]))
            with self.db() as c:
                got=c.execute("SELECT idx FROM chunks WHERE room=? AND session=? AND guest=? ORDER BY idx",(room,session,guest)).fetchall()
                if [r[0] for r in got] != list(range(count)):
                    raise HTTPException(409,"Chunk sequence incomplete; local data must be retained")
                c.execute("UPDATE tracks SET expected=?,interrupted=? WHERE room=? AND session=? AND guest=?",
                          (count,int(bool(data.get("interrupted"))),room,session,guest))
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
        if any(r.get("rec_state") == "recording" for r in self.a["ROOMS"].values()):
            raise HTTPException(409, "ux.recording_busy")
        cfg=json.loads(json.dumps(self.config))
        if any(k not in DEFAULTS for k in p):raise HTTPException(422,"Unknown setting")
        cfg.update(p)
        bounds=dict(retention_days=(0,3650),diagnostic_days=(1,365),max_guests=(1,100),max_duration_s=(60,86400),
                    max_chunk_bytes=(65536,268435456),max_track_bytes=(1048576,1099511627776),min_free_bytes=(0,1099511627776),
                    requests_per_minute=(30,10000),token_requests_per_minute=(30,10000),admission_minutes=(5,43200),upload_kbps=(32,100000))
        for k,(lo,hi) in bounds.items():cfg[k]=number(cfg[k],lo,hi,k)
        for k in ("collect_device_metadata","automatic_chapters","pause_uploads_during_recording"):
            if not isinstance(cfg[k],bool):raise HTTPException(422,f"{k}: boolean required")
        for k in ("consent_text","consent_text_en","privacy_policy","storage_region","backup_policy"):
            if not isinstance(cfg[k],str) or len(cfg[k])>12000:raise HTTPException(422,f"Invalid {k}")
        if not cfg["consent_text"].strip() or not cfg["consent_text_en"].strip():raise HTTPException(422,"Consent text must not be empty")
        if not isinstance(cfg["profiles"],list) or not 1<=len(cfg["profiles"])<=20:raise HTTPException(422,"1..20 profiles required")
        ids=set()
        for pro in cfg["profiles"]:
            if not isinstance(pro,dict):raise HTTPException(422,"Invalid profile")
            self.ident(pro.get("id",""))
            if pro["id"] in ids:raise HTTPException(422,"Duplicate profile id")
            ids.add(pro["id"])
            if not isinstance(pro.get("name"),str) or not 1<=len(pro["name"])<=100:raise HTTPException(422,"Profile name required")
            for k,lo,hi in (("sample_rate",8000,192000),("channels",1,2),("width",160,3840),("height",120,2160),("fps",1,60),("audio_bitrate",64000,512000),("video_bitrate",100000,40000000),("chunk_ms",1000,10000),("max_duration_s",60,86400)):
                pro[k]=number(pro.get(k),lo,hi,k)
            if not isinstance(pro.get("video"),bool):raise HTTPException(422,"Profile video boolean required")
            if pro.get("container") not in ("pcm","webm","mp4") or pro.get("codec") not in ("pcm_s16le","vp8,opus","vp9,opus","h264,aac"):
                raise HTTPException(422,"Unsupported preferred codec/container")
            if not pro["video"] and (pro["container"]!="pcm" or pro["codec"]!="pcm_s16le"):
                raise HTTPException(422,"Audio profiles use lossless PCM")
            if pro["video"] and pro["container"]=="pcm":raise HTTPException(422,"Video needs a media container")
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
            if any(r.get("rec_state") == "recording" for r in self.a["ROOMS"].values()):
                raise HTTPException(409, "ux.recording_busy")
            if any(p["video"] for p in cfg["profiles"]):
                raise HTTPException(422, "ux.audio_profiles_only")
            c.execute("INSERT OR REPLACE INTO settings VALUES(1,?)",(json.dumps(cfg),))
            self.config=cfg
            self.a["MARKER_KINDS"]=set(x["id"] for x in cats)
        self.audit(role,"settings.update",detail={"fields":list(p)})
        return {"ok":True}


    async def consent(self, room, request: Request):
        token = self.guest(request, room, admission=True)
        p = await self.payload(request)
        if p.get("accepted") is not True:
            raise HTTPException(422, "ux.consent_required")
        text=self.config["consent_text_en" if self.a["_room_locale"](room)=="en" else "consent_text"]
        if p.get("digest")!=hashlib.sha256(text.encode()).hexdigest():
            raise HTTPException(409,"ux.consent_changed")
        with self.db() as c:
            c.execute("INSERT OR REPLACE INTO guest_consents VALUES(?,?,?,?)",
                      (token["id"], room, hashlib.sha256(self.config["consent_text_en" if self.a["_room_locale"](room)=="en" else "consent_text"].encode()).hexdigest(), time.time()))
        return {"ok": True}

    def bootstrap(self,room,request: Request):
        token=self.guest(request,room,admission=True)
        r=self.a["ROOMS"].get(room,{})
        pro=self.profile(room)
        if r.get("rec_session") and r.get("rec_state")=="recording":
            t=self.take(room,r["rec_session"])
            if t:pro=json.loads(t["profile"])
        policy={k:self.config[k] for k in ("consent_text","privacy_policy","retention_days","storage_region","backup_policy","collect_device_metadata","upload_kbps","pause_uploads_during_recording","max_duration_s")}
        if self.a["_room_locale"](room)=="en":policy["consent_text"]=self.config["consent_text_en"]
        policy["consent_digest"]=hashlib.sha256(policy["consent_text"].encode()).hexdigest()
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
            self.a["_token_revoke"](r["owner"])
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
        if origin and origin not in allowed:return await deny(403,"Origin denied")
        if s.production and not s.origins:return await deny(503,"Configure STUDIO_ALLOWED_ORIGINS")
        if scope.get("method") in ("POST","PUT","PATCH","DELETE") and not origin and b"cookie" in headers and headers.get(b"sec-fetch-site")==b"cross-site":
            return await deny(403,"Cross-site write denied")
        try:
            s.rate_check("ip:"+str(scope.get("client",("unknown",))[0]),s.config["requests_per_minute"])
        except HTTPException as e:return await deny(e.status_code,e.detail)
        auth_role=None;token=None
        if ws:
            from http.cookies import SimpleCookie
            jar=SimpleCookie();jar.load(headers.get(b"cookie",b"").decode())
            cookie=jar.get(s.a["COOKIE_NAME"])
            if path.startswith("/ws/host/"):
                auth_role=s.a["_session_role"](cookie.value if cookie else "")
                if not auth_role:return await deny(401,"Authentication required")
            elif path.startswith("/ws/guest/"):
                q=parse_qs(scope.get("query_string",b"").decode());value=q.get("token",[""])[0]
                token=s.a["_token_resolve"](value)
                parts=path.split("/")
                if not token or token["room"]!=parts[-2] or parts[-1]!="g_"+token["id"]:return await deny(403,"Invalid guest identity")
                guests=s.a["ROOMS"].get(token["room"],{}).get("guests",{})
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
            nonlocal count,window,body_bytes
            msg=await original_receive()
            if msg["type"]=="http.request":
                body_bytes+=len(msg.get("body",b""))
                cap=s.config["max_chunk_bytes"] if path.startswith("/upload/") else 8*1024*1024
                if body_bytes>cap:raise HTTPException(413,"Request too large")
            if msg["type"]=="websocket.receive":
                size=len(msg.get("text","") or "")+len(msg.get("bytes",b"") or b"")
                if time.monotonic()-window>=1:window=time.monotonic();count=0
                count+=1
                revoked=token and not s.a["_token_resolve"](value)
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
                if public_origin and path in ("/host", "/host.html", "/recorder.html"):
                    policy = policy.replace("script-src 'self'", "script-src 'self' " + public_origin + "/external_api.js")
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
    routes=[("/consent/{room}",s.consent,["POST"]),("/bootstrap/{room}",s.bootstrap,["GET"]),("/manifest/{room}/{guest}/{session}",s.manifest,["GET"]),
            ("/telemetry/{room}/{guest}/{session}",s.telemetry,["POST"]),("/sessions",s.overview,["GET"]),
            ("/session/{room}/{session}",s.session_edit,["PATCH"]),("/session/{room}/{session}/{action}",s.action,["POST"]),
            ("/markers/{room}/{session}/export",s.export_markers,["GET"]),("/marker/{id_}",s.marker_edit,["PATCH"]),
            ("/settings",s.settings_get,["GET"]),("/settings",s.settings_set,["PUT"]),
            ("/privacy-request/{id_}",s.privacy_resolve,["POST"]),
            ("/operations",s.operations,["GET"]),("/security/{action}",s.security_action,["POST"])]
    for path,handler,methods in routes:app.add_api_route(prefix+path,handler,methods=methods)
    @app.get("/assets/{name}")
    def client_asset(name: str):
        if name not in ("audio-profiles.js", "recording-media.js", "ux.js"):
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

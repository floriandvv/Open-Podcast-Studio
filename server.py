"""
Mini Podcast Recorder Server
----------------------------
Nimmt WebM/PCM-Chunks von Gast-Browsern entgegen und legt sie unter
    uploads/<room>/<guest>/<session>/chunk-XXXXXX.pcm
ab. Beim Finish werden alle Chunks zu einer WAV-Datei zusammengefuegt.

Phase 3 -- Authentifizierung:
    Admin/Host/Index-Seiten sind durch ein Session-Cookie geschuetzt.
    Das Admin-Passwort wird als bcrypt-Hash in der .env-Datei hinterlegt.

Phase 4 -- Gast-Token:
    Gaeste erhalten einen kryptografisch sicheren Einladungslink der Form
        /recorder.html?token=<token>
    Der Token enthaelt keinen sichtbaren Raumnamen. Der Raum ist
    ausschliesslich serverseitig in tokens.db hinterlegt.
    Neue Routen:
        POST /host/token/<room>        -> Token erzeugen (Auth required)
        GET  /host/tokens/<room>       -> Token-Liste anzeigen (Auth required)
        DELETE /host/token/<token_id>  -> Token widerrufen (Auth required)
        GET  /token/resolve            -> Token pruefen + Raum zurueckgeben (offen)

    Passwort-Hash erzeugen:
        python -c "from passlib.hash import bcrypt; print(bcrypt.hash('DEIN_PASSWORT'))"
    Dann in .env eintragen:
        ADMIN_PASSWORD_HASH=$2b$12$...
        SESSION_SECRET=<langer-zufaelliger-string>

Start:
    python server.py
"""

import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import wave
import zipfile
import io
from html import escape as html_escape
from json import dumps as json_dumps
from pathlib import Path

import uvicorn
from fastapi import (Cookie, Depends, FastAPI, HTTPException, Request, Response,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Optionale Abhaengigkeiten
# ---------------------------------------------------------------------------
try:
    from passlib.hash import bcrypt as _bcrypt
    _HAVE_BCRYPT = True
except ImportError:
    _HAVE_BCRYPT = False

try:
    from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
    _HAVE_ITSDANGEROUS = True
except ImportError:
    _HAVE_ITSDANGEROUS = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _check_deps():
    missing = []
    if not _HAVE_BCRYPT:
        missing.append("passlib[bcrypt]")
    if not _HAVE_ITSDANGEROUS:
        missing.append("itsdangerous")
    if missing:
        print("FEHLER: Fehlende Pakete. Bitte installieren:")
        print(f"  pip install {' '.join(missing)}")
        raise SystemExit(1)

_check_deps()

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
# Passwoerter werden NICHT mehr direkt als Hash in der .env gehalten, sondern
# in auth.json verwaltet (editierbar per Admin-Panel, Feature 2). Die .env
# liefert nur noch das STANDARD-Passwort (Feature 14) fuer die Erstinstallation.
DEFAULT_ADMIN_PASSWORD: str = os.environ.get("DEFAULT_ADMIN_PASSWORD", "CHANGEME!")
DEFAULT_HOST_PASSWORD:  str = os.environ.get("DEFAULT_HOST_PASSWORD",  "CHANGEME!")
SESSION_SECRET: str      = os.environ.get("SESSION_SECRET", "")
SESSION_MAX_AGE: int     = int(os.environ.get("SESSION_MAX_AGE_HOURS", "12")) * 3600

# Laufzeit-Konfig wird in config.json persistiert (editierbar per Admin-Panel).
# Defaults gelten nur beim allerersten Start.
CONFIG_PATH = None  # wird nach BASE-Definition gesetzt

if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_hex(32)
    print("WARNUNG: SESSION_SECRET nicht gesetzt — temporaerer Secret aktiv.")
    print("  Bitte SESSION_SECRET in .env setzen.")

_SIGNER    = TimestampSigner(SESSION_SECRET, salt="podcast-session")
COOKIE_NAME = "ps_session"


# ---------------------------------------------------------------------------
# Auth-Store (auth.json) -- Feature 2, 13, 14
# ---------------------------------------------------------------------------
# Zwei Rollen:
#   - admin: sieht Admin-Panel + Host-Studio (Vollzugriff)
#   - host : sieht NUR die Raum-Eingabemaske + Host-Studio (kein Admin-Panel)
# Passwoerter werden als bcrypt-Hash in auth.json gespeichert und sind
# ueber das Admin-Panel zuruecksetzbar -- ohne .env-Edit / Neustart.
AUTH_PATH = None  # nach BASE gesetzt
_AUTH_LOCK = threading.Lock()


def _hash_pw(pw: str) -> str:
    return _bcrypt.hash(pw)


def _auth_load() -> dict:
    try:
        if AUTH_PATH and AUTH_PATH.exists():
            data = json.loads(AUTH_PATH.read_text())
            if data.get("admin_hash") and data.get("host_hash"):
                return data
    except Exception:
        pass
    # Erstinstallation: Standard-Passwoerter (Feature 14: "CHANGEME!")
    data = {
        "admin_hash": _hash_pw(DEFAULT_ADMIN_PASSWORD),
        "host_hash":  _hash_pw(DEFAULT_HOST_PASSWORD),
    }
    _auth_save(data)
    return data


def _auth_save(data: dict):
    if AUTH_PATH is None:
        return
    with _AUTH_LOCK:
        AUTH_PATH.write_text(json.dumps(data, indent=2))


def _check_password(pw: str) -> str | None:
    """Gibt die Rolle ('admin'|'host') zurueck oder None bei falschem Passwort."""
    data = _auth_load()
    try:
        if _bcrypt.verify(pw, data["admin_hash"]):
            return "admin"
    except Exception:
        pass
    try:
        if _bcrypt.verify(pw, data["host_hash"]):
            return "host"
    except Exception:
        pass
    return None


def _set_password(role: str, new_pw: str):
    if role not in ("admin", "host"):
        raise HTTPException(400, "Rolle muss 'admin' oder 'host' sein")
    if not new_pw or len(new_pw) < 4:
        raise HTTPException(400, "Passwort muss mindestens 4 Zeichen haben")
    data = _auth_load()
    data[f"{role}_hash"] = _hash_pw(new_pw)
    _auth_save(data)

# ---------------------------------------------------------------------------
# Pfade / Konstanten
# ---------------------------------------------------------------------------
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHANNELS    = 1
SAMPLE_WIDTH        = 2

BASE       = Path(__file__).parent
# Keep mutable runtime data separate from the application code. This allows
# Docker deployments to mount one persistent volume at DATA_DIR.
DATA_DIR   = Path(os.environ.get("DATA_DIR", str(BASE))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS    = DATA_DIR / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"
AUTH_PATH   = DATA_DIR / "auth.json"

# ---------------------------------------------------------------------------
# Persistente Konfiguration (config.json)
# ---------------------------------------------------------------------------
_CFG_LOCK = threading.Lock()
_CFG_DEFAULTS = {
    "token_days":       7,      # Standard-Laufzeit fuer Gast-Token in Tagen
    "recording_days":   30,     # Aufnahmen aelter als N Tage automatisch loeschen (0=deaktiviert)
    "chunk_hours":      72,     # Rohe Chunk-Dateien aelter als N Std loeschen (Feature 10)
    "log_days":         14,     # Gast-Console-Logs aelter als N Tage loeschen (0=deaktiviert)
    # Custom Branding (Feature 8)
    "brand_name":       "Podcast Studio",
    "brand_color":      "#30a46c",
    "brand_favicon":    "",     # Data-URL oder Pfad; leer = Standard
    # Archivierte Raeume (Feature 6) -- Liste von Raumnamen
    "archived_rooms":   [],
}

def _cfg_load() -> dict:
    """Laedt config.json; fehlende Schluessel werden mit Defaults aufgefuellt.

    WICHTIG: config.json soll *immer* existieren (auch wenn nie Branding gesetzt
    wurde), damit alle Clients Branding konsistent vom Server beziehen koennen.
    """
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text())
            cfg  = dict(_CFG_DEFAULTS)
            cfg.update({k: v for k, v in data.items() if k in _CFG_DEFAULTS})
            return cfg
    except Exception:
        pass

    # Erststart oder defekte Datei -> Defaults schreiben und zurueckgeben.
    try:
        cfg = dict(_CFG_DEFAULTS)
        _cfg_save(cfg)
        return cfg
    except Exception:
        return dict(_CFG_DEFAULTS)

def _cfg_save(cfg: dict):
    with _CFG_LOCK:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))

def _cfg_get(key: str):
    return _cfg_load().get(key, _CFG_DEFAULTS.get(key))

SAFE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Presence-Schwellen (Heartbeat alle 2 s, Pegel alle 200 ms).
#   <= GUEST_STALE_AFTER      -> 🟢 online
#   <= GUEST_OFFLINE_AFTER    -> 🟡 stale   ("wackelt / keine Heartbeats mehr")
#   danach                    -> 🔴 offline
# Frueher galt GUEST_FORGET_AFTER (600 s) zugleich als Gelb-Grenze -> ein
# laengst verschwundener Gast blieb 10 Minuten lang gelb. Gelb kommt jetzt
# frueh (nach 3 fehlenden Heartbeats) und Rot nach 2 Minuten.
GUEST_STALE_AFTER   = 6.0    # 3 verpasste Heartbeats -> gelb
GUEST_OFFLINE_AFTER = 20.0   # danach rot
GUEST_FORGET_AFTER  = 120.0  # danach ganz aus der Liste entfernen
START_LEAD_SECONDS = 5.0

# Push presence updates even when guests go silent.
# Without this, the host UI only updates when a guest sends a heartbeat,
# so "offline" state changes are not visible until a manual refresh.
PRESENCE_TICK = 2.0

_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_LOGIN_LOCK      = threading.Lock()
LOGIN_MAX_ATTEMPTS   = 5
LOGIN_WINDOW_SECONDS = 60

# ---------------------------------------------------------------------------
# SQLite – Token-Datenbank
# ---------------------------------------------------------------------------
DB_PATH = DATA_DIR / "tokens.db"
_DB_LOCK = threading.Lock()


def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _DB_LOCK, _db_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guest_tokens (
                id          TEXT PRIMARY KEY,   -- UUID, kurzform
                token       TEXT UNIQUE NOT NULL,
                room        TEXT NOT NULL,
                label       TEXT NOT NULL DEFAULT '',
                created_at  REAL NOT NULL,
                expires_at  REAL NOT NULL,
                revoked     INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Marker-Tabelle: Zeitmarken die der Host waehrend der Aufnahme setzt.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS markers (
                id          TEXT PRIMARY KEY,    -- kurze ID
                room        TEXT NOT NULL,
                session     TEXT NOT NULL DEFAULT '',
                kind        TEXT NOT NULL,        -- 'ad' | 'cut_in' | 'cut_out'
                created_at  REAL NOT NULL,        -- Server-Zeit (epoch, s)
                offset_ms   INTEGER NOT NULL DEFAULT 0,  -- ms seit Aufnahmestart
                note        TEXT NOT NULL DEFAULT ''
            )
        """)
        # Raum-Registry: ein Raum existiert, sobald er einmal angelegt/besucht
        # wurde -- unabhaengig davon, ob schon Tokens oder Aufnahmen vorliegen.
        # Dadurch erscheinen neue Raeume sofort in der Admin-Uebersicht.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                room        TEXT PRIMARY KEY,
                created_at  REAL NOT NULL
            )
        """)
        # Gast-Console-Logs: persistente Ablage der clientseitigen log()-Events.
        # RAM (GUEST_CONSOLE) bleibt fuer den Live-Blick; hier liegt die Historie,
        # damit Aufnahmen auch nachtraeglich analysiert werden koennen.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guest_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                room        TEXT NOT NULL,
                guest       TEXT NOT NULL DEFAULT '',
                session     TEXT NOT NULL DEFAULT '',
                ts          REAL NOT NULL,        -- Client-Zeit (epoch, s)
                level       TEXT NOT NULL DEFAULT 'info',  -- 'info' | 'ok' | 'err'
                msg         TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_guest_logs_room_ts ON guest_logs (room, ts)")
        conn.commit()

_init_db()

# DB-Migration: note-Spalte in markers sicherstellen (falls alte DB vorhanden)
try:
    with _DB_LOCK, _db_conn() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(markers)").fetchall()]
        if "note" not in cols:
            conn.execute("ALTER TABLE markers ADD COLUMN note TEXT NOT NULL DEFAULT ''")
            conn.commit()
except Exception as e:
    print("[db] marker note migration failed:", e)


# Raum-Registry ------------------------------------------------------------

def _room_register(room: str) -> None:
    """Legt einen Raum in der Registry an (idempotent). So taucht ein neu
    erstellter Raum sofort in /admin/rooms auf -- nicht erst nach der ersten
    Aufnahme oder Token-Erzeugung."""
    try:
        with _DB_LOCK, _db_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO rooms (room, created_at) VALUES (?, ?)",
                (room, time.time()),
            )
            conn.commit()
    except Exception as e:
        print("[rooms] register failed:", e)


def _room_registry_list() -> list[str]:
    try:
        with _DB_LOCK, _db_conn() as conn:
            rows = conn.execute("SELECT room FROM rooms").fetchall()
        return [r["room"] for r in rows]
    except Exception:
        return []


def _room_registry_delete(room: str) -> None:
    try:
        with _DB_LOCK, _db_conn() as conn:
            conn.execute("DELETE FROM rooms WHERE room=?", (room,))
            conn.commit()
    except Exception:
        pass


# Gast-Console-Logs (persistent) ------------------------------------------

def _guest_logs_store(room: str, guest: str, session: str, lines: list) -> None:
    """Schreibt Console-Zeilen eines Gastes persistent in die DB.

    `lines` ist die vom Client gelieferte Liste aus dict(ts, level, msg).
    Wird zusaetzlich zum RAM-Puffer (GUEST_CONSOLE) aufgerufen.
    """
    if not lines or not isinstance(lines, list):
        return
    now = time.time()
    rows = []
    for ln in lines[-50:]:
        if not isinstance(ln, dict):
            continue
        rows.append((
            room,
            str(guest or "")[:80],
            str(session or "")[:40],
            float(ln.get("ts") or now),
            str(ln.get("level") or "info")[:10],
            str(ln.get("msg") or "")[:400],
        ))
    if not rows:
        return
    try:
        with _DB_LOCK, _db_conn() as conn:
            conn.executemany(
                "INSERT INTO guest_logs (room, guest, session, ts, level, msg) "
                "VALUES (?, ?, ?, ?, ?, ?)", rows)
            conn.commit()
    except Exception as e:
        print("[guest_logs] store failed:", e)


def _guest_logs_query(room: str, since: float = 0.0, limit: int = 4000) -> list[dict]:
    """Liefert Console-Logs eines Raums (aelteste zuerst), optional ab `since`."""
    try:
        with _DB_LOCK, _db_conn() as conn:
            rows = conn.execute(
                "SELECT guest, session, ts, level, msg FROM guest_logs "
                "WHERE room=? AND ts > ? ORDER BY ts ASC, id ASC LIMIT ?",
                (room, float(since or 0.0), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print("[guest_logs] query failed:", e)
        return []


def _guest_logs_delete_room(room: str) -> None:
    try:
        with _DB_LOCK, _db_conn() as conn:
            conn.execute("DELETE FROM guest_logs WHERE room=?", (room,))
            conn.commit()
    except Exception:
        pass


# Marker-Operationen -------------------------------------------------------

MARKER_KINDS = {"ad", "cut_in", "cut_out"}


def _new_session_id() -> str:
    """Kurze, URL-/Pfad-sichere Session-ID für eine Aufnahme.

    Wir nutzen Base36 aus current time (ms) + 2 Bytes Randomness.
    Ergebnis ist kompakt (gut für UI/Ordnernamen) und ausreichend eindeutig.
    """
    now_ms = int(time.time() * 1000)
    rnd = secrets.token_hex(2)  # 4 hex chars
    base36 = "0123456789abcdefghijklmnopqrstuvwxyz"
    n = now_ms
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = base36[r] + out
    out = out or "0"
    return f"s{out}{rnd}"  # beginnt bewusst mit 's'


def _marker_create(room: str, session: str, kind: str,
                   offset_ms: int = 0, note: str = "") -> dict:
    marker_id = secrets.token_hex(4)
    now       = time.time()
    with _DB_LOCK, _db_conn() as conn:
        conn.execute(
            "INSERT INTO markers (id, room, session, kind, created_at, offset_ms, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (marker_id, room, session[:40], kind, now, int(offset_ms), note[:200]),
        )
        conn.commit()
    return {"id": marker_id, "room": room, "session": session, "kind": kind,
            "created_at": now, "offset_ms": int(offset_ms), "note": note}


def _marker_list(room: str, session: str | None = None) -> list[dict]:
    with _DB_LOCK, _db_conn() as conn:
        if session:
            rows = conn.execute(
                "SELECT * FROM markers WHERE room=? AND session=? ORDER BY offset_ms ASC, created_at ASC",
                (room, session)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM markers WHERE room=? ORDER BY created_at DESC", (room,)).fetchall()
    return [dict(r) for r in rows]


def _marker_sessions(room: str) -> list[str]:
    """Alle Sessions eines Raums, fuer die Marker existieren (neueste zuerst)."""
    with _DB_LOCK, _db_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT session FROM markers WHERE room=? AND session<>'' "
            "ORDER BY session DESC",
            (room,),
        ).fetchall()
    return [str(r["session"]) for r in rows if r["session"]]


def _marker_delete(marker_id: str) -> bool:
    with _DB_LOCK, _db_conn() as conn:
        cur = conn.execute("DELETE FROM markers WHERE id=?", (marker_id,))
        conn.commit()
    return cur.rowcount > 0


# Token-Operationen --------------------------------------------------------

def _token_create(room: str, days: int, label: str = "") -> dict:
    """Erzeugt einen neuen Gast-Token und speichert ihn in der DB."""
    token_id  = secrets.token_hex(4)          # kurze ID fuer Verwaltung
    token_val = secrets.token_urlsafe(32)      # 256-Bit Einladungstoken
    now       = time.time()
    expires   = now + days * 86400
    with _DB_LOCK, _db_conn() as conn:
        conn.execute(
            "INSERT INTO guest_tokens (id, token, room, label, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (token_id, token_val, room, label[:80], now, expires),
        )
        conn.commit()
    return {"id": token_id, "token": token_val, "room": room,
            "label": label, "created_at": now, "expires_at": expires}


def _token_resolve(token_val: str) -> dict | None:
    """
    Prueft Token und gibt {room, expires_at, ...} zurueck, oder None.
    Timing-sicher: auch bei fehlendem Token wird verglichen.
    """
    with _DB_LOCK, _db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guest_tokens WHERE revoked=0", ()
        ).fetchall()
    # Timing-sicherer Vergleich ueber alle Zeilen
    found = None
    for r in row:
        if hmac.compare_digest(r["token"], token_val):
            found = r
    if found is None:
        return None
    if time.time() > found["expires_at"]:
        return None  # abgelaufen
    return dict(found)


def _token_list(room: str) -> list[dict]:
    with _DB_LOCK, _db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM guest_tokens WHERE room=? ORDER BY created_at DESC", (room,)
        ).fetchall()
    return [dict(r) for r in rows]


def _token_revoke(token_id: str) -> bool:
    with _DB_LOCK, _db_conn() as conn:
        cur = conn.execute(
            "UPDATE guest_tokens SET revoked=1 WHERE id=?", (token_id,)
        )
        conn.commit()
    return cur.rowcount > 0




def _wav_add_markers(wav_path: Path, markers: list[dict]):
    """Feature 7: Schreibt Marker in WAV.

    Fuer Adobe Audition sind klassische RIFF-WAV Marker am verlaesslichsten:
    - 'cue ' Chunk (Positions)
    - optional 'LIST'/'adtl' mit 'labl' Subchunks (Labels)

    Erwartet markers mit offset_ms/kind/note.
    """
    try:
        if not markers:
            return
        import struct

        data = wav_path.read_bytes()
        if data[0:4] != b'RIFF' or data[8:12] != b'WAVE':
            return

        # Parse fmt  + data chunk offset to convert ms->sample frames
        # Minimal RIFF scan
        riff_size = struct.unpack('<I', data[4:8])[0]
        pos = 12
        fmt = None
        data_chunk_found = False
        data_chunk_size = None
        while pos + 8 <= len(data):
            cid = data[pos:pos+4]
            csz = struct.unpack('<I', data[pos+4:pos+8])[0]
            cdata = pos + 8
            if cid == b'fmt ':
                if csz >= 16:
                    wFormatTag, nChannels, nSamplesPerSec, nAvgBytesPerSec, nBlockAlign, wBitsPerSample = struct.unpack('<HHIIHH', data[cdata:cdata+16])
                    fmt = {
                        'channels': nChannels,
                        'sr': nSamplesPerSec,
                        'blockAlign': nBlockAlign,
                        'bps': wBitsPerSample,
                    }
            if cid == b'data':
                data_chunk_found = True
                data_chunk_size = csz
                break
            pos = cdata + csz
            if csz % 2 == 1:
                pos += 1

        if not fmt or not data_chunk_found:
            return

        sr = int(fmt['sr'] or 48000)

        # Build cue points (sample offset in frames)
        # Sort by offset
        ms_sorted = sorted(markers, key=lambda m: int(m.get('offset_ms') or 0))

        def pack_chunk(cid: bytes, payload: bytes) -> bytes:
            # chunk header + payload + pad
            out = cid + struct.pack('<I', len(payload)) + payload
            if len(payload) % 2 == 1:
                out += b''
            return out

        cue_entries = []
        labl_entries = []
        for i, m in enumerate(ms_sorted, start=1):
            ms = int(m.get('offset_ms') or 0)
            sample_offset = int(round(ms * sr / 1000.0))
            cue_id = i
            # cue point structure (24 bytes)
            # dwName, dwPosition, fccChunk('data'), dwChunkStart(0), dwBlockStart(0), dwSampleOffset
            cue_entries.append(struct.pack('<II4sIII', cue_id, sample_offset, b'data', 0, 0, sample_offset))

            kind = str(m.get('kind') or '')
            note = str(m.get('note') or '')
            label = (kind + (': ' if note else '') + note).strip() or kind or 'marker'
            label_b = label.encode('utf-8', errors='ignore') + b''
            labl_payload = struct.pack('<I', cue_id) + label_b
            if len(labl_payload) % 2 == 1:
                labl_payload += b''
            labl_entries.append(b'labl' + struct.pack('<I', len(labl_payload)) + labl_payload)

        cue_payload = struct.pack('<I', len(cue_entries)) + b''.join(cue_entries)
        cue_chunk = pack_chunk(b'cue ', cue_payload)

        adtl_payload = b'adtl' + b''.join(labl_entries)
        list_chunk = pack_chunk(b'LIST', adtl_payload)

        # Append chunks at end of RIFF
        new_data = data + cue_chunk + list_chunk
        new_riff_size = riff_size + len(cue_chunk) + len(list_chunk)
        new_data = new_data[0:4] + struct.pack('<I', new_riff_size) + new_data[8:]
        wav_path.write_bytes(new_data)
    except Exception as e:
        print('[markers] WAV marker write failed:', e)

# ---------------------------------------------------------------------------
# Audio-Hilfsfunktionen
# ---------------------------------------------------------------------------

def _write_wav_from_pcm(chunks, dest_dir, sample_rate, channels):
    wav_path = dest_dir / "full.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(sample_rate)
        for c in chunks:
            w.writeframes(c.read_bytes())
    if not wav_path.exists() or wav_path.stat().st_size <= 44:
        raise HTTPException(500, "WAV-Erzeugung fehlgeschlagen (keine PCM-Daten)")
    return wav_path


def _transcode_webm_to_wav(chunks, dest_dir):
    tmp_webm = dest_dir / "_concat.webm"
    with tmp_webm.open("wb") as out:
        for c in chunks:
            out.write(c.read_bytes())
    wav_path = dest_dir / "full.wav"
    cmd = [FFMPEG, "-y", "-fflags", "+genpts", "-i", str(tmp_webm),
           "-vn", "-acodec", "pcm_s16le", "-ar", str(DEFAULT_SAMPLE_RATE),
           "-ac", "2", str(wav_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not wav_path.exists():
        try:
            tmp_webm.unlink()
        except OSError:
            pass
        raise HTTPException(500, "ffmpeg-Transkodierung fehlgeschlagen: "
                            + (proc.stderr or "")[-800:])
    return wav_path, tmp_webm


def _maybe_make_mp4(tmp_webm, dest_dir):
    """Feature 9: Video-Route neu validieren + H.264-Fallback.
    Versucht aus der zusammengefuegten WebM eine breit kompatible MP4
    (H.264/AAC) zu erzeugen. Schlaegt das fehl (z.B. kein Video-Track),
    wird still uebersprungen -- Audio bleibt unberuehrt.
    """
    if not tmp_webm or not tmp_webm.exists():
        return None
    # Pruefen, ob ueberhaupt ein Video-Stream vorhanden ist.
    probe = subprocess.run(
        [FFMPEG, "-i", str(tmp_webm)], capture_output=True, text=True)
    if "Video:" not in (probe.stderr or ""):
        return None
    mp4_path = dest_dir / "full.mp4"
    cmd = [FFMPEG, "-y", "-fflags", "+genpts", "-i", str(tmp_webm),
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", "-c:a", "aac", "-b:a", "192k",
           str(mp4_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not mp4_path.exists():
        print("[video] H.264-Transkodierung fehlgeschlagen:",
              (proc.stderr or "")[-300:])
        return None
    return mp4_path


def _session_wavs(room: str, session: str) -> list[tuple[str, Path]]:
    """Liefert alle fertigen Gast-WAVs einer Session, stabil nach Gast sortiert."""
    check_ident(room, session)
    room_dir = UPLOADS / room
    if not room_dir.exists():
        return []
    wavs = []
    for guest_dir in sorted(room_dir.iterdir()):
        if not guest_dir.is_dir() or guest_dir.name.startswith("."):
            continue
        wav = guest_dir / session / "full.wav"
        if wav.exists() and wav.stat().st_size > 44:
            wavs.append((guest_dir.name, wav))
    return wavs


def _mixdown_path(room: str, session: str) -> Path:
    check_ident(room, session)
    # Abgeleitete Dateien bewusst ausserhalb der Gast-/Session-Baumstruktur
    # halten, damit /sessions und Cleanup sie nicht als Gastaufnahme interpretieren.
    return DATA_DIR / "mixdowns" / room / session / "mixdown.mp3"


def _ensure_session_mixdown(room: str, session: str, force: bool = False) -> Path | None:
    """Erzeugt bzw. aktualisiert den MP3-Mixdown aller Gastspuren einer Session.

    Der Mix wird atomar ersetzt. Dadurch kann der Host nie eine halb geschriebene
    MP3 abrufen, waehrend ein weiterer Gast gerade fertig wird.
    """
    wavs = _session_wavs(room, session)
    if not wavs:
        return None
    dest = _mixdown_path(room, session)
    newest_wav = max(p.stat().st_mtime for _, p in wavs)
    if (not force and dest.exists() and dest.stat().st_size > 0
            and dest.stat().st_mtime >= newest_wav):
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name("mixdown.tmp.mp3")
    cmd = [FFMPEG, "-y"]
    for _, wav in wavs:
        cmd += ["-i", str(wav)]
    if len(wavs) == 1:
        cmd += ["-map", "0:a:0", "-vn", "-codec:a", "libmp3lame", "-b:a", "192k", str(tmp)]
    else:
        inputs = "".join(f"[{i}:a:0]" for i in range(len(wavs)))
        graph = f"{inputs}amix=inputs={len(wavs)}:duration=longest:dropout_transition=0:normalize=1[mix]"
        cmd += ["-filter_complex", graph, "-map", "[mix]", "-vn",
                "-codec:a", "libmp3lame", "-b:a", "192k", str(tmp)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        print("[mixdown] FFmpeg konnte nicht gestartet werden:", exc)
        return None
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        try:
            tmp.unlink()
        except OSError:
            pass
        print("[mixdown] Fehler:", (proc.stderr or "")[-800:])
        return None
    os.replace(tmp, dest)
    return dest


def safe(*parts):
    for p in parts:
        if not SAFE.match(p):
            raise HTTPException(400, "Ungueltiger Pfad-Bestandteil: " + p)
    return UPLOADS.joinpath(*parts)


def check_ident(*parts):
    for p in parts:
        if not SAFE.match(p):
            raise HTTPException(400, "Ungueltiger Bezeichner: " + p)


# ---------------------------------------------------------------------------
# Session / Auth
# ---------------------------------------------------------------------------

def _make_session_cookie(role: str = "admin") -> str:
    # Rolle ist im signierten Cookie hinterlegt: "role:admin" | "role:host"
    return _SIGNER.sign(f"role:{role}").decode()


def _session_role(token: str) -> str | None:
    """Gibt die Rolle des gueltigen Cookies zurueck, sonst None."""
    try:
        raw = _SIGNER.unsign(token, max_age=SESSION_MAX_AGE).decode()
    except Exception:
        return None
    if raw == "authenticated":      # Abwaertskompatibel zu alten Cookies
        return "admin"
    if raw.startswith("role:"):
        r = raw.split(":", 1)[1]
        return r if r in ("admin", "host") else None
    return None


def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    with _LOGIN_LOCK:
        attempts = [t for t in _LOGIN_ATTEMPTS.get(ip, [])
                    if now - t < LOGIN_WINDOW_SECONDS]
        if len(attempts) >= LOGIN_MAX_ATTEMPTS:
            _LOGIN_ATTEMPTS[ip] = attempts
            return False
        attempts.append(now)
        _LOGIN_ATTEMPTS[ip] = attempts
        return True


def require_auth(ps_session: str | None = Cookie(default=None)):
    """Beliebig eingeloggt (admin ODER host)."""
    if not ps_session or _session_role(ps_session) is None:
        raise HTTPException(
            status_code=303,
            headers={"Location": "/login"},
            detail="Nicht authentifiziert",
        )
    return _session_role(ps_session)


def require_admin(ps_session: str | None = Cookie(default=None)):
    """Nur die Admin-Rolle (Feature 13)."""
    role = _session_role(ps_session) if ps_session else None
    if role is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"},
                            detail="Nicht authentifiziert")
    if role != "admin":
        raise HTTPException(status_code=403, detail="Nur fuer Admins")
    return role


# ---------------------------------------------------------------------------
# Raum-State (In-Memory)
# ---------------------------------------------------------------------------
ROOMS = {}
GUEST_CONSOLE = {}  # room -> list[dict]
_CONSOLE_LOCK = threading.Lock()

_LOCK = threading.Lock()


def _room(room):
    r = ROOMS.get(room)
    if r is None:
        r = {"command":  {"action": None, "start_at": None, "session": None, "issued_at": 0},
             "settings": {"audio_only": True, "debug_level": 0},
             "guests":   {}}
        ROOMS[room] = r
    return r


def _prune(room_obj):
    now = time.time()
    dead = [g for g, info in room_obj["guests"].items()
            if now - info.get("last_seen", 0) > GUEST_FORGET_AFTER]
    for g in dead:
        room_obj["guests"].pop(g, None)


def _prune_level_throttle(max_age: float = 300.0) -> None:
    """Raeumt die Pegel-Drossel-Map auf.

    _LEVEL_LAST_SENT bekommt pro Raum einen Eintrag und wurde frueher nie
    geleert -> bei vielen kurzlebigen Raeumen ein langsam wachsendes Leck.
    Eintraege, die laenger als max_age nicht mehr angefasst wurden, gehoeren
    zu Raeumen ohne aktive Gaeste und koennen weg.
    """
    now = time.time()
    stale = [rm for rm, ts in _LEVEL_LAST_SENT.items() if now - ts > max_age]
    for rm in stale:
        _LEVEL_LAST_SENT.pop(rm, None)


# ---------------------------------------------------------------------------
# WebSocket-Hub (Phase 5: Echtzeit-Kanal)
# ---------------------------------------------------------------------------
# Pro Raum halten wir zwei Mengen offener WebSockets:
#   - host_sockets[room]  : Host-/Admin-Panels (empfangen den Raum-Status-Push)
#   - guest_sockets[room] : Gast-Recorder      (empfangen command + settings)
# Der In-Memory-Zustand ROOMS bleibt die einzige Quelle der Wahrheit. Bei jeder
# Aenderung (Trigger, Settings, Marker, Heartbeat) pushen wir an die passende
# Gruppe. Der Chunk-Upload laeuft UNVERAENDERT ueber HTTP (PUT /upload ...).
#
# Wichtig: WebSockets leben im asyncio-Loop. Die Sende-Funktionen sind async.
# Aus synchronen HTTP-Routen (Trigger/Settings/Marker) stossen wir den Broadcast
# ueber den laufenden Event-Loop an (run_coroutine_threadsafe-frei, da FastAPI-
# Handler async sind -> wir machen die relevanten Routen async und awaiten).
_WS_HOSTS:  dict[str, set] = {}
_WS_GUESTS: dict[str, set] = {}
_WS_LOCK = threading.Lock()


def _ws_add(bucket: dict, room: str, ws) -> None:
    with _WS_LOCK:
        bucket.setdefault(room, set()).add(ws)


def _ws_remove(bucket: dict, room: str, ws) -> None:
    with _WS_LOCK:
        s = bucket.get(room)
        if s:
            s.discard(ws)
            if not s:
                bucket.pop(room, None)


def _ws_targets(bucket: dict, room: str) -> list:
    with _WS_LOCK:
        return list(bucket.get(room, ()))


async def _ws_send(ws, payload: dict) -> bool:
    try:
        await ws.send_json(payload)
        return True
    except Exception:
        return False


async def _broadcast_guests(room: str) -> None:
    """Schickt command + settings + server_time an alle Gaeste eines Raums."""
    with _LOCK:
        r = _room(room)
        msg = {
            "type":        "command",
            "command":     dict(r["command"]),
            "settings":    dict(r["settings"]),
            "server_time": int(time.time() * 1000),
            # issued_at steckt in command, aber wir lassen es explizit drin und
            # sorgen hier dafür, dass es immer mitkommt (für Client-Dedupe/Debug).
        }
    for ws in _ws_targets(_WS_GUESTS, room):
        ok = await _ws_send(ws, msg)
        if not ok:
            _ws_remove(_WS_GUESTS, room, ws)


async def _broadcast_host_status(room: str) -> None:
    """Schickt den vollstaendigen Raum-Status an alle Host-Panels eines Raums."""
    payload = _build_status(room)
    payload["type"] = "status"
    for ws in _ws_targets(_WS_HOSTS, room):
        ok = await _ws_send(ws, payload)
        if not ok:
            _ws_remove(_WS_HOSTS, room, ws)


# --- Sprint 2 (QA): leichtgewichtiger Pegel-Kanal --------------------------
# Der volle Status-Push (inkl. Marker-DB-Query) ist zu teuer, um ihn mehrmals
# pro Sekunde zu senden. Gaeste schicken darum zusaetzlich zum 2s-Heartbeat
# ein sehr kleines {"type":"level"}-Paket (~250ms), das nur den RMS traegt.
# Wir spiegeln es unveraendert an die Host-Panels; der Host glaettet lokal.
LEVEL_MIN_INTERVAL = 0.12          # Broadcast-Drossel pro Raum (Sekunden)
_LEVEL_LAST_SENT: dict[str, float] = {}


async def _broadcast_host_levels(room: str) -> None:
    """Schickt nur die aktuellen Pegel aller Gaeste an die Host-Panels."""
    now = time.time()
    last = _LEVEL_LAST_SENT.get(room, 0.0)
    if now - last < LEVEL_MIN_INTERVAL:
        return
    _LEVEL_LAST_SENT[room] = now
    targets = _ws_targets(_WS_HOSTS, room)
    if not targets:
        return
    with _LOCK:
        r = _room(room)
        levels = [{"guest": g,
                   "rms":  float(i.get("rms", 0.0) or 0.0),
                   "peak": float(i.get("peak", 0.0) or 0.0)}
                  for g, i in r["guests"].items()]
    msg = {"type": "levels", "room": room, "levels": levels,
           "server_time": int(now * 1000)}
    for ws in targets:
        if not await _ws_send(ws, msg):
            _ws_remove(_WS_HOSTS, room, ws)


def _build_status(room: str) -> dict:
    """Erzeugt das Status-Objekt (Gaeste, command, settings, marker) -- die
    gemeinsame Basis fuer HTTP /host/status und den WebSocket-Push."""
    now_s = time.time()
    with _LOCK:
        r = _room(room)
        _prune(r)
        guests = []
        for info in r["guests"].values():
            age  = now_s - info.get("last_seen", 0)
            conn = ("online" if age <= GUEST_STALE_AFTER
                    else "stale" if age <= GUEST_OFFLINE_AFTER
                    else "offline")
            row  = {k: info.get(k) for k in (
                "guest", "display_name", "session", "state",
                "mic_label", "speaker_label", "rms", "queue", "rec_mb", "up_mb")}
            # Sprint 2: Mic-Inventar + aktuelles Geraet + Wechsel-Status.
            row["mic_devices"]          = info.get("mic_devices", [])
            row["current_mic_deviceId"] = info.get("current_mic_deviceId", "")
            row["active_mic_deviceId"]  = info.get("active_mic_deviceId", "")
            row["mic_active"]           = bool(info.get("mic_active", True))
            row["mic_alert"]            = info.get("mic_alert")
            row["mic_lost_during_recording"] = bool(info.get("mic_lost_during_recording"))
            # Auswahl != aktives Geraet -> der Host sieht die Abweichung direkt.
            row["mic_mismatch"] = bool(
                info.get("current_mic_deviceId") and info.get("active_mic_deviceId")
                and info.get("current_mic_deviceId") != info.get("active_mic_deviceId"))
            row["mic_pending"]          = bool(info.get("mic_cmd"))
            row["mic_last_result"]      = info.get("mic_last_result")
            row["connection"]         = conn
            row["seconds_since_seen"] = round(age, 1)
            guests.append(row)
        guests.sort(key=lambda x: (x.get("display_name") or x.get("guest") or "").lower())
        cmd      = dict(r["command"])
        settings = dict(r["settings"])
        cur_session = r.get("rec_session", "")
    markers = _marker_list(room, cur_session) if cur_session else []
    # Fix (Review): Der Host brauchte bisher pro Status-Push zwei zusaetzliche
    # HTTP-Requests, um die Marker der im Dropdown gewaehlten Session zu holen.
    # Wir liefern die bekannten Sessions gleich mit, damit der Client nur noch
    # bei einem echten Session-Wechsel nachladen muss.
    try:
        sessions_known = _marker_sessions(room)
    except Exception:
        sessions_known = []
    return {
        "ok": True, "room": room, "server_time": int(now_s * 1000),
        "command": cmd, "settings": settings, "guests": guests,
        "online_count": sum(1 for g in guests if g["connection"] == "online"),
        "markers": markers, "session": cur_session,
        "marker_sessions": sessions_known,
    }


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI()


# ── Login / Logout ──────────────────────────────────────────────────────────

@app.get("/login")
def login_page():
    return _render_page("login.html")


@app.post("/login")
async def login(request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        return JSONResponse(
            {"ok": False, "error": "Zu viele Anmeldeversuche. Bitte warte eine Minute."},
            status_code=429)
    try:
        form     = await request.form()
        password = str(form.get("password", ""))
    except Exception:
        return JSONResponse({"ok": False, "error": "Ungueltige Anfrage."}, status_code=400)

    role = _check_password(password)
    if role is None:
        return JSONResponse({"ok": False, "error": "Falsches Passwort."}, status_code=401)

    resp = JSONResponse({"ok": True, "redirect": "/", "role": role})
    resp.set_cookie(key=COOKIE_NAME, value=_make_session_cookie(role),
                    max_age=SESSION_MAX_AGE, httponly=True,
                    samesite="strict", secure=False, path="/")
    return resp


@app.post("/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(key=COOKIE_NAME, path="/")
    return resp


# ── Geschuetzte Seiten ───────────────────────────────────────────────────────

@app.get("/")
@app.get("/index.html")
def index(_auth=Depends(require_auth)):
    return _render_page("index.html")


@app.get("/me")
def whoami(role: str = Depends(require_auth)):
    return {"ok": True, "role": role}


# ---------------------------------------------------------------------------
# Sprint 3: Branding flickerfrei ausliefern
# ---------------------------------------------------------------------------
# Frueher holte jede Seite das Branding per fetch('/branding') NACH dem ersten
# Paint. Bis die Antwort da war, galt der CSS-Default (--brand: var(--ok)) ->
# beim Reload blitzte kurz Gruen auf den Buttons auf. Workaround war ein
# "visibility:hidden bis Branding da"-Hack, der die Seite flackern/springen
# liess.
#
# Jetzt rendert der Server die Branding-Variablen direkt in den <head> der
# HTML-Seite. Beim ersten Paint stimmen die Farben bereits -- kein FOUC,
# kein Verstecken der Seite, kein zusaetzlicher Request.
# Semantische Farben (--ok/--warn/--accent) werden bewusst NICHT angefasst.

def _hex_parse(color: str) -> tuple[int, int, int] | None:
    m = re.match(r"^#([0-9a-fA-F]{6})$", str(color or "").strip())
    if not m:
        return None
    n = int(m.group(1), 16)
    return (n >> 16) & 255, (n >> 8) & 255, n & 255


def _brand_hover(rgb: tuple[int, int, int]) -> str:
    """Hover-Farbe: ~18 % Richtung Weiss aufhellen (wie bisher im Client)."""
    mix = lambda c: max(0, min(255, round(c + (255 - c) * 0.18)))
    return "#" + "".join(f"{mix(c):02x}" for c in rgb)


def _brand_on(rgb: tuple[int, int, int]) -> str:
    """Textfarbe auf Brand-Flaechen nach WCAG-Kontrast (schwarz oder weiss)."""
    def lin(v: float) -> float:
        s = v / 255.0
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    L = 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2])
    c_white = (max(L, 1.0) + 0.05) / (min(L, 1.0) + 0.05)
    c_black = (max(L, 0.0) + 0.05) / (min(L, 0.0) + 0.05)
    return "#000" if c_black >= c_white else "#fff"


def _branding_head() -> str:
    """Baut den <head>-Block: Farbvariablen, Titel-Suffix, Favicon, JSON."""
    cfg = _cfg_load()
    name    = str(cfg.get("brand_name", "Podcast Studio"))
    color   = str(cfg.get("brand_color", "#30a46c"))
    favicon = str(cfg.get("brand_favicon", ""))

    rgb = _hex_parse(color)
    parts = []
    if rgb:
        parts.append(
            ":root{"
            f"--brand:{color};"
            f"--brand-hover:{_brand_hover(rgb)};"
            f"--brand-on:{_brand_on(rgb)};"
            "}"
        )
    css = f"<style id=\"brand-vars\">{''.join(parts)}</style>" if parts else ""

    ico = ""
    if favicon and favicon.startswith(("data:", "/", "http")):
        ico = f'<link rel="icon" href="{html_escape(favicon, quote=True)}">'

    payload = json_dumps({"ok": True, "name": name, "color": color,
                          "favicon": favicon})
    js = f"<script>window.__BRANDING__={payload};</script>"
    return css + ico + js


def _render_page(filename: str, status_code: int = 200) -> HTMLResponse:
    """Liefert eine HTML-Seite mit serverseitig eingesetztem Branding aus.

    Der Marker <!--BRANDING--> steht in jeder Seite als LETZTES Element im
    <head>. Dadurch gewinnen die injizierten :root-Variablen gegen die
    Default-Werte im Seiten-CSS (gleiche Spezifitaet -> letzte Regel gewinnt)
    und die Farben stimmen bereits beim ersten Paint.
    """
    path = BASE / filename
    try:
        html = path.read_text(encoding="utf-8")
    except OSError:
        raise HTTPException(404, "Seite nicht gefunden")

    block = _branding_head()
    marker = "<!--BRANDING-->"
    if marker in html:
        # Kein re.sub -> keine Backslash-/Gruppen-Escapes im Ersetzungstext.
        html = html.replace(marker, block, 1)
    else:
        # Fallback: direkt vor </head> einsetzen (nicht nach <head>, sonst
        # ueberschreibt das Seiten-CSS die Branding-Variablen wieder).
        idx = html.lower().find("</head>")
        if idx != -1:
            html = html[:idx] + block + html[idx:]
        else:
            html = block + html

    return HTMLResponse(html, status_code=status_code,
                        headers={"Cache-Control": "no-store"})


@app.get("/branding")
def branding():
    """Oeffentliches Branding (Name/Farbe/Favicon) fuer alle Seiten -- Feature 8."""
    cfg = _cfg_load()
    return {
        "ok": True,
        "name":    cfg.get("brand_name", "Podcast Studio"),
        "color":   cfg.get("brand_color", "#30a46c"),
        "favicon": cfg.get("brand_favicon", ""),
    }


@app.get("/admin.html")
@app.get("/admin")
def admin(_role=Depends(require_admin)):
    return _render_page("admin.html")


@app.get("/host.html")
@app.get("/host")
def host(_auth=Depends(require_auth)):
    return _render_page("host.html")


# ── Recorder: Token-Pruefung ─────────────────────────────────────────────────

@app.get("/recorder.html")
def recorder(token: str | None = None):
    """
    Ohne Token oder mit ungueltigem/abgelaufenem Token -> token_error.html.
    Mit gueltigem Token -> recorder.html ausliefern.
    Das JS im Recorder holt den Raum dann via /token/resolve.
    """
    if not token:
        return _render_page("token_error.html", status_code=403)
    info = _token_resolve(token)
    if info is None:
        return _render_page("token_error.html", status_code=403)
    return _render_page("recorder.html")


@app.get("/token/resolve")
def token_resolve(token: str | None = None):
    """
    Gibt {ok, room, expires_at} zurueck, wenn der Token gueltig ist.
    Wird vom Recorder-JS beim Start einmalig aufgerufen.
    """
    if not token:
        raise HTTPException(403, "Kein Token angegeben")
    info = _token_resolve(token)
    if info is None:
        raise HTTPException(403, "Token ungueltig oder abgelaufen")
    return {
        "ok":         True,
        "room":       info["room"],
        "expires_at": info["expires_at"],
        "label":      info.get("label", ""),
    }


# ── Host: Token-Verwaltung ───────────────────────────────────────────────────

@app.post("/host/token/{room}")
async def host_token_create(room, _auth=Depends(require_auth)):
    """Token fuer einen Raum erstellen.

    Regel: pro Raum soll es immer nur EINEN gueltigen Token geben.
    - Gibt es bereits einen aktiven Token (nicht revoked, nicht expired), wird
      dieser zurueckgegeben (idempotent) und KEIN neuer erzeugt.
    - Gibt es keinen aktiven Token (abgelaufen / widerrufen / geloescht), wird
      ein neuer erzeugt.

    Laufzeit kommt aus der globalen Konfig (token_days).
    Label ist automatisch der Raumname.
    """
    check_ident(room)
    cfg = _cfg_load()
    if room in set(cfg.get("archived_rooms", [])):
        raise HTTPException(400, "Raum ist archiviert (keine neuen Gastlinks)")

    _room_register(room)  # Raum sofort registrieren (Admin-Uebersicht)

    # Bereits aktiven Token fuer diesen Raum finden
    now = time.time()
    with _DB_LOCK, _db_conn() as conn:
        row = conn.execute(
            "SELECT id, token, room, label, created_at, expires_at, revoked "
            "FROM guest_tokens "
            "WHERE room=? AND revoked=0 AND expires_at>? "
            "ORDER BY created_at DESC LIMIT 1",
            (room, now),
        ).fetchone()

    if row:
        tok = dict(row)
        return {
            "ok":         True,
            "id":         tok["id"],
            "token":      tok["token"],
            "room":       tok["room"],
            "label":      tok.get("label", "") or room,
            "expires_at": tok["expires_at"],
            "link":       f"/recorder.html?token={tok['token']}",
            "existing":   True,
        }

    days  = int(cfg.get("token_days", _cfg_get("token_days")))
    label = room
    tok   = _token_create(room, days, label)
    return {
        "ok":         True,
        "id":         tok["id"],
        "token":      tok["token"],
        "room":       tok["room"],
        "label":      tok["label"],
        "expires_at": tok["expires_at"],
        "link":       f"/recorder.html?token={tok['token']}",
        "existing":   False,
    }


@app.get("/host/tokens/{room}")
def host_token_list(room, active_only: int = 0, _auth=Depends(require_auth)):
    """Token eines Raums.
    Feature 11: Das Host-Panel ruft mit active_only=1 auf und sieht so nur
    aktive Token (widerrufene/abgelaufene verschwinden aus der Anzeige).
    Das Admin-Panel ruft ohne Flag auf und sieht die vollstaendige Liste.
    """
    check_ident(room)
    now  = time.time()
    toks = _token_list(room)
    for t in toks:
        t["expired"] = t["expires_at"] < now
        t["active"]  = not t["revoked"] and not t["expired"]
    if active_only:
        toks = [t for t in toks if t["active"]]
    return {"ok": True, "room": room, "tokens": toks}


@app.delete("/host/token/hard/{token_id}")
def host_token_hard_delete(token_id: str, _auth=Depends(require_auth)):
    """Feature 11: Token wirklich aus der DB entfernen, sodass er auch aus der
    Host-Anzeige verschwindet (nicht nur widerrufen)."""
    if not re.match(r"^[0-9a-f]{8}$", token_id):
        raise HTTPException(400, "Ungueltige Token-ID")
    with _DB_LOCK, _db_conn() as conn:
        cur = conn.execute("DELETE FROM guest_tokens WHERE id=?", (token_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "Token nicht gefunden")
    return {"ok": True, "deleted": token_id}


@app.delete("/host/token/{token_id}")
def host_token_revoke(token_id: str, _auth=Depends(require_auth)):
    """Token widerrufen (permanent, nicht loeschbar)."""
    if not re.match(r"^[0-9a-f]{8}$", token_id):
        raise HTTPException(400, "Ungueltige Token-ID")
    ok = _token_revoke(token_id)
    if not ok:
        raise HTTPException(404, "Token nicht gefunden")
    return {"ok": True, "revoked": token_id}


# ── Admin: Alle Gast-Token raumuebergreifend ─────────────────────────────────

@app.get("/admin/tokens")
def admin_token_list(_role=Depends(require_admin)):
    """Liste ALLER Gast-Token (alle Raeume) mit Status fuer das Admin-Panel.
    Liefert keine vollstaendigen Token-Werte zurueck, sondern nur eine kurze
    Vorschau (erste/letzte Zeichen) -- der vollstaendige Token ist ein Geheimnis
    und wird nur einmalig bei der Erzeugung im Host-Studio gezeigt.
    """
    now = time.time()
    with _DB_LOCK, _db_conn() as conn:
        rows = conn.execute(
            "SELECT id, token, room, label, created_at, expires_at, revoked "
            "FROM guest_tokens ORDER BY created_at DESC"
        ).fetchall()
    out = []
    for r in rows:
        tok = r["token"] or ""
        preview = (tok[:6] + "…" + tok[-4:]) if len(tok) > 12 else "…"
        expired = r["expires_at"] < now
        out.append({
            "id":            r["id"],
            "room":          r["room"],
            "label":         r["label"],
            "token_preview": preview,
            "created_at":    r["created_at"],
            "expires_at":    r["expires_at"],
            "revoked":       bool(r["revoked"]),
            "expired":       expired,
            "active":        (not r["revoked"]) and (not expired),
        })
    return {"ok": True, "tokens": out, "server_time": int(now * 1000)}


@app.delete("/admin/token/hard/{token_id}")
def admin_token_delete(token_id: str, _role=Depends(require_admin)):
    """Token endgueltig aus der Datenbank loeschen (Admin).
    WICHTIG: Diese spezifischere Route muss VOR /admin/token/{token_id}
    deklariert werden, sonst faengt der generische Pfad 'hard' als token_id ab.
    """
    if not re.match(r"^[0-9a-f]{8}$", token_id):
        raise HTTPException(400, "Ungueltige Token-ID")
    with _DB_LOCK, _db_conn() as conn:
        cur = conn.execute("DELETE FROM guest_tokens WHERE id=?", (token_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "Token nicht gefunden")
    return {"ok": True, "deleted": token_id}


@app.delete("/admin/token/{token_id}")
def admin_token_revoke(token_id: str, _role=Depends(require_admin)):
    """Token widerrufen (Admin). Der Link wird sofort ungueltig, der Eintrag
    bleibt zur Nachvollziehbarkeit erhalten."""
    if not re.match(r"^[0-9a-f]{8}$", token_id):
        raise HTTPException(400, "Ungueltige Token-ID")
    if not _token_revoke(token_id):
        raise HTTPException(404, "Token nicht gefunden")
    return {"ok": True, "revoked": token_id}


# ── Health ───────────────────────────────────────────────────────────────────

@app.head("/health")
@app.get("/health")
def health():
    return {"ok": True}


# ── Gast-API (offen) ─────────────────────────────────────────────────────────

@app.put("/upload/{room}/{guest}/{session}/{chunk}")
async def upload(room, guest, session, chunk, request: Request, ext: str = "pcm"):
    if not re.match(r"^\d{6}$", chunk):
        raise HTTPException(400, "Chunk-Name muss 6-stellige Zahl sein")
    # Feature 9: Audio-Chunks kommen als rohes PCM (.pcm), Video-Chunks als
    # WebM-Container-Fragmente (.webm). Andere Endungen werden abgelehnt.
    if ext not in ("pcm", "webm"):
        raise HTTPException(400, "Unbekannte Chunk-Endung")
    dest_dir = safe(room, guest, session)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / ("chunk-" + chunk + "." + ext)
    data = await request.body()
    dest.write_bytes(data)
    return {"ok": True, "bytes": len(data), "path": str(dest.relative_to(BASE))}


@app.post("/meta/{room}/{guest}/{session}")
async def meta(room, guest, session, request: Request):
    dest_dir = safe(room, guest, session)
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    try:
        sr = int(payload.get("sample_rate") or DEFAULT_SAMPLE_RATE)
    except (TypeError, ValueError):
        sr = DEFAULT_SAMPLE_RATE
    try:
        ch = int(payload.get("channels") or DEFAULT_CHANNELS)
    except (TypeError, ValueError):
        ch = DEFAULT_CHANNELS
    sr = max(8000, min(192000, sr))
    ch = max(1, min(2, ch))
    (dest_dir / "meta.json").write_text(json.dumps({"sample_rate": sr, "channels": ch}))
    return {"ok": True, "sample_rate": sr, "channels": ch}


@app.post("/finish/{room}/{guest}/{session}")
async def finish(room, guest, session):
    dest_dir = safe(room, guest, session)
    if not dest_dir.exists():
        raise HTTPException(404, "Session nicht gefunden")

    pcm_chunks  = sorted(dest_dir.glob("chunk-*.pcm"))
    webm_chunks = sorted(dest_dir.glob("chunk-*.webm"))

    if pcm_chunks:
        sample_rate, channels = DEFAULT_SAMPLE_RATE, DEFAULT_CHANNELS
        meta_file = dest_dir / "meta.json"
        if meta_file.exists():
            try:
                m           = json.loads(meta_file.read_text())
                sample_rate = int(m.get("sample_rate", sample_rate))
                channels    = int(m.get("channels", channels))
            except Exception:
                pass
        wav_path = _write_wav_from_pcm(pcm_chunks, dest_dir, sample_rate, channels)
        n_chunks = len(pcm_chunks)
    elif webm_chunks:
        wav_path, tmp_webm = _transcode_webm_to_wav(webm_chunks, dest_dir)
        n_chunks = len(webm_chunks)
        # Feature 9: H.264/MP4-Fallback erzeugen (falls Video vorhanden), dann aufraeumen.
        try:
            _maybe_make_mp4(tmp_webm, dest_dir)
        except Exception as e:
            print("[video] Fallback-Fehler:", e)
        try:
            tmp_webm.unlink()
        except OSError:
            pass
    else:
        raise HTTPException(404, "Keine Chunks vorhanden")

    # Feature 7: Marker in WAV schreiben (nur Marker dieser Session)
    try:
        _wav_add_markers(wav_path, _marker_list(room, session))
    except Exception as e:
        print("[markers] Fehler:", e)

    with _LOCK:
        r = ROOMS.get(room)
        if r and guest in r["guests"]:
            r["guests"][guest]["state"] = "done"
            r["guests"][guest]["queue"] = 0

    # Host-Panels live ueber den Abschluss informieren.
    try:
        await _broadcast_host_status(room)
    except Exception:
        pass

    # Nach jedem fertiggestellten Gast die gemeinsame Session-MP3 aktualisieren.
    # Bereits fertige Gastspuren werden dabei zusammen mit der neuen Spur gemischt.
    mixdown = _ensure_session_mixdown(room, session, force=True)

    return {"ok": True, "chunks": n_chunks,
            "merged": str(wav_path.relative_to(BASE)),
            "mixdown": f"/host/mixdown/{room}/{session}" if mixdown else None,
            "size_mb": round(wav_path.stat().st_size / 1024 / 1024, 2)}


# ── Host-API (geschuetzt) ─────────────────────────────────────────────────────

@app.get("/host/status/{room}")
def host_status(room, _auth=Depends(require_auth)):
    check_ident(room)
    _room_register(room)   # Raum sofort in der Admin-Uebersicht sichtbar machen
    return JSONResponse(_build_status(room))


@app.get("/host/marker_sessions/{room}")
def host_marker_sessions(room: str, _auth=Depends(require_auth)):
    """Liefert alle Sessions, für die es Marker in der DB gibt (absteigend)."""
    check_ident(room)
    return {"ok": True, "room": room, "sessions": _marker_sessions(room)}


@app.post("/host/room/{room}/ensure")
def host_room_ensure(room, _auth=Depends(require_auth)):
    """Registriert einen Raum, sobald der Host das Studio oeffnet -- damit er
    sofort (vor dem ersten Token/der ersten Aufnahme) in /admin/rooms erscheint."""
    check_ident(room)
    _room_register(room)
    return {"ok": True, "room": room}


@app.post("/host/trigger/{room}")
async def host_trigger(room, request: Request, _auth=Depends(require_auth)):
    check_ident(room)
    _room_register(room)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    action = payload.get("action")
    now_ms = int(time.time() * 1000)
    with _LOCK:
        r = _room(room)
        if action == "start":
            # Gemeinsame Session-ID fuer diese Aufnahme (Host + alle Gaeste + Marker + Historie).
            # Wir verwenden eine kurze Base36-ID (lesbar/kompakt), bleibt aber eindeutig genug.
            sid = _new_session_id()
            r["command"] = {"action": "start",
                            "start_at": now_ms + int(START_LEAD_SECONDS * 1000),
                            "session": sid, "issued_at": now_ms}
            # Aufnahme-Startzeit merken (fuer Marker-Offsets).
            r["rec_started_at"] = now_ms + int(START_LEAD_SECONDS * 1000)
            r["rec_session"]    = sid
        elif action == "stop":
            # Session bleibt die aktuelle Aufnahme-Session (nicht neu generieren)
            r["command"] = {"action": "stop", "start_at": None,
                            "session": r.get("rec_session", ""), "issued_at": now_ms}
        elif action == "clear":
            r["command"] = {"action": None, "start_at": None,
                            "session": None, "issued_at": now_ms}
        else:
            raise HTTPException(400, "action muss start, stop oder clear sein")
        cmd = dict(r["command"])
    # Echtzeit-Push: Gaeste bekommen den Befehl sofort, Host-Panels den Status.
    await _broadcast_guests(room)
    await _broadcast_host_status(room)
    return {"ok": True, "command": cmd, "server_time": now_ms}


@app.post("/host/settings/{room}")
async def host_settings(room, request: Request, _auth=Depends(require_auth)):
    check_ident(room)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    now_ms = int(time.time() * 1000)
    with _LOCK:
        r = _room(room)
        s = r["settings"]
        if "audio_only" in payload:
            s["audio_only"] = bool(payload.get("audio_only"))
        if "debug_level" in payload:
            try:
                lvl = int(payload.get("debug_level") or 0)
            except (TypeError, ValueError):
                lvl = 0
            s["debug_level"] = max(0, min(2, lvl))
        settings = dict(s)
    await _broadcast_guests(room)
    await _broadcast_host_status(room)
    return {"ok": True, "settings": settings, "server_time": now_ms}


# ── Host-API: Marker ─────────────────────────────────────────────────────────

@app.post("/host/marker/{room}")
async def host_marker_create(room, request: Request, _auth=Depends(require_auth)):
    """Setzt einen Marker waehrend (oder nach) der Aufnahme.
    Body: { kind: 'ad'|'cut_in'|'cut_out', note?: str }
    Der Offset (ms seit Aufnahmestart) wird serverseitig berechnet.
    """
    check_ident(room)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    kind = str(payload.get("kind", ""))
    if kind not in MARKER_KINDS:
        raise HTTPException(400, "kind muss 'ad', 'cut_in' oder 'cut_out' sein")
    note = str(payload.get("note", ""))
    now_ms = int(time.time() * 1000)
    with _LOCK:
        r = _room(room)
        started = r.get("rec_started_at") or now_ms
        session = r.get("rec_session", "")
    offset_ms = max(0, now_ms - int(started))
    m = _marker_create(room, session, kind, offset_ms, note)
    await _broadcast_host_status(room)
    return {"ok": True, "marker": m}


@app.post("/host/marker/{marker_id}/note")
async def host_marker_set_note(marker_id: str, request: Request, _auth=Depends(require_auth)):
    """Setzt/aktualisiert die Notiz eines Markers.
    Body: { note: str }
    """
    if not re.match(r"^[0-9a-f]{8}$", marker_id):
        raise HTTPException(400, "Ungueltige Marker-ID")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    note = str(payload.get("note", ""))[:200]

    room = None
    with _DB_LOCK, _db_conn() as conn:
        row = conn.execute("SELECT room FROM markers WHERE id=?", (marker_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Marker nicht gefunden")
        room = row["room"]
        conn.execute("UPDATE markers SET note=? WHERE id=?", (note, marker_id))
        conn.commit()

    if room:
        await _broadcast_host_status(room)
    return {"ok": True, "id": marker_id, "note": note}


@app.post("/host/marker/{marker_id}/shift")
async def host_marker_shift(marker_id: str, request: Request, _auth=Depends(require_auth)):
    """Verschiebt einen Marker zeitlich um delta_ms (kann negativ sein).
    Body: { delta_ms: int }

    Hinweis: Offset ist ms seit Aufnahmestart; clamp >= 0.
    """
    if not re.match(r"^[0-9a-f]{8}$", marker_id):
        raise HTTPException(400, "Ungueltige Marker-ID")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    try:
        delta = int(payload.get("delta_ms") or 0)
    except (TypeError, ValueError):
        delta = 0
    delta = max(-600000, min(600000, delta))  # +/- 10 Minuten Safety

    room = None
    with _DB_LOCK, _db_conn() as conn:
        row = conn.execute("SELECT room, offset_ms FROM markers WHERE id=?", (marker_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Marker nicht gefunden")
        room = row["room"]
        new_off = max(0, int(row["offset_ms"] or 0) + delta)
        conn.execute("UPDATE markers SET offset_ms=? WHERE id=?", (new_off, marker_id))
        conn.commit()

    if room:
        await _broadcast_host_status(room)
    return {"ok": True, "id": marker_id, "delta_ms": delta, "offset_ms": new_off}


@app.get("/host/markers/{room}")
def host_marker_list(room, session: str | None = None, _auth=Depends(require_auth)):
    """Marker eines Raums (optional auf eine Session gefiltert)."""
    check_ident(room)
    markers = _marker_list(room, session)
    return {"ok": True, "room": room, "markers": markers}


@app.post("/host/apply_markers/{room}")
async def host_apply_markers(room: str, request: Request, _role=Depends(require_admin)):
    """Wendet die Marker einer Session auf alle full.wav dieser Session an.

    Body: { session: str }

    Überschreibt die WAV-Datei in-place (RIFF cue/LIST adtl wird neu geschrieben).
    """
    check_ident(room)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    session = str(payload.get("session") or "")[:40]
    if not session:
        raise HTTPException(400, "session fehlt")

    markers = _marker_list(room, session)

    room_dir = safe(room)
    if not room_dir.exists() or not room_dir.is_dir():
        raise HTTPException(404, "Raum nicht gefunden")

    updated = []
    for guest_dir in sorted(room_dir.iterdir()):
        if not guest_dir.is_dir():
            continue
        sess_dir = guest_dir / session
        wav_path = sess_dir / "full.wav"
        if wav_path.exists() and wav_path.stat().st_size > 44:
            try:
                _wav_add_markers(wav_path, markers)
                updated.append(str(wav_path.relative_to(BASE)))
            except Exception as e:
                print("[markers] apply failed:", e)

    if not updated:
        raise HTTPException(404, "Keine full.wav für diese Session gefunden")

    # Marker veraendern die WAV-Dateien; deshalb den abgeleiteten Mixdown erneuern.
    _ensure_session_mixdown(room, session, force=True)
    await _broadcast_host_status(room)
    return {"ok": True, "room": room, "session": session, "markers": len(markers), "wavs": updated}


@app.delete("/host/marker/{marker_id}")
async def host_marker_delete(marker_id: str, _auth=Depends(require_auth)):
    if not re.match(r"^[0-9a-f]{8}$", marker_id):
        raise HTTPException(400, "Ungueltige Marker-ID")
    # Raum des Markers ermitteln, damit der Status-Push den richtigen Raum trifft.
    room = None
    try:
        with _DB_LOCK, _db_conn() as conn:
            row = conn.execute("SELECT room FROM markers WHERE id=?", (marker_id,)).fetchone()
            if row:
                room = row["room"]
    except Exception:
        pass
    if not _marker_delete(marker_id):
        raise HTTPException(404, "Marker nicht gefunden")
    if room:
        await _broadcast_host_status(room)
    return {"ok": True, "deleted": marker_id}


# ── Download (geschuetzt, korrekter Dateiname + MIME) ─────────────────────────

@app.get("/download/{room}/{guest}/{session}")
def download_recording(room, guest, session, _role=Depends(require_admin)):
    """Liefert die fertige WAV mit korrektem Content-Type und sprechendem
    Dateinamen aus. Behebt den Bug, bei dem der Browser sonst eine
    JSON-Fehlerseite als '.json' speichert bzw. eine falsche Endung waehlt.
    """
    dest_dir = safe(room, guest, session)
    wav_path = dest_dir / "full.wav"
    if not wav_path.exists():
        raise HTTPException(404, "Aufnahme noch nicht zusammengefuehrt (full.wav fehlt)")
    filename = f"{room}_{guest}_{session}.wav"
    return FileResponse(
        str(wav_path),
        media_type="audio/wav",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Admin: Globale Konfig ────────────────────────────────────────────────────

@app.get("/admin/config")
def admin_config_get(_auth=Depends(require_auth)):
    """Globale Einstellungen lesen."""
    return {"ok": True, "config": _cfg_load()}


@app.post("/admin/config")
async def admin_config_set(request: Request, _auth=Depends(require_auth)):
    """Globale Einstellungen schreiben.
    Body: { token_days?: int, recording_days?: int }
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    cfg = _cfg_load()
    if "token_days" in payload:
        cfg["token_days"]     = max(1, min(365, int(payload["token_days"])))
    if "recording_days" in payload:
        cfg["recording_days"] = max(0, min(3650, int(payload["recording_days"])))
    if "chunk_hours" in payload:
        cfg["chunk_hours"]    = max(1, min(8760, int(payload["chunk_hours"])))
    if "log_days" in payload:
        cfg["log_days"]       = max(0, min(3650, int(payload["log_days"])))
    if "brand_name" in payload:
        cfg["brand_name"]     = str(payload["brand_name"] or "Podcast Studio")[:60]
    if "brand_color" in payload:
        c = str(payload["brand_color"] or "").strip()
        if re.match(r"^#[0-9a-fA-F]{6}$", c):
            cfg["brand_color"] = c
    if "brand_favicon" in payload:
        cfg["brand_favicon"]  = str(payload["brand_favicon"] or "")[:200000]
    _cfg_save(cfg)
    return {"ok": True, "config": cfg}


# ── Admin: Passwort-Reset (Feature 2 + 13) ───────────────────────────────────

@app.post("/admin/password")
async def admin_set_password(request: Request, _role=Depends(require_admin)):
    """Admin kann Admin- UND Host-Passwort zuruecksetzen.
    Body: { role: 'admin'|'host', new_password: str }
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    target = str(payload.get("role", ""))
    new_pw = str(payload.get("new_password", ""))
    _set_password(target, new_pw)
    return {"ok": True, "role": target}


# ── Admin: Einzelne Aufnahme loeschen (Feature 3) ────────────────────────────

@app.delete("/admin/session/{room}/{guest}/{session}")
def admin_delete_session(room, guest, session, _role=Depends(require_admin)):
    """Loescht den kompletten Session-Ordner inkl. Chunks + full.wav."""
    dest_dir = safe(room, guest, session)
    uploads_abs = UPLOADS.resolve()
    try:
        dest_dir.resolve().relative_to(uploads_abs)
    except ValueError:
        raise HTTPException(400, "Ungueltiger Pfad")
    if not dest_dir.exists():
        raise HTTPException(404, "Session nicht gefunden")
    shutil.rmtree(dest_dir)
    # Mixdown nach dem Loeschen einer Gastspur neu aufbauen bzw. entfernen.
    remaining = _session_wavs(room, session)
    mix_path = _mixdown_path(room, session)
    if remaining:
        _ensure_session_mixdown(room, session, force=True)
    else:
        try:
            if mix_path.exists():
                mix_path.unlink()
            if mix_path.parent.exists() and not any(mix_path.parent.iterdir()):
                mix_path.parent.rmdir()
        except OSError:
            pass
    # leere Eltern-Ordner aufraeumen
    for d in (dest_dir.parent, dest_dir.parent.parent):
        try:
            if d.exists() and d != UPLOADS and not any(d.iterdir()):
                d.rmdir()
        except Exception:
            pass
    return {"ok": True, "deleted": f"{room}/{guest}/{session}"}


# ── Rollengetrennte Audio-Ausgabe ───────────────────────────────────────────

@app.get("/host/mixdown/{room}/{session}")
def host_mixdown(room, session, _auth=Depends(require_auth)):
    """Einzige Audio-Ausgabe fuer Hosts: gemeinsamer MP3-Mixdown der Session."""
    path = _ensure_session_mixdown(room, session)
    if path is None or not path.exists():
        raise HTTPException(404, "MP3-Mixdown noch nicht verfügbar")
    return FileResponse(str(path), media_type="audio/mpeg",
                        headers={"Content-Disposition": "inline",
                                 "Cache-Control": "no-store"})


@app.get("/admin/preview/{room}/{guest}/{session}")
def admin_preview_recording(room, guest, session, _role=Depends(require_admin)):
    """Admin-Vorschau einer einzelnen WAV-Spur."""
    dest_dir = safe(room, guest, session)
    wav_path = dest_dir / "full.wav"
    if not wav_path.exists():
        raise HTTPException(404, "full.wav noch nicht vorhanden")
    return FileResponse(str(wav_path), media_type="audio/wav",
                        headers={"Content-Disposition": "inline"})


@app.get("/admin/session-export/{room}/{session}")
def admin_export_session_zip(room, session, _role=Depends(require_admin)):
    """ZIP mit allen Gast-WAVs genau einer Session; nur fuer Admins."""
    wavs = _session_wavs(room, session)
    if not wavs:
        raise HTTPException(404, "Keine fertigen WAVs in dieser Session")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for guest, wav in wavs:
            zf.write(str(wav), f"{room}_{session}/{guest}.wav")
    buf.seek(0)
    fname = f"{room}_{session}_wavs.zip"
    return StreamingResponse(buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ── ZIP-Export aller Spuren eines Raums (Feature 4) ──────────────────────────

@app.get("/export/{room}")
def export_room_zip(room, _role=Depends(require_admin)):
    """Packt alle full.wav eines Raums in ein ZIP und streamt es."""
    check_ident(room)
    room_dir = safe(room)
    if not room_dir.exists() or not room_dir.is_dir():
        raise HTTPException(404, "Raum nicht gefunden")

    wavs = []
    for guest_dir in sorted(room_dir.iterdir()):
        if not guest_dir.is_dir():
            continue
        for sess_dir in sorted(guest_dir.iterdir()):
            if not sess_dir.is_dir():
                continue
            wav = sess_dir / "full.wav"
            if wav.exists():
                arc = f"{room}/{guest_dir.name}_{sess_dir.name}.wav"
                wavs.append((wav, arc))
    if not wavs:
        raise HTTPException(404, "Keine fertigen Aufnahmen (full.wav) in diesem Raum")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for wav, arc in wavs:
            zf.write(str(wav), arc)
    buf.seek(0)
    fname = f"{room}_tracks.zip"
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ── Raum-Management: Liste / Archivieren / Loeschen (Feature 6) ───────────────

@app.get("/admin/rooms")
def admin_rooms(_role=Depends(require_admin)):
    """Alle Raeume mit Statistik + Archiv-Flag."""

    cfg = _cfg_load()
    archived = set(cfg.get("archived_rooms", []))
    rooms = {}
    # Raeume aus uploads
    if UPLOADS.exists():
        for room_dir in UPLOADS.iterdir():
            if not room_dir.is_dir():
                continue
            sess = 0
            size = 0.0
            for gd in room_dir.iterdir():
                if not gd.is_dir():
                    continue
                for sd in gd.iterdir():
                    if not sd.is_dir():
                        continue
                    sess += 1
                    full = sd / "full.wav"
                    if full.exists():
                        size += full.stat().st_size
            rooms[room_dir.name] = {"room": room_dir.name, "sessions": sess,
                                    "size_mb": round(size / 1024 / 1024, 1)}
    # Raeume aus aktiven Tokens (auch ohne Aufnahmen)
    with _DB_LOCK, _db_conn() as conn:
        for r in conn.execute("SELECT DISTINCT room FROM guest_tokens").fetchall():
            rooms.setdefault(r["room"], {"room": r["room"], "sessions": 0, "size_mb": 0.0})
    # Raeume aus der Registry (sofort sichtbar nach dem Erstellen, auch ohne
    # Tokens oder Aufnahmen).
    for name in _room_registry_list():
        rooms.setdefault(name, {"room": name, "sessions": 0, "size_mb": 0.0})
    out = []
    for name, info in rooms.items():
        info["archived"] = name in archived
        # "online" bedeutet: ein Host ist im Raum.
        # Gäste alleine sollen den Raum NICHT als live markieren.
        host_online = bool(ROOMS.get(name, {}).get("host_online"))
        info["online"] = host_online

        # Recording-Status: nur wenn Host online ist.
        # command.action == 'start' + start_at erreicht => recording.
        try:
            r = ROOMS.get(name, {})
            cmd = (r.get("command") or {})
            now_ms = int(time.time() * 1000)
            start_at = cmd.get("start_at")
            action = cmd.get("action")
            info["recording"] = bool(host_online and action == "start" and start_at and now_ms >= int(start_at))
            info["countdown"] = bool(host_online and action == "start" and start_at and now_ms < int(start_at))
        except Exception:
            info["recording"] = False
            info["countdown"] = False
        out.append(info)
    out.sort(key=lambda x: x["room"].lower())
    return {"ok": True, "rooms": out}


@app.post("/admin/room/{room}/archive")
async def admin_room_archive(room, request: Request, _role=Depends(require_admin)):
    """Raum (de)archivieren. Body: { archived: bool }"""
    check_ident(room)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    want = bool(payload.get("archived", True))
    cfg = _cfg_load()
    arch = set(cfg.get("archived_rooms", []))
    if want:
        arch.add(room)
    else:
        arch.discard(room)
    cfg["archived_rooms"] = sorted(arch)
    _cfg_save(cfg)
    return {"ok": True, "room": room, "archived": want}


@app.delete("/admin/room/{room}")
def admin_room_delete(room, _role=Depends(require_admin)):
    """Loescht alle Aufnahmen + Tokens eines Raums komplett."""
    check_ident(room)
    room_dir = safe(room)
    uploads_abs = UPLOADS.resolve()
    if room_dir.exists():
        try:
            room_dir.resolve().relative_to(uploads_abs)
            shutil.rmtree(room_dir)
        except ValueError:
            raise HTTPException(400, "Ungueltiger Pfad")
    # Abgeleitete MP3-Mixdowns des Raums ebenfalls vollstaendig entfernen.
    mix_room = DATA_DIR / "mixdowns" / room
    if mix_room.exists():
        try:
            shutil.rmtree(mix_room)
        except OSError as exc:
            print("[mixdown] Raum-Cleanup fehlgeschlagen:", exc)
    with _DB_LOCK, _db_conn() as conn:
        conn.execute("DELETE FROM guest_tokens WHERE room=?", (room,))
        conn.execute("DELETE FROM markers WHERE room=?", (room,))
        conn.commit()
    # Persistente Gast-Logs des Raums ebenfalls entfernen (sonst verwaiste Zeilen).
    _guest_logs_delete_room(room)
    _room_registry_delete(room)
    cfg = _cfg_load()
    arch = set(cfg.get("archived_rooms", []))
    arch.discard(room)
    cfg["archived_rooms"] = sorted(arch)
    _cfg_save(cfg)
    ROOMS.pop(room, None)
    with _CONSOLE_LOCK:
        GUEST_CONSOLE.pop(room, None)
    return {"ok": True, "deleted": room}


@app.get("/admin/room/{room}/guests")
def admin_room_guests(room, _role=Depends(require_admin)):
    """Live-Gastliste eines Raums (wie host_status, aber Admin-Zugriff)."""
    check_ident(room)
    now_s = time.time()
    with _LOCK:
        r = _room(room)
        _prune(r)
        guests = []
        for info in r["guests"].values():
            age  = now_s - info.get("last_seen", 0)
            conn = ("online" if age <= GUEST_STALE_AFTER
                    else "stale" if age <= GUEST_OFFLINE_AFTER else "offline")
            row = {k: info.get(k) for k in (
                "guest", "display_name", "session", "state",
                "mic_label", "speaker_label", "rms", "queue", "rec_mb", "up_mb")}
            # Sprint 2: Mic-Inventar + aktuelles Geraet + Wechsel-Status.
            row["mic_devices"]          = info.get("mic_devices", [])
            row["current_mic_deviceId"] = info.get("current_mic_deviceId", "")
            row["mic_pending"]          = bool(info.get("mic_cmd"))
            row["mic_last_result"]      = info.get("mic_last_result")
            row["connection"]         = conn
            row["seconds_since_seen"] = round(age, 1)
            guests.append(row)
        guests.sort(key=lambda x: (x.get("display_name") or x.get("guest") or "").lower())
    return {"ok": True, "room": room, "server_time": int(now_s * 1000), "guests": guests,
            "online_count": sum(1 for g in guests if g["connection"] == "online")}


# ── Sessions-API (geschuetzt) ─────────────────────────────────────────────────

@app.get("/admin/room/{room}/logs")
def admin_room_logs(room, since: float = 0.0, _role=Depends(require_admin)):
    """Gast-Console-Logs eines Raums, gruppiert pro Gast (persistente DB-Historie).

    Antwortform:
      {
        ok: true,
        room: "<room>",
        server_time: <epoch_s>,
        guests: [
          {
            guest: "<id>",
            last_ts: <epoch_s>,
            counts: { info, ok, err },
            last: { ts, level, msg, session },   # juengstes Ereignis (Inline-Zeile)
            lines: [ { ts, level, msg, session }, ... ]  # chronologisch
          }, ...
        ]
      }

    `since` erlaubt Delta-Polling: nur Zeilen mit ts > since werden geliefert.
    """
    check_ident(room)
    rows = _guest_logs_query(room, since=since)
    groups: dict[str, dict] = {}
    for r in rows:
        g = str(r.get("guest") or "")
        grp = groups.get(g)
        if grp is None:
            grp = {"guest": g, "last_ts": 0.0,
                   "counts": {"info": 0, "ok": 0, "err": 0},
                   "last": None, "lines": []}
            groups[g] = grp
        line = {"ts": r.get("ts"), "level": r.get("level"),
                "msg": r.get("msg"), "session": r.get("session")}
        grp["lines"].append(line)
        lvl = line["level"] if line["level"] in grp["counts"] else "info"
        grp["counts"][lvl] += 1
        if (line["ts"] or 0) >= grp["last_ts"]:
            grp["last_ts"] = line["ts"] or 0
            grp["last"] = line
    # Gaeste nach juengstem Ereignis sortieren (aktivste oben).
    guests = sorted(groups.values(), key=lambda x: x["last_ts"], reverse=True)
    return {"ok": True, "room": room, "server_time": time.time(), "guests": guests}


@app.get("/sessions")
def sessions(role=Depends(require_auth)):
    """Session-Inventar fuer Host und Admin.

    Die Liste bleibt transportseitig flach (eine Zeile pro Gastspur), enthaelt
    aber gemeinsame Mixdown-Felder. Die UIs gruppieren zuerst nach Raum+Session.
    Hosts erhalten keine WAV-/ZIP-URLs; diese Endpunkte sind zusaetzlich
    serverseitig auf die Admin-Rolle beschraenkt.
    """
    out = []
    archived = set(_cfg_get("archived_rooms") or [])
    session_keys = set()
    for room_dir in (sorted(UPLOADS.iterdir()) if UPLOADS.exists() else []):
        if not room_dir.is_dir():
            continue
        for guest_dir in sorted(room_dir.iterdir()):
            if not guest_dir.is_dir():
                continue
            for sess_dir in sorted(guest_dir.iterdir()):
                if not sess_dir.is_dir():
                    continue
                chunks = list(sess_dir.glob("chunk-*.pcm")) + list(sess_dir.glob("chunk-*.webm"))
                full = sess_dir / "full.wav"
                has_wav = full.exists() and full.stat().st_size > 44
                last_seen = max([sess_dir.stat().st_mtime]
                                + [p.stat().st_mtime for p in chunks]
                                + ([full.stat().st_mtime] if has_wav else []))
                created_at = sess_dir.stat().st_ctime
                state = ("complete" if has_wav and chunks else
                         "wav_only" if has_wav else
                         "chunks_only" if chunks else "prepared")
                out.append({
                    "room": room_dir.name, "guest": guest_dir.name,
                    "session": sess_dir.name, "label": sess_dir.name,
                    "chunks": len(chunks), "chunks_count": len(chunks),
                    "merged": has_wav, "has_wav": has_wav,
                    "size_mb": round((full.stat().st_size if has_wav else
                        sum(c.stat().st_size for c in chunks)) / 1024 / 1024, 2),
                    "created_at": created_at, "last_seen": last_seen,
                    "archived": room_dir.name in archived, "online": False,
                    "deleted": False, "state": state,
                })
                if has_wav:
                    session_keys.add((room_dir.name, sess_dir.name))

    # Alte Sessions bekommen ihren Mixdown beim ersten Listenabruf nachtraeglich.
    mix_state = {}
    for room_name, session_name in session_keys:
        path = _ensure_session_mixdown(room_name, session_name)
        mix_state[(room_name, session_name)] = bool(path and path.exists())
    for row in out:
        row["has_mixdown"] = mix_state.get((row["room"], row["session"]), False)
        row["mixdown_url"] = (f"/host/mixdown/{row['room']}/{row['session']}"
                              if row["has_mixdown"] else None)
        # Explizit nur Admin-Metadaten markieren; keine privilegierten URLs an Hosts.
        row["admin_assets"] = bool(role == "admin")
    out.sort(key=lambda r: (r["last_seen"], r["room"], r["session"], r["guest"]), reverse=True)
    return JSONResponse(out)


# ── WebSocket-Endpunkte (Phase 5: Echtzeit) ──────────────────────────────────
# Zwei Kanaele, beide an den vorhandenen ROOMS-Zustand gekoppelt. Der
# Chunk-Upload bleibt bewusst bei HTTP (PUT /upload ...) -- nur die Steuer- und
# Telemetriedaten laufen hier in Echtzeit.

def _apply_guest_telemetry(room: str, guest: str, payload: dict) -> None:
    """Uebernimmt einen Gast-Heartbeat in ROOMS (gemeinsam von HTTP /poll und WS)."""
    # Konsolen-Logs (optional) mitschreiben.
    lines = payload.get("console") or []
    if lines and isinstance(lines, list):
        now = time.time()
        with _CONSOLE_LOCK:
            buf = GUEST_CONSOLE.setdefault(room, [])
            for ln in lines[-50:]:
                if not isinstance(ln, dict):
                    continue
                buf.append({
                    "ts": float(ln.get("ts") or now),
                    "guest": guest,
                    "session": str(payload.get("session", ""))[:40],
                    "level": str(ln.get("level") or "info")[:10],
                    "msg": str(ln.get("msg") or "")[:400],
                })
            if len(buf) > 3000:
                del buf[:-2000]
        # Persistente Ablage (Historie + spaetere Analyse).
        _guest_logs_store(room, guest, str(payload.get("session", "")), lines)
    now_s = time.time()
    with _LOCK:
        r = _room(room)
        g = r["guests"].get(guest, {})
        g.update({
            "guest":         guest,
            "display_name":  str(payload.get("display_name", guest))[:80],
            "session":       str(payload.get("session", ""))[:40],
            "state":         str(payload.get("state", "idle"))[:20],
            "mic_label":     str(payload.get("mic_label", "unbekannt"))[:120],
            "speaker_label": str(payload.get("speaker_label", "Standard"))[:120],
            "rms":           float(payload.get("rms", 0.0) or 0.0),
            "queue":         int(payload.get("queue", 0) or 0),
            "rec_mb":        float(payload.get("rec_mb", 0.0) or 0.0),
            "up_mb":         float(payload.get("up_mb", 0.0) or 0.0),
            "last_seen":     now_s,
        })
        # Sprint 2: Mikrofon-Inventar + aktuelles Geraet uebernehmen.
        devs_in = payload.get("mic_devices")
        if isinstance(devs_in, list):
            clean = []
            for d in devs_in:
                if isinstance(d, dict) and str(d.get("deviceId", "")):
                    clean.append({
                        "deviceId": str(d.get("deviceId", ""))[:200],
                        "label":    str(d.get("label", "") or "Mikrofon")[:120],
                    })
            g["mic_devices"] = clean[:20]
        if "current_mic_deviceId" in payload:
            g["current_mic_deviceId"] = str(payload.get("current_mic_deviceId", ""))[:200]
        # Tatsaechlich gebundenes Geraet + Warnhinweise. Auswahl und aktives
        # Geraet koennen nach einem Hotplug auseinanderlaufen -- der Host soll
        # sehen, was wirklich aufgenommen wird.
        if "active_mic_deviceId" in payload:
            g["active_mic_deviceId"] = str(payload.get("active_mic_deviceId", ""))[:200]
        if "mic_active" in payload:
            g["mic_active"] = bool(payload.get("mic_active"))
        alert = payload.get("mic_alert")
        if isinstance(alert, dict) and alert.get("text"):
            g["mic_alert"] = {
                "kind": str(alert.get("kind", "warn"))[:10],
                "text": str(alert.get("text", ""))[:200],
            }
        elif "mic_alert" in payload:
            g.pop("mic_alert", None)
        if "mic_lost_during_recording" in payload:
            g["mic_lost_during_recording"] = bool(payload.get("mic_lost_during_recording"))
        # Ergebnis nur fuer den EXAKT passenden Mic-Befehl akzeptieren.
        # Der Recorder sendet Telemetrie wiederholt; eine alte Bestaetigung darf
        # daher keinen spaeteren Befehl loeschen oder dessen Status ueberschreiben.
        res = payload.get("mic_last_result")
        if isinstance(res, dict):
            current_cmd = g.get("mic_cmd")
            result_cmd_id = str(res.get("command_id", ""))[:64]
            expected_cmd_id = (
                str(current_cmd.get("command_id", ""))[:64]
                if isinstance(current_cmd, dict) else ""
            )
            if current_cmd and result_cmd_id and result_cmd_id == expected_cmd_id:
                g["mic_last_result"] = {
                    "command_id": result_cmd_id,
                    "ok":     bool(res.get("ok")),
                    "label":  str(res.get("label", "") or "")[:120],
                    "error":  str(res.get("error", "") or "")[:160],
                }
                g.pop("mic_cmd", None)
        r["guests"][guest] = g


@app.websocket("/ws/guest/{room}/{guest}")
async def ws_guest(websocket: WebSocket, room: str, guest: str, token: str | None = None):
    # Zugang wie beim Recorder ueber den Gast-Token absichern.
    if not SAFE.match(room) or not SAFE.match(guest):
        await websocket.close(code=4000)
        return
    info = _token_resolve(token) if token else None
    if info is None or info.get("room") != room:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    _ws_add(_WS_GUESTS, room, websocket)
    try:
        # Initialer State-Push (command + settings), damit der Gast sofort
        # weiss, ob z.B. gerade eine Aufnahme laeuft.
        await _broadcast_guests(room)
        while True:
            data = await websocket.receive_json()
            mtype = data.get("type")
            if mtype == "heartbeat":
                _apply_guest_telemetry(room, guest, data)
                # Host-Panels live aktualisieren.
                await _broadcast_host_status(room)
                # Antwort mit aktueller server_time/command/settings.
                with _LOCK:
                    r = _room(room)
                    gi = r["guests"].get(guest, {})
                    # Sprint 2: gast-gezieltes Mic-Kommando mitliefern (falls gesetzt).
                    mic_cmd = gi.get("mic_cmd")
                    await _ws_send(websocket, {
                        "type": "command",
                        "command": dict(r["command"]),
                        "settings": dict(r["settings"]),
                        "mic_cmd": dict(mic_cmd) if mic_cmd else None,
                        "server_time": int(time.time() * 1000),
                    })
            elif mtype == "level":
                # Nur Pegel aktualisieren -- kein voller Status-Rebuild.
                try:
                    rms = float(data.get("rms", 0.0) or 0.0)
                except (TypeError, ValueError):
                    rms = 0.0
                try:
                    peak = float(data.get("peak", 0.0) or 0.0)
                except (TypeError, ValueError):
                    peak = 0.0
                with _LOCK:
                    gi = _room(room)["guests"].get(guest)
                    if gi is not None:
                        gi["rms"] = rms
                        gi["peak"] = peak
                        gi["last_seen"] = time.time()
                await _broadcast_host_levels(room)
            elif mtype == "ping":
                await _ws_send(websocket, {"type": "pong",
                                          "server_time": int(time.time() * 1000)})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print("[ws-guest] Fehler:", e)
    finally:
        _ws_remove(_WS_GUESTS, room, websocket)


@app.websocket("/ws/host/{room}")
async def ws_host(websocket: WebSocket, room: str,
                  ps_session: str | None = Cookie(default=None)):
    # Host-/Admin-Auth ueber das Session-Cookie.
    if not SAFE.match(room) or _session_role(ps_session or "") is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    _room_register(room)
    # Mark host presence for room overview (index + admin rooms list)
    with _LOCK:
        r = _room(room)
        r["host_online"] = True
        r["host_last_seen"] = time.time()
    _ws_add(_WS_HOSTS, room, websocket)
    try:
        # Initialer Status-Push.
        await _ws_send(websocket, {**_build_status(room), "type": "status"})
        while True:
            data = await websocket.receive_json()
            mtype = data.get("type")
            now_ms = int(time.time() * 1000)
            if mtype == "trigger":
                action = data.get("action")
                with _LOCK:
                    r = _room(room)
                    if action == "start":
                        sid = _new_session_id()
                        r["command"] = {"action": "start",
                                        "start_at": now_ms + int(START_LEAD_SECONDS * 1000),
                                        "session": sid, "issued_at": now_ms}
                        r["rec_started_at"] = now_ms + int(START_LEAD_SECONDS * 1000)
                        r["rec_session"]    = sid
                    elif action == "stop":
                        r["command"] = {"action": "stop", "start_at": None,
                                        "session": r.get("rec_session", ""), "issued_at": now_ms}
                    elif action == "clear":
                        r["command"] = {"action": None, "start_at": None,
                                        "session": None, "issued_at": now_ms}
                await _broadcast_guests(room)
                await _broadcast_host_status(room)
            elif mtype == "settings":
                with _LOCK:
                    r = _room(room)
                    s = r["settings"]
                    if "audio_only" in data:
                        s["audio_only"] = bool(data.get("audio_only"))
                    if "debug_level" in data:
                        try:
                            lvl = int(data.get("debug_level") or 0)
                        except (TypeError, ValueError):
                            lvl = 0
                        s["debug_level"] = max(0, min(2, lvl))
                await _broadcast_guests(room)
                await _broadcast_host_status(room)
            elif mtype == "marker":
                kind = str(data.get("kind", ""))
                if kind in MARKER_KINDS:
                    note = str(data.get("note", ""))
                    with _LOCK:
                        r = _room(room)
                        started = r.get("rec_started_at") or now_ms
                        session = r.get("rec_session", "")
                    offset_ms = max(0, now_ms - int(started))
                    _marker_create(room, session, kind, offset_ms, note)
                    await _broadcast_host_status(room)
            elif mtype == "marker_delete":
                mid = str(data.get("id", ""))
                if re.match(r"^[0-9a-f]{8}$", mid):
                    _marker_delete(mid)
                    await _broadcast_host_status(room)
            elif mtype == "set_mic":
                # Sprint 2: Host waehlt Gast-Mikrofon. Wir legen ein GAST-GEZIELTES
                # Kommando am Gast ab; ausgeliefert wird es im Heartbeat-Reply an
                # genau diesen Gast (siehe ws_guest). Ausfuehrung + Fallback beim Gast.
                tgt = str(data.get("guest", ""))
                did = str(data.get("deviceId", ""))[:200]
                lbl = str(data.get("label", ""))[:120]
                if SAFE.match(tgt) and did:
                    with _LOCK:
                        r = _room(room)
                        gi = r["guests"].get(tgt)
                        if gi is not None:
                            # Eindeutige ID statt nur Millisekunden-Zeitstempel:
                            # Sie koppelt Recorder-Antwort und Pending-Befehl 1:1.
                            command_id = secrets.token_hex(12)
                            gi["mic_cmd"] = {
                                "deviceId": did, "label": lbl,
                                "issued_at": now_ms,
                                "command_id": command_id,
                            }
                            gi.pop("mic_last_result", None)
                    await _broadcast_host_status(room)
            elif mtype == "ping":
                await _ws_send(websocket, {"type": "pong", "server_time": now_ms})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print("[ws-host] Fehler:", e)
    finally:
        _ws_remove(_WS_HOSTS, room, websocket)
        # Unmark host presence when the host socket disconnects
        with _LOCK:
            r = _room(room)
            r["host_online"] = False
            r["host_last_seen"] = time.time()


# ── Static Uploads ─────────────────────────────────────────────────────────
app.mount("/uploads", StaticFiles(directory=str(UPLOADS)), name="uploads")


# ── Auto-Lösch-Task ───────────────────────────────────────────────────────────

def _cleanup_old_recordings():
    """
    Loescht Aufnahme-Ordner (uploads/<room>/<guest>/<session>/) die aelter
    als recording_days Tage sind. Laeuft als Hintergrund-Thread alle 6 Stunden.

    Sicherheit:
    - Pfade werden mit UPLOADS.resolve() abgeglichen (kein Path-Traversal).
    - Leere Gast- und Raum-Ordner werden ebenfalls aufgeraeumt.
    - Bei recording_days=0 wird NICHT geloescht.
    """
    while True:
        try:
            days = int(_cfg_get("recording_days"))
            if days > 0 and UPLOADS.exists():
                cutoff = time.time() - days * 86400
                uploads_abs = UPLOADS.resolve()
                deleted = 0
                for room_dir in list(UPLOADS.iterdir()):
                    if not room_dir.is_dir():
                        continue
                    for guest_dir in list(room_dir.iterdir()):
                        if not guest_dir.is_dir():
                            continue
                        for sess_dir in list(guest_dir.iterdir()):
                            if not sess_dir.is_dir():
                                continue
                            # Sicherheitscheck: Pfad muss unter UPLOADS liegen
                            try:
                                sess_dir.resolve().relative_to(uploads_abs)
                            except ValueError:
                                continue
                            # Zeitstempel: meta.json created_at oder Ordner-mtime
                            ref_time = sess_dir.stat().st_mtime
                            meta_f = sess_dir / "meta.json"
                            if meta_f.exists():
                                ref_time = min(ref_time, meta_f.stat().st_mtime)
                            if ref_time < cutoff:
                                try:
                                    shutil.rmtree(sess_dir)
                                    deleted += 1
                                except Exception as e:
                                    print(f"[cleanup] Fehler beim Loeschen von {sess_dir}: {e}")
                        # Leere Gast-Ordner entfernen
                        try:
                            if guest_dir.exists() and not any(guest_dir.iterdir()):
                                guest_dir.rmdir()
                        except Exception:
                            pass
                    # Leere Raum-Ordner entfernen
                    try:
                        if room_dir.exists() and not any(room_dir.iterdir()):
                            room_dir.rmdir()
                    except Exception:
                        pass
                if deleted:
                    print(f"[cleanup] {deleted} alte Aufnahme(n) geloescht (>{days} Tage).")
        except Exception as e:
            print(f"[cleanup] Unerwarteter Fehler: {e}")
        # Alle 6 Stunden pruefen
        time.sleep(6 * 3600)


def _cleanup_old_chunks():
    """Feature 10: Loescht rohe Chunk-Dateien (chunk-*.pcm / chunk-*.webm) die
    aelter als chunk_hours Stunden sind. full.wav bleibt unberuehrt. So werden
    abgebrochene/halbe Sessions, deren Chunks nie zu WAV zusammengefuegt wurden,
    nach der eingestellten Frist (Standard 72h) aufgeraeumt.
    Im Admin-Panel einstellbar. Laeuft alle Stunde.
    """
    while True:
        try:
            hours = int(_cfg_get("chunk_hours") or 72)
            if hours > 0 and UPLOADS.exists():
                cutoff = time.time() - hours * 3600
                deleted = 0
                for chunk in UPLOADS.rglob("chunk-*"):
                    if not chunk.is_file():
                        continue
                    if chunk.suffix not in (".pcm", ".webm"):
                        continue
                    try:
                        if chunk.stat().st_mtime < cutoff:
                            chunk.unlink()
                            deleted += 1
                    except Exception:
                        pass
                if deleted:
                    print(f"[chunk-cleanup] {deleted} alte Chunk-Datei(en) geloescht (>{hours}h).")
        except Exception as e:
            print(f"[chunk-cleanup] Fehler: {e}")
        time.sleep(3600)


def _cleanup_old_logs():
    """Loescht persistente Gast-Console-Logs (Tabelle guest_logs) die aelter
    als log_days Tage sind. Im Admin-Panel einstellbar. Bei log_days=0 wird
    NICHT geloescht. Laeuft alle 6 Stunden.
    """
    while True:
        try:
            days = int(_cfg_get("log_days") or 0)
            if days > 0:
                cutoff = time.time() - days * 86400
                with _DB_LOCK, _db_conn() as conn:
                    cur = conn.execute(
                        "DELETE FROM guest_logs WHERE ts < ?", (cutoff,))
                    conn.commit()
                    if cur.rowcount:
                        print(f"[log-cleanup] {cur.rowcount} alte Log-Zeile(n) geloescht (>{days} Tage).")
        except Exception as e:
            print(f"[log-cleanup] Fehler: {e}")
        time.sleep(6 * 3600)


def _start_cleanup_thread():
    threading.Thread(target=_cleanup_old_recordings, daemon=True, name="cleanup").start()
    threading.Thread(target=_cleanup_old_chunks, daemon=True, name="chunk-cleanup").start()
    threading.Thread(target=_cleanup_old_logs, daemon=True, name="log-cleanup").start()


_start_cleanup_thread()


# ── Presence tick (host status push even without guest heartbeats) ───────────
# Fixes: host UI not updating "stale/offline" unless a guest sends data.
# We broadcast the computed status periodically for rooms that currently have
# at least one host websocket connected.
try:
    import asyncio

    async def _presence_loop():
        while True:
            try:
                # Snapshot rooms that have hosts connected
                with _WS_LOCK:
                    rooms = list(_WS_HOSTS.keys())
                for room in rooms:
                    try:
                        await _broadcast_host_status(room)
                    except Exception:
                        pass
                # Pegel-Drossel-Map periodisch aufraeumen (kein Wachstum ueber Zeit).
                _prune_level_throttle()
            except Exception:
                pass
            await asyncio.sleep(float(PRESENCE_TICK or 2.0))

    @app.on_event("startup")
    async def _startup_presence_task():
        asyncio.create_task(_presence_loop())
except Exception:
    pass


if __name__ == "__main__":
    # ------------------------------------------------------------------
    # WebSocket-Backend pruefen.
    # Ein "bare" uvicorn ohne WS-Treiber (websockets / wsproto) nimmt zwar
    # HTTP-Requests an, lehnt aber den WebSocket-Upgrade ab -> der Client
    # bleibt ewig auf "verbinden". Genau dieses Symptom sehen wir hier.
    # ------------------------------------------------------------------
    _ws_impl = None
    try:
        import websockets  # noqa: F401
        _ws_impl = "websockets"
    except ImportError:
        try:
            import wsproto  # noqa: F401
            _ws_impl = "wsproto"
        except ImportError:
            _ws_impl = None

    if _ws_impl is None:
        print("=" * 68)
        print("FEHLER: Kein WebSocket-Backend fuer uvicorn installiert.")
        print("Ohne dieses Paket funktioniert die Echtzeit-Verbindung NICHT")
        print("(Gast bleibt auf 'verbinden', Host sieht keine Gaeste).")
        print("Bitte installieren und Server neu starten:")
        print("    pip install 'uvicorn[standard]'")
        print("  oder minimal:")
        print("    pip install websockets")
        print("=" * 68)
        raise SystemExit(1)

    print(f"[start] WebSocket-Backend: {_ws_impl}")
    print("[start] Server laeuft auf http://0.0.0.0:8000  (LAN: http://<deine-IP>:8000)")
    # Backend EXPLIZIT waehlen. "auto" bevorzugt 'websockets' und faellt nicht
    # in jeder uvicorn-Version sauber auf 'wsproto' zurueck -> daher fest setzen.
    uvicorn.run(app, host="0.0.0.0", port=8000, ws=_ws_impl)

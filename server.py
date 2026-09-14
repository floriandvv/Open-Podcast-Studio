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
LOCALE_DIR = BASE / "locale"
DEFAULT_LOCALE = "de"   # Quellsprache der Oberflaeche

# Keep mutable runtime data separate from the application code. This allows
# Docker deployments to mount one persistent volume at DATA_DIR.
DATA_DIR   = Path(os.environ.get("DATA_DIR", str(BASE))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS    = DATA_DIR / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
# Verwaltete Branding-Dateien (Logo/Favicon) statt Data-URLs in config.json.
BRANDING_DIR = DATA_DIR / "branding"
BRANDING_DIR.mkdir(parents=True, exist_ok=True)
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
    "brand_color":      "",       # Leer = Akzentfarbe des aktiven Presets
    "brand_favicon":    "",     # Legacy Data-URL (nur noch Fallback beim Lesen)
    # Erweitertes Theming: Hintergrund, Buttontext und allgemeine Textfarbe
    # sind jetzt eigene Tokens. Leer = Wert aus dem gewaehlten Preset.
    "brand_preset":     "default",  # default | dark | contrast
    "brand_bg":         "",         # Seitenhintergrund
    "brand_text":       "",         # Allgemeine UI-Textfarbe
    "brand_on_brand":   "",         # Textfarbe auf Brand-Flaechen (Buttons)
    # Verwaltete Branding-Dateien unter DATA_DIR/branding/ (Punkt 7).
    # Gespeichert wird nur Metadata; die Bytes liegen als Datei auf der Platte.
    "brand_logo_asset":        None,   # {"file","name","size","mime","updated_at"}
    "brand_favicon_asset":     None,
    # Logo/Favicon gehoeren zum jeweils gewaehlten globalen Theme-Preset.
    "global_preset_assets":    {},
    # Eigene globale Preset-Definitionen. Die Keys der eingebauten Presets
    # sind reserviert; Varianten davon werden als neue Presets angelegt.
    "global_presets":     [],
    # Wiederverwendbare Branding-Presets fuer Gaeste. Der Admin pflegt die
    # Bibliothek; Hosts weisen beim Erstellen eines Raums genau ein Preset zu.
    "branding_presets":  [],
    "room_preset_assignments": {},
    # Legacy-Mapping wird nur noch fuer bestehende Installationen gelesen.
    "room_branding":    {},
    "locale":            "de",   # Sprache der Oberflaeche (de = Quelltext)
    "room_locales":      {},      # Optional recorder locale per room
    # Archived rooms -- list of room names
    "archived_rooms":   [],
    # Aufnahme-Schutzmechanismen (Recording guardrails)
    "require_guest_online": True,   # Start nur, wenn mindestens ein Gast online ist
    "require_guest_ready":  False,  # Gaeste muessen sich aktiv bereit melden
    "clip_threshold_dbfs":  -1.0,   # ab diesem Spitzenpegel gilt ein Sample als Clipping
    "clip_min_samples":     3,      # so viele aufeinanderfolgende Samples = echtes Clipping
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


def _normalise_locale(value: str) -> str:
    value = str(value or DEFAULT_LOCALE).lower().replace('_', '-')
    code = value.split('-', 1)[0]
    if not re.fullmatch(r"[a-z]{2,3}", code):
        return DEFAULT_LOCALE
    candidate = LOCALE_DIR / f"{code}.json"
    return code if candidate.is_file() else DEFAULT_LOCALE


def _available_locales() -> list[dict]:
    result = []
    for path in sorted(LOCALE_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            meta = data.get("meta", {})
            result.append({"code": path.stem, "name": meta.get("name", path.stem),
                           "native_name": meta.get("native_name", path.stem)})
        except Exception:
            continue
    return result


def _load_locale(locale: str) -> dict:
    code = _normalise_locale(locale)
    try:
        return json.loads((LOCALE_DIR / f"{code}.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _global_locale() -> str:
    return _normalise_locale(_cfg_get("locale"))


def _room_locale(room: str) -> str:
    overrides = _cfg_get("room_locales") or {}
    return _normalise_locale(overrides.get(room) or _global_locale())

SAFE = re.compile(r"^[a-zA-Z0-9_-]+$")
# Dateinamen verwalteter Branding-Assets (kein Pfad, keine Traversal-Zeichen).
SAFE_FILE = re.compile(r"^[a-zA-Z0-9._-]+$")

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
        # Clipping-Ereignisse: uebersteuerte Passagen eines Gastes. Immer an eine
        # session_id gebunden, damit sie einer konkreten Aufnahme zuordenbar sind.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS clip_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                room        TEXT NOT NULL,
                guest       TEXT NOT NULL DEFAULT '',
                session     TEXT NOT NULL DEFAULT '',
                ts          REAL NOT NULL,
                offset_ms   INTEGER NOT NULL DEFAULT 0,
                peak_dbfs   REAL NOT NULL DEFAULT 0,
                samples     INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_clip_events_room_ts ON clip_events (room, ts)")
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


def _clip_event_store(room: str, guest: str, session: str, ts: float,
                      offset_ms: int, peak_dbfs: float, samples: int,
                      duration_ms: int) -> None:
    """Persistiert ein Clipping-Ereignis (Admin-Logs + spaetere Analyse)."""
    try:
        with _DB_LOCK, _db_conn() as conn:
            conn.execute(
                "INSERT INTO clip_events (room, guest, session, ts, offset_ms, "
                "peak_dbfs, samples, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (room, str(guest or "")[:80], str(session or "")[:40], float(ts),
                 int(offset_ms), float(peak_dbfs), int(samples), int(duration_ms)))
            conn.commit()
    except Exception as e:
        print("[clip_events] store failed:", e)


def _clip_events_query(room: str, session: str | None = None,
                       since: float = 0.0, limit: int = 2000) -> list[dict]:
    try:
        with _DB_LOCK, _db_conn() as conn:
            if session:
                rows = conn.execute(
                    "SELECT * FROM clip_events WHERE room=? AND session=? AND ts>? "
                    "ORDER BY ts ASC LIMIT ?",
                    (room, session, float(since), int(limit))).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM clip_events WHERE room=? AND ts>? "
                    "ORDER BY ts ASC LIMIT ?",
                    (room, float(since), int(limit))).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print("[clip_events] query failed:", e)
        return []


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
        # Roadmap 6: abgelehnte Zugriffe zaehlen, damit die Diagnose-Ansicht
        # zwischen "leer" und "nicht angemeldet" unterscheiden kann.
        _diag_bump("auth_failures")
        _diag_bump("room_list_unauthenticated")
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
        _diag_bump("auth_failures")
        _diag_bump("room_list_unauthenticated")
        raise HTTPException(status_code=303, headers={"Location": "/login"},
                            detail="Nicht authentifiziert")
    if role != "admin":
        _diag_bump("room_list_denied")
        _error_record("auth", "Admin-Route ohne Admin-Rolle aufgerufen",
                      detail=f"Rolle: {role}")
        raise HTTPException(status_code=403, detail="Nur fuer Admins")
    return role


# ---------------------------------------------------------------------------
# Roadmap 6: Betriebs-Telemetrie fuer Health & Diagnostics
# ---------------------------------------------------------------------------
# Der Server sammelt einige leichte Kennzahlen im Speicher, damit das
# Admin-Panel einen echten Zustandsbericht zeigen kann statt nur "laeuft".
# Alles ist bewusst fluechtig: nach einem Neustart beginnt die Messung neu.
SERVER_START_TS = time.time()

# Ringpuffer fuer die letzten Serverfehler. Kein Log-Ersatz, sondern das, was
# ein Admin im Panel sofort sehen muss.
ERROR_LOG_MAX = 100
_ERROR_LOG: list[dict] = []
_ERROR_LOG_LOCK = threading.Lock()

# Zaehler fuer Zugriffe auf die Raumliste (Roadmap 6: room-list request status
# und Berechtigungsfehler sichtbar machen).
_DIAG_COUNTERS = {
    "room_list_ok": 0,
    "room_list_denied": 0,
    "room_list_unauthenticated": 0,
    "room_list_error": 0,
    "auth_failures": 0,
    "upload_chunks": 0,
    "upload_bytes": 0,
    "upload_errors": 0,
    "finish_ok": 0,
    "finish_errors": 0,
    "wav_rebuilds": 0,
}
_DIAG_LOCK = threading.Lock()

# Letzter Raumlisten-Zugriff, damit im Panel sichtbar ist, wann und mit welchem
# Ergebnis zuletzt gelesen wurde.
_ROOM_LIST_LAST: dict = {}


def _diag_bump(key: str, amount: int = 1) -> None:
    with _DIAG_LOCK:
        if key in _DIAG_COUNTERS:
            _DIAG_COUNTERS[key] += amount


def _diag_snapshot() -> dict:
    with _DIAG_LOCK:
        return dict(_DIAG_COUNTERS)


def _error_record(source: str, message: str, room: str = "", detail: str = "") -> None:
    """Legt einen Fehler in den Ringpuffer und schreibt ihn auf stdout.

    `source` ist die Herkunft (z. B. "ws-host", "upload", "merge"), damit ein
    Admin die Meldung ohne Code-Kenntnis zuordnen kann.
    """
    entry = {
        "ts": time.time(),
        "source": str(source)[:40],
        "room": str(room)[:80],
        "message": str(message)[:500],
        "detail": str(detail)[:1000],
    }
    with _ERROR_LOG_LOCK:
        _ERROR_LOG.append(entry)
        if len(_ERROR_LOG) > ERROR_LOG_MAX:
            del _ERROR_LOG[:len(_ERROR_LOG) - ERROR_LOG_MAX]
    print(f"[{entry['source']}] {entry['message']}" + (f" :: {entry['detail']}" if detail else ""))


def _errors_recent(limit: int = 50, since: float = 0.0) -> list[dict]:
    with _ERROR_LOG_LOCK:
        rows = [e for e in _ERROR_LOG if e["ts"] > since]
    return rows[-limit:][::-1]


def _fmt_uptime(seconds: float) -> str:
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


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
             "guests":   {},
             # Idempotenz-Buch: verarbeitete Trigger (action|session|issued_at).
             "cmd_seen": {},
             "rec_state": "idle"}
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
# Roadmap 5: Single-Host-Instance Lock (Session Locking)
# ---------------------------------------------------------------------------
# Pro Raum darf genau EINE Host-Instanz steuern (Start/Stopp/Settings/Marker/
# Mikrofonwahl). Weitere Host-Clients erhalten einen Read-only-Zustand.
#
# Modell:
#   _HOST_LOCKS[room] = {
#       "room": str, "client_id": str, "role": "host"|"admin",
#       "label": str,               # Anzeigename fuer die UI
#       "acquired_at": float,       # Unix-Sekunden
#       "last_renew": float,
#       "expires_at": float,        # Ablauf; wird bei jedem Renew verlaengert
#       "connected": bool,          # WebSocket der steuernden Instanz offen?
#   }
#
# Lebenszyklus:
#   acquire  -> erster Client bekommt den Lock (mode="control")
#   renew    -> Heartbeat (WS-Ping oder HTTP) verlaengert expires_at
#   release  -> expliziter Verzicht (Tab schliessen, "Steuerung abgeben")
#   recovery -> nach Disconnect laeuft eine kurze Kulanzzeit (GRACE); danach
#               ist der Lock "stale" und wird beim naechsten Zugriff automatisch
#               freigegeben. Ein Serverneustart leert die Locks vollstaendig,
#               weil der Zustand absichtlich nur In-Memory gehalten wird.
HOST_LOCK_TTL = 30.0      # Gueltigkeit ab dem letzten Renew (verbundener Host)
HOST_LOCK_GRACE = 20.0    # Kulanzzeit nach einem Disconnect (Reload/Netzwerk)

_HOST_LOCKS: dict[str, dict] = {}
_HOST_LOCK_MUTEX = threading.Lock()

# Steuerbefehle, die den Lock zwingend brauchen.
LOCK_GUARDED_ACTIONS = ("trigger", "settings", "marker", "marker_delete", "set_mic")


def _lock_public(lock: dict | None, now: float | None = None) -> dict | None:
    """Serialisierbare Sicht auf einen Lock (ohne interne Felder)."""
    if not lock:
        return None
    now = now if now is not None else time.time()
    return {
        "room":        lock["room"],
        "client_id":   lock["client_id"],
        "role":        lock["role"],
        "label":       lock.get("label", ""),
        "acquired_at": round(lock["acquired_at"], 3),
        "last_renew":  round(lock["last_renew"], 3),
        "expires_at":  round(lock["expires_at"], 3),
        "expires_in":  round(max(0.0, lock["expires_at"] - now), 1),
        "connected":   bool(lock.get("connected")),
        "ttl":         HOST_LOCK_TTL,
        "grace":       HOST_LOCK_GRACE,
    }


def _lock_is_stale(lock: dict, now: float) -> bool:
    return now >= lock["expires_at"]


def _lock_prune_locked(room: str, now: float) -> dict | None:
    """Entfernt einen abgelaufenen Lock. Erwartet _HOST_LOCK_MUTEX."""
    lock = _HOST_LOCKS.get(room)
    if lock is None:
        return None
    if _lock_is_stale(lock, now):
        _HOST_LOCKS.pop(room, None)
        return None
    return lock


def _lock_get(room: str) -> dict | None:
    now = time.time()
    with _HOST_LOCK_MUTEX:
        lock = _lock_prune_locked(room, now)
        return dict(lock) if lock else None


def _lock_acquire(room: str, client_id: str, role: str = "host",
                  label: str = "", force: bool = False) -> dict:
    """Versucht den Lock zu bekommen.

    Rueckgabe: {"mode": "control"|"readonly", "lock": <public>,
                "acquired": bool, "takeover": bool, "reason": str}
    """
    now = time.time()
    with _HOST_LOCK_MUTEX:
        cur = _lock_prune_locked(room, now)
        takeover = False
        reason = ""
        if cur and cur["client_id"] != client_id and not force:
            # Fremder, gueltiger Lock -> Read-only.
            return {"mode": "readonly", "acquired": False, "takeover": False,
                    "reason": "locked_by_other", "lock": _lock_public(cur, now)}
        if cur and cur["client_id"] != client_id and force:
            takeover = True
            reason = "takeover"
        elif cur and cur["client_id"] == client_id:
            reason = "renewed"
        else:
            reason = "acquired"
        lock = {
            "room": room,
            "client_id": client_id,
            "role": role,
            "label": label or (cur or {}).get("label", ""),
            "acquired_at": (cur or {}).get("acquired_at", now) if reason == "renewed" else now,
            "last_renew": now,
            "expires_at": now + HOST_LOCK_TTL,
            "connected": True,
        }
        _HOST_LOCKS[room] = lock
        return {"mode": "control", "acquired": True, "takeover": takeover,
                "reason": reason, "lock": _lock_public(lock, now)}


def _lock_renew(room: str, client_id: str) -> dict:
    now = time.time()
    with _HOST_LOCK_MUTEX:
        cur = _lock_prune_locked(room, now)
        if cur is None:
            return {"mode": "none", "renewed": False, "reason": "no_lock", "lock": None}
        if cur["client_id"] != client_id:
            return {"mode": "readonly", "renewed": False, "reason": "locked_by_other",
                    "lock": _lock_public(cur, now)}
        cur["last_renew"] = now
        cur["expires_at"] = now + HOST_LOCK_TTL
        cur["connected"] = True
        return {"mode": "control", "renewed": True, "reason": "renewed",
                "lock": _lock_public(cur, now)}


def _lock_release(room: str, client_id: str) -> dict:
    now = time.time()
    with _HOST_LOCK_MUTEX:
        cur = _lock_prune_locked(room, now)
        if cur is None:
            return {"released": False, "reason": "no_lock", "lock": None}
        if cur["client_id"] != client_id:
            return {"released": False, "reason": "not_owner",
                    "lock": _lock_public(cur, now)}
        _HOST_LOCKS.pop(room, None)
        return {"released": True, "reason": "released", "lock": None}


def _lock_mark_disconnected(room: str, client_id: str) -> dict | None:
    """Disconnect der steuernden Instanz: Lock bleibt fuer HOST_LOCK_GRACE
    reserviert, damit ein Reload oder kurzer Netzausfall die Steuerung
    zurueckbekommt. Danach ist er stale und faellt an den naechsten Host."""
    now = time.time()
    with _HOST_LOCK_MUTEX:
        cur = _lock_prune_locked(room, now)
        if cur is None or cur["client_id"] != client_id:
            return None
        cur["connected"] = False
        cur["expires_at"] = min(cur["expires_at"], now + HOST_LOCK_GRACE)
        return _lock_public(cur, now)


def _lock_holds(room: str, client_id: str) -> bool:
    """True, wenn client_id aktuell steuern darf.

    Ist kein Lock vorhanden (z. B. direkt nach einem Serverneustart), darf ein
    identifizierter Client steuern und bekommt den Lock implizit.
    """
    if not client_id:
        # Ohne Client-Kennung nur erlauben, wenn niemand den Raum haelt.
        return _lock_get(room) is None
    res = _lock_acquire(room, client_id)
    return res["mode"] == "control"


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
            # Guardrails muessen auch beim Gast bekannt sein: nur so kann der
            # Recorder die Bereit-Meldung anfordern und Clipping korrekt melden.
            "guardrails":  {
                "require_guest_ready": bool(_cfg_get("require_guest_ready")),
                "clip_threshold_dbfs": float(_cfg_get("clip_threshold_dbfs")),
                "clip_min_samples":    int(_cfg_get("clip_min_samples")),
            },
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


def _guest_ready_state(info: dict, conn_state: str) -> dict:
    """Fasst zusammen, ob ein Gast technisch und explizit aufnahmebereit ist."""
    perms = info.get("permissions") or {}
    mic_ok = bool(info.get("mic_active", False)) and perms.get("microphone") != "denied"
    cam_needed = not bool(info.get("audio_only", True))
    cam_ok = (not cam_needed) or (perms.get("camera") != "denied"
                                  and bool(info.get("cam_active", False)))
    device_ok = not bool(info.get("mic_mismatch_flag"))
    blockers = []
    if conn_state != "online":
        blockers.append("offline")
    if perms.get("microphone") == "denied":
        blockers.append("mic_permission")
    elif not mic_ok:
        blockers.append("mic_signal")
    if cam_needed and not cam_ok:
        blockers.append("camera")
    if not device_ok:
        blockers.append("device_mismatch")
    return {
        "tech_ready": conn_state == "online" and mic_ok and cam_ok and device_ok,
        "declared_ready": bool(info.get("declared_ready")),
        "blockers": blockers,
    }


def _start_gate(room: str) -> dict:
    """Prueft, ob eine Aufnahme gestartet werden darf.

    Regeln (Admin-konfigurierbar):
      - require_guest_online : mindestens ein Gast ist online und technisch bereit
      - require_guest_ready  : jeder online-Gast hat sich aktiv bereit gemeldet
    """
    status = _build_status(room)
    guests = status.get("guests", [])
    online = [g for g in guests if g.get("connection") == "online"]
    require_online = bool(_cfg_get("require_guest_online"))
    require_ready = bool(_cfg_get("require_guest_ready"))
    tech_ready = [g for g in online if (g.get("ready") or {}).get("tech_ready")]

    if require_online and not online:
        return {"ok": False, "reason": "no_guest_online",
                "detail": "Kein Gast ist online. Die Aufnahme kann nicht starten.",
                "guests": []}
    if require_online and not tech_ready:
        names = ", ".join((g.get("display_name") or g.get("guest") or "?") for g in online)
        return {"ok": False, "reason": "no_guest_ready",
                "detail": f"Kein Gast ist aufnahmebereit ({names}). "
                          "Pruefe Mikrofon-Freigabe und Geraeteauswahl.",
                "guests": [g.get("guest") for g in online]}
    if require_ready:
        missing = [g for g in online if not (g.get("ready") or {}).get("declared_ready")]
        if missing:
            names = ", ".join((g.get("display_name") or g.get("guest") or "?") for g in missing)
            return {"ok": False, "reason": "not_declared_ready",
                    "detail": f"Diese Gaeste haben sich noch nicht bereit gemeldet: {names}.",
                    "guests": [g.get("guest") for g in missing]}
    return {"ok": True, "reason": "", "detail": "",
            "guests": [g.get("guest") for g in tech_ready]}


def _cmd_remember(r: dict, action: str, session: str, issued_at: int) -> bool:
    """Idempotenz: True, wenn dieser Befehl neu ist (und damit auszufuehren).

    Der Schluessel besteht aus action + session + issued_at -- exakt der Tripel,
    den auch der Recorder zum Deduplizieren nutzt. Wiederholte Zustellungen
    (Reconnect, Doppelklick, Retry) bleiben dadurch folgenlos.
    """
    seen = r.setdefault("cmd_seen", {})
    key = f"{action}|{session or ''}|{int(issued_at)}"
    now = time.time()
    for k, ts in list(seen.items()):
        if now - ts > 3600:
            seen.pop(k, None)
    if key in seen:
        return False
    seen[key] = now
    return True


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
                "guest", "client_id", "display_name", "session", "state",
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
            info["mic_mismatch_flag"] = row["mic_mismatch"]
            # Berechtigungen + Geraetepruefung (Start-Gate).
            row["permissions"]    = dict(info.get("permissions") or {})
            row["cam_active"]     = bool(info.get("cam_active", False))
            row["ready"]          = _guest_ready_state(info, conn)
            row["declared_ready"] = bool(info.get("declared_ready"))
            # Clipping-Telemetrie (live).
            row["clipping"]       = bool(info.get("clipping"))
            row["clip_count"]     = int(info.get("clip_count", 0) or 0)
            row["clip_last_dbfs"] = float(info.get("clip_last_dbfs", 0.0) or 0.0)
            row["clip_last_ts"]   = float(info.get("clip_last_ts", 0.0) or 0.0)
            row["peak"]           = float(info.get("peak", 0.0) or 0.0)
            row["mic_pending"]          = bool(info.get("mic_cmd"))
            row["mic_last_result"]      = info.get("mic_last_result")
            row["connection"]         = conn
            row["seconds_since_seen"] = round(age, 1)
            guests.append(row)
        guests.sort(key=lambda x: (x.get("display_name") or x.get("guest") or "").lower())
        lobby_rows = _lobby_list(r, now_s)
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
        # Punkt 8: Wartende Gaeste, die noch keinen Namen bestaetigt haben.
        "lobby": lobby_rows,
        "lobby_count": len(lobby_rows),
        "online_count": sum(1 for g in guests if g["connection"] == "online"),
        "ready_count": sum(1 for g in guests
                           if (g.get("ready") or {}).get("tech_ready")),
        "markers": markers, "session": cur_session,
        "marker_sessions": sessions_known,
        # Roadmap 5: Wer steuert diesen Raum gerade?
        "lock": _lock_public(_lock_get(room)),
        "guardrails": {
            "require_guest_online": bool(_cfg_get("require_guest_online")),
            "require_guest_ready":  bool(_cfg_get("require_guest_ready")),
            "clip_threshold_dbfs":  float(_cfg_get("clip_threshold_dbfs")),
            "clip_min_samples":     int(_cfg_get("clip_min_samples")),
        },
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
    raw = str(color or "").strip()
    m = re.match(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$", raw)
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:               # #abc -> #aabbcc
        h = "".join(c * 2 for c in h)
    n = int(h, 16)
    return (n >> 16) & 255, (n >> 8) & 255, n & 255


def _brand_hover(rgb: tuple[int, int, int]) -> str:
    """Hover-Farbe: ~18 % Richtung Weiss aufhellen (wie bisher im Client)."""
    mix = lambda c: max(0, min(255, round(c + (255 - c) * 0.18)))
    return "#" + "".join(f"{mix(c):02x}" for c in rgb)


# ---------------------------------------------------------------------------
# Theme-Presets und abgeleitete Farbtokens
# ---------------------------------------------------------------------------
# Bisher war nur die Primaerfarbe konfigurierbar; Hintergrund, Textfarbe und
# der Text auf Brand-Flaechen waren in jeder Seite fest verdrahtet. Jetzt gibt
# es drei versionierte Presets als Basis, die einzeln ueberschrieben werden
# koennen. Alles Weitere (Panels, Rahmen, gedaempfter Text) wird aus Hinter-
# grund und Textfarbe berechnet, damit ein helles Theme nicht von Hand
# nachgepflegt werden muss.

BRAND_PRESETS = {
    "default":  {"version": 1, "label": "Default",
                 "bg": "#0f1115", "text": "#e8eaed", "brand": "#30a46c"},
    "dark":     {"version": 1, "label": "Dark",
                 "bg": "#07080b", "text": "#f2f4f7", "brand": "#30a46c"},
    "contrast": {"version": 1, "label": "High Contrast",
                 "bg": "#000000", "text": "#ffffff", "brand": "#ffd400"},
}
DEFAULT_PRESET = "default"


def _global_presets(cfg: dict | None = None) -> dict[str, dict]:
    """Eingebaute und vom Admin angelegte globale Presets als gemeinsamer Katalog."""
    cfg = cfg or _cfg_load()
    result = {key: {**value, "key": key, "builtin": True}
              for key, value in BRAND_PRESETS.items()}
    for raw in cfg.get("global_presets") or []:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or "").strip().lower()
        if (not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", key)
                or key in BRAND_PRESETS):
            # Eingebaute Presets sind reserviert und duerfen nicht durch
            # benutzerdefinierte Datensaetze ueberschrieben werden.
            continue
        base = BRAND_PRESETS[DEFAULT_PRESET]
        result[key] = {
            "key": key,
            "label": str(raw.get("label") or base["label"])[:60],
            "version": int(raw.get("version") or base.get("version") or 1),
            "bg": str(raw.get("bg") or base["bg"]),
            "text": str(raw.get("text") or base["text"]),
            "brand": str(raw.get("brand") or base["brand"]),
            "on_brand": str(raw.get("on_brand") or ""),
            "appearance": str(raw.get("appearance") or "dark"),
            "builtin": key in BRAND_PRESETS,
            "custom": True,
        }
    return result


def _brand_preset(name: str | None = None, cfg: dict | None = None) -> dict:
    presets = _global_presets(cfg)
    key = str(name or "").strip().lower()
    return presets.get(key, presets[DEFAULT_PRESET])


def _rel_lum(rgb: tuple[int, int, int]) -> float:
    def lin(v: float) -> float:
        c = v / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2])


def _contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = _rel_lum(a), _rel_lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> str:
    """Mischt zwei Farben; t=0 ergibt a, t=1 ergibt b."""
    return "#" + "".join(f"{round(a[i] + (b[i] - a[i]) * t):02x}" for i in range(3))


def _theme_tokens(cfg: dict) -> dict:
    """Berechnet alle Theme-Variablen aus Preset plus Overrides.

    Panels, Rahmen und gedaempfter Text werden aus Hintergrund und Textfarbe
    abgeleitet. Dadurch funktioniert auch ein heller Hintergrund, ohne dass
    jede Seite eigene Regeln braucht.
    """
    preset = _brand_preset(cfg.get("brand_preset"), cfg)
    preset_key = str(preset.get("key") or DEFAULT_PRESET)

    def pick(key: str, fallback: str) -> str:
        val = str(cfg.get(key) or "").strip()
        return val if _hex_parse(val) else fallback

    bg_hex    = pick("brand_bg", preset["bg"])
    text_hex  = pick("brand_text", preset["text"])
    brand_hex = pick("brand_color", preset["brand"])

    bg    = _hex_parse(bg_hex)    or _hex_parse(preset["bg"])
    text  = _hex_parse(text_hex)  or _hex_parse(preset["text"])
    brand = _hex_parse(brand_hex) or _hex_parse(preset["brand"])

    # Panels heben sich leicht vom Hintergrund ab -- Richtung Text, damit die
    # Abstufung bei hellen wie dunklen Themes in die richtige Richtung geht.
    panel  = _mix(bg, text, 0.06)
    panel2 = _mix(bg, text, 0.03)
    border = _mix(bg, text, 0.18)
    muted  = _mix(text, bg, 0.38)
    # Weitere abgeleitete Flaechen: bisher standen diese Werte als feste
    # Hex-Codes in jeder Seite und haben jedes helle Theme gebrochen.
    muted_2       = _mix(text, bg, 0.55)   # zweite, ruhigere Textstufe
    border_strong = _mix(bg, text, 0.30)   # Rahmen auf Bedienelementen
    surface_hover = _mix(bg, text, 0.10)   # Hover-Flaeche auf Panels

    # Manuelle Buttontextfarben sind verbindlich. Schwarz/Weiss wird nur
    # automatisch gewaehlt, wenn kein Override gespeichert ist. Schlechter
    # Kontrast erzeugt eine Warnung, aber keine heimliche Ueberschreibung.
    requested_on = str(cfg.get("brand_on_brand") or preset.get("on_brand") or "").strip()
    on_brand_manual = bool(_hex_parse(str(cfg.get("brand_on_brand") or "").strip()))
    on_brand_effective = requested_on if _hex_parse(requested_on) else _brand_on(brand)

    return {
        "preset":         preset["label"],
        "preset_key":     preset_key,
        "preset_version": preset["version"],
        "bg":             bg_hex,
        "panel":          panel,
        "panel2":         panel2,
        "border":         border,
        "text":           text_hex,
        "muted":          muted,
        "muted_2":        muted_2,
        "border_strong":  border_strong,
        "surface_hover":  surface_hover,
        "brand":          brand_hex,
        "brand_hover":    _brand_hover(brand),
        "brand_on":       on_brand_effective,
        "brand_on_requested": requested_on,
        "brand_on_manual":    on_brand_manual,
        "brand_on_adjusted":  False,
        "brand_on_warning":   on_brand_manual and _contrast(_hex_parse(on_brand_effective), brand) < 4.5,
        "bg_manual":          bool(_hex_parse(str(cfg.get("brand_bg") or "").strip())),
        "text_manual":        bool(_hex_parse(str(cfg.get("brand_text") or "").strip())),
        "contrast_text_bg":   round(_contrast(text, bg), 2),
        "contrast_on_brand":  round(_contrast(_hex_parse(on_brand_effective) or (255, 255, 255), brand), 2),
    }


def _brand_on(rgb: tuple[int, int, int]) -> str:
    """Textfarbe auf Brand-Flaechen nach WCAG-Kontrast (schwarz oder weiss)."""
    def lin(v: float) -> float:
        s = v / 255.0
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    L = 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2])
    c_white = (max(L, 1.0) + 0.05) / (min(L, 1.0) + 0.05)
    c_black = (max(L, 0.0) + 0.05) / (min(L, 0.0) + 0.05)
    return "#000000" if c_black >= c_white else "#ffffff"



# ---------------------------------------------------------------------------
# Punkt 7: Branding als verwaltete Dateien (kein Data-URL-Blob in config.json)
# ---------------------------------------------------------------------------
# Frueher wanderten Logo und Favicon als base64-Data-URL in config.json. Das
# blaeht die Konfiguration auf, macht jedes Backup teuer und schickt bei jedem
# Seitenaufruf denselben Blob durch den <head>. Jetzt liegen die Bytes als
# Datei unter DATA_DIR/branding/, config.json haelt nur noch Metadaten, und
# die Auslieferung laeuft ueber eine eigene Route mit ETag/Cache-Header.

# Pro globalem Preset gibt es genau ein Logo sowie ein optionales Favicon.
BRANDING_KINDS = ("logo", "favicon")
BRANDING_MIME = {
    "image/png":     ".png",
    "image/jpeg":    ".jpg",
    "image/svg+xml": ".svg",
    "image/webp":    ".webp",
    "image/gif":     ".gif",
    "image/x-icon":  ".ico",
    "image/vnd.microsoft.icon": ".ico",
}
BRANDING_MAX_BYTES = 2 * 1024 * 1024


def _branding_asset(kind: str) -> dict | None:
    """Metadaten eines Branding-Assets; None wenn nicht gesetzt/Datei fehlt."""
    if kind not in BRANDING_KINDS:
        return None
    cfg = _cfg_load()
    preset_key = str(cfg.get("brand_preset") or DEFAULT_PRESET)
    per_preset = cfg.get("global_preset_assets") or {}
    meta = (per_preset.get(preset_key) or {}).get(kind)
    # Einmaliger Legacy-Fallback nur fuer das Default-Preset. Andernfalls
    # wuerde ein fehlendes Logo versehentlich das Logo eines anderen Presets zeigen.
    if not isinstance(meta, dict) and not per_preset and preset_key == DEFAULT_PRESET:
        meta = cfg.get(f"brand_{kind}_asset")
    if not isinstance(meta, dict):
        return None
    fname = str(meta.get("file") or "")
    if not fname or not SAFE_FILE.match(fname):
        return None
    path = BRANDING_DIR / fname
    if not path.is_file():
        return None
    return dict(meta)


def _branding_asset_path(kind: str) -> Path | None:
    meta = _branding_asset(kind)
    if not meta:
        return None
    return BRANDING_DIR / str(meta["file"])


def _branding_asset_url(kind: str) -> str:
    """Oeffentliche URL inkl. Versions-Query, damit Browser sauber neu laden."""
    meta = _branding_asset(kind)
    if not meta:
        return ""
    return f"/branding/{kind}?v={int(meta.get('updated_at') or 0)}"


def _global_preset_asset_url(preset_key: str, kind: str = "logo",
                             cfg: dict | None = None) -> str:
    cfg = cfg or _cfg_load()
    meta = ((cfg.get("global_preset_assets") or {}).get(preset_key) or {}).get(kind)
    if not isinstance(meta, dict):
        return ""
    fname = str(meta.get("file") or "")
    if not fname or not SAFE_FILE.match(fname) or not (BRANDING_DIR / fname).is_file():
        return ""
    return (f"/branding/global-preset/{preset_key}/{kind}"
            f"?v={int(meta.get('updated_at') or 0)}")


def _branding_store(kind: str, data: bytes, mime: str, original_name: str,
                    preset_key: str | None = None) -> dict:
    """Schreibt ein Branding-Asset und ersetzt ein evtl. vorhandenes."""
    if kind not in BRANDING_KINDS:
        raise HTTPException(400, "Unbekannter Branding-Typ")
    if not data:
        raise HTTPException(400, "Leere Datei")
    if len(data) > BRANDING_MAX_BYTES:
        raise HTTPException(413, "Datei zu gross (max. 2 MB)")
    mime = (mime or "").split(";")[0].strip().lower()
    ext = BRANDING_MIME.get(mime)
    if not ext:
        raise HTTPException(415, "Nicht unterstuetztes Bildformat")
    if mime == "image/svg+xml" and re.search(rb"<script|javascript:|onload=",
                                             data[:200000], re.I):
        # SVG kann Skripte tragen -> aktive Inhalte werden abgelehnt.
        raise HTTPException(400, "SVG mit aktiven Inhalten wird abgelehnt")

    cfg = _cfg_load()
    preset_key = str(preset_key or cfg.get("brand_preset") or DEFAULT_PRESET)
    old_meta = ((cfg.get("global_preset_assets") or {}).get(preset_key) or {}).get(kind)
    old = (BRANDING_DIR / str(old_meta.get("file"))) if isinstance(old_meta, dict) and SAFE_FILE.match(str(old_meta.get("file") or "")) else None
    fname = f"{kind}-{secrets.token_hex(8)}{ext}"
    (BRANDING_DIR / fname).write_bytes(data)
    meta = {
        "file": fname,
        "name": str(original_name or fname)[:120],
        "size": len(data),
        "mime": mime,
        "updated_at": int(time.time()),
    }
    per_preset = dict(cfg.get("global_preset_assets") or {})
    slot = dict(per_preset.get(preset_key) or {})
    slot[kind] = meta
    per_preset[preset_key] = slot
    cfg["global_preset_assets"] = per_preset
    cfg[f"brand_{kind}_asset"] = meta
    if kind == "favicon":
        cfg["brand_favicon"] = ""   # Legacy-Data-URL ist damit abgeloest.
    _cfg_save(cfg)
    if old and old.name != fname:
        try:
            old.unlink()
        except OSError:
            pass
    return meta


def _branding_clear(kind: str, preset_key: str | None = None) -> None:
    cfg = _cfg_load()
    preset_key = str(preset_key or cfg.get("brand_preset") or DEFAULT_PRESET)
    old_meta = ((cfg.get("global_preset_assets") or {}).get(preset_key) or {}).get(kind)
    old = ((BRANDING_DIR / str(old_meta.get("file")))
           if isinstance(old_meta, dict)
           and SAFE_FILE.match(str(old_meta.get("file") or "")) else None)
    per_preset = dict(cfg.get("global_preset_assets") or {})
    slot = dict(per_preset.get(preset_key) or {})
    slot[kind] = None
    per_preset[preset_key] = slot
    cfg["global_preset_assets"] = per_preset
    cfg[f"brand_{kind}_asset"] = None
    if kind == "favicon":
        cfg["brand_favicon"] = ""
    _cfg_save(cfg)
    if old:
        try:
            old.unlink()
        except OSError:
            pass


def _preset_asset_url(preset_id: str, meta: dict | None) -> str:
    if not preset_id or not isinstance(meta, dict):
        return ""
    fname = str(meta.get("file") or "")
    if not fname or not SAFE_FILE.match(fname) or not (BRANDING_DIR / fname).is_file():
        return ""
    return f"/branding/preset/{preset_id}/logo?v={int(meta.get('updated_at') or 0)}"


def _room_presets_public(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or _cfg_load()
    result = []
    for raw in cfg.get("branding_presets") or []:
        if not isinstance(raw, dict) or not raw.get("id") or not raw.get("name"):
            continue
        p = dict(raw)
        p["logo"] = _preset_asset_url(str(p["id"]), p.get("logo_asset"))
        result.append(p)
    return result


def _room_preset(room: str, cfg: dict | None = None) -> dict | None:
    cfg = cfg or _cfg_load()
    preset_id = str((cfg.get("room_preset_assignments") or {}).get(room) or "")
    return next((p for p in _room_presets_public(cfg) if str(p.get("id")) == preset_id), None)


def _branding_public() -> dict:
    """Was alle Seiten (und das Admin-Panel) ueber das Branding wissen muessen."""
    cfg = _cfg_load()
    legacy = str(cfg.get("brand_favicon", "") or "")
    logo = _branding_asset("logo")
    fav = _branding_asset("favicon")
    tok = _theme_tokens(cfg)
    return {
        "ok": True,
        "name":  cfg.get("brand_name", "Podcast Studio"),
        "color": tok["brand"],
        # favicon bleibt aus Kompatibilitaet ein einzelnes URL-Feld.
        "favicon": _branding_asset_url("favicon") or legacy,
        "logo":    _branding_asset_url("logo"),
        "theme": tok,
        "presets": [
            {**v, "logo": _global_preset_asset_url(k, "logo", cfg)}
            for k, v in _global_presets(cfg).items()
        ],
        "room_presets": _room_presets_public(cfg),
        "room_preset_assignments": cfg.get("room_preset_assignments") or {},
        "room_branding": _cfg_get("room_branding") or {},
        "assets": {
            "logo":       {**logo, "url": _branding_asset_url("logo")} if logo else None,
            "favicon":    {**fav, "url": _branding_asset_url("favicon")} if fav else None,
        },
        "legacy_favicon": bool(legacy and not fav),
    }


def _script_json(value) -> str:
    """JSON embedded in HTML must not be able to close its script element."""
    return json_dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _branding_head(room: str | None = None) -> str:
    """Baut den <head>-Block: Theme-Variablen, Titel-Suffix, Favicon, JSON."""
    cfg = _cfg_load()
    tok = _theme_tokens(cfg)
    name    = str(cfg.get("brand_name", "Podcast Studio"))
    favicon = _branding_asset_url("favicon") or str(cfg.get("brand_favicon", ""))
    logo    = _branding_asset_url("logo")

    # Ein Raum verweist auf ein vom Admin vorbereitetes Preset. Das Preset
    # wirkt ausschliesslich im Gast-Recorder; Host und Admin behalten bewusst
    # das globale Theme, sehen aber den Preset-Namen als Kontext.
    room_over = {}
    if room:
        preset = _room_preset(room, cfg)
        if preset:
            room_over = preset
            if preset.get("name"):
                name = str(preset["name"])[:60]
            if preset.get("logo"):
                logo = str(preset["logo"])
            p_cfg = {
                "brand_preset": preset.get("appearance") == "light" and "default" or "dark",
                "brand_color": preset.get("brand_color") or cfg.get("brand_color"),
                "brand_bg": preset.get("bg") or ("#f6f7f9" if preset.get("appearance") == "light" else "#0f1115"),
                "brand_text": preset.get("text") or ("#15171b" if preset.get("appearance") == "light" else "#e8eaed"),
                "brand_on_brand": preset.get("on_brand") or "",
            }
            tok = _theme_tokens(p_cfg)

    css = (
        '<style id="brand-vars">:root{'
        f'--bg:{tok["bg"]};'
        f'--panel:{tok["panel"]};'
        f'--panel2:{tok["panel2"]};'
        f'--border:{tok["border"]};'
        f'--text:{tok["text"]};'
        f'--muted:{tok["muted"]};'
        f'--muted-2:{tok["muted_2"]};'
        f'--border-strong:{tok["border_strong"]};'
        f'--surface-hover:{tok["surface_hover"]};'
        f'--brand:{tok["brand"]};'
        f'--brand-hover:{tok["brand_hover"]};'
        f'--brand-on:{tok["brand_on"]};'
        '}</style>'
    )

    ico = ""
    if favicon and favicon.startswith(("data:", "/", "http")):
        ico = f'<link rel="icon" href="{html_escape(favicon, quote=True)}">'

    payload = _script_json({
        "ok": True, "name": name, "color": tok["brand"],
        "favicon": favicon, "logo": logo,
        "theme": tok, "room": room or "", "room_branding": room_over,
    })
    js = f"<script>window.__BRANDING__={payload};</script>"
    return css + ico + js


def _render_page(filename: str, status_code: int = 200, room: str | None = None) -> HTMLResponse:
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

    block = _branding_head(room)
    global_locale = _global_locale()
    page_locale = _room_locale(room) if room else global_locale
    locale_bootstrap = r"""
<style id="a11y-base">
/* Punkt 6: sichtbare Tastatur-Fokuszustaende auf allen Seiten.
   :focus-visible trifft nur Tastatur-/AT-Navigation, Mausklicks bleiben ruhig.
   Zwei Ringe (Marke + dunkler Aussenring) halten den Kontrast auf hellen
   UND dunklen Flaechen ueber 3:1. */
:where(a,button,input,select,textarea,summary,[tabindex]:not([tabindex="-1"])):focus-visible{
  outline:3px solid var(--brand,#30a46c);
  outline-offset:2px;
  box-shadow:0 0 0 5px rgba(0,0,0,.55);
  border-radius:6px;
}
/* Fokus im Dateiauswahl-Wrapper sichtbar machen: der native Input ist
   optisch versteckt, der Ring gehoert deshalb an das umgebende Label. */
.file-btn:focus-within{outline:3px solid var(--brand,#30a46c);outline-offset:2px}
/* Sprungmarke: nur sichtbar, wenn sie den Fokus hat. */
.skip-link{position:absolute;left:-9999px;top:0;z-index:9999;padding:10px 16px;
  background:var(--brand,#30a46c);color:var(--brand-on,#fff);border-radius:0 0 8px 0;
  font:600 14px/1.2 inherit;text-decoration:none}
.skip-link:focus{left:0}
.visually-hidden{position:absolute;width:1px;height:1px;margin:-1px;padding:0;
  overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation-duration:.001ms !important;animation-iteration-count:1 !important;
    transition-duration:.001ms !important;scroll-behavior:auto !important}
}
/* Punkt 6: enge Viewports. Listen und Raster brechen auf eine Spalte um,
   Bedienflaechen bleiben mindestens 44 px hoch. */
@media (max-width: 720px){
  .wrap,.panel{padding-left:12px;padding-right:12px}
  .topbar{flex-wrap:wrap;gap:8px}
  .topbar .row{flex-wrap:wrap}
  .guest-grid,.guests,.rooms-grid,.overview-dashboard{grid-template-columns:1fr !important}
  .mk-row{display:grid !important;grid-template-columns:auto 1fr;gap:6px 10px;align-items:center}
  .mk-shifts{grid-column:1/-1;display:flex;flex-wrap:wrap}
  .mk-note{grid-column:1/-1;width:100%}
  .cfg-row{flex-direction:column;align-items:flex-start !important;gap:8px}
  .link-box{flex-direction:column;align-items:stretch}
  .link-box input,.link-box button{width:100%}
  button,.btn,[role="button"]{min-height:44px}
  table{display:block;overflow-x:auto}
}
</style>
<script id="localization-bootstrap">
window.OpenPodcastI18n = {
  locale: __PAGE_LOCALE__,
  globalLocale: __GLOBAL_LOCALE__,
  data: {}, ui: {}, _uiKeys: [], _uiSubKeys: [], _patterns: [],
  _textSources: new WeakMap(), _attributeSources: new WeakMap(), _loadVersion: 0,
  setData(code, data) {
    this.locale = code;
    this.data = data || {};
    this.ui = this.data.ui || {};
    this._translatedValues = new Set(Object.values(this.ui));
    this._uiKeys = Object.keys(this.ui).sort((a,b) => b.length-a.length);
    this._uiSubKeys = this._uiKeys.filter(k => k.trim().length >= 3 && this.ui[k] !== k);
    const escape = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    this._patterns = Object.entries(this.data.patterns || {}).map(([source, target]) => {
      const ids = [];
      const tokens = source.split(/(\{\d+\})/g);
      const expression = tokens.map(part => {
        const m = /^\{(\d+)\}$/.exec(part);
        if (!m) return escape(part);
        ids.push(m[1]); return '(.*?)';
      }).join('');
      return {re: new RegExp('^' + expression + '$', 's'), ids, target, source,
        weight: source.replace(/\{\d+\}/g, '').length};
    }).sort((a,b) => b.weight-a.weight);
    document.documentElement.lang = code;
  },
  async load(locale) {
    const code = locale || this.locale || 'de';
    const version = ++this._loadVersion;
    const res = await fetch('/locales/' + encodeURIComponent(code), {cache:'no-store'});
    if (!res.ok) throw new Error('Locale HTTP ' + res.status);
    const data = await res.json();
    if (version !== this._loadVersion) return; // A newer selection always wins.
    this.setData(data.meta?.code || code, data);
    this.apply(document);
    document.dispatchEvent(new CustomEvent('i18n:applied', {detail:{locale:this.locale}}));
  },
  t(key, fallback) {
    return key.split('.').reduce((o,k)=>o && o[k], this.data) ?? fallback ?? key;
  },
  s(text) {
    const raw = String(text == null ? '' : text);
    if (this.locale === 'de') return raw;
    const key = raw.trim();
    if (!key) return raw;
    const compact = key.replace(/\s+/g, ' ');
    const lead = raw.match(/^\s*/)[0], trail = raw.match(/\s*$/)[0];
    const hit = this.ui[key] ?? this.ui[compact];
    if (hit != null) return lead + hit + trail;
    if (this._translatedValues.has(key) || this._translatedValues.has(compact)) return raw;
    for (const p of this._patterns) {
      const match = p.re.exec(compact);
      if (!match) continue;
      const values = {};
      p.ids.forEach((id,i) => { values[id] = match[i+1]; });
      // These captures are application-generated status/error messages, not names.
      const messageSlots = {
        'Gast {0}: {1}, wartet seit {2} Sekunden': ['1'],
        'Fehler: {0}': ['0'],
        'Wechsel fehlgeschlagen: {0}': ['0'],
        'Keine Aufnahmebereitschaft. {0}': ['0']
      };
      for (const id of messageSlots[p.source] || []) {
        if (values[id] !== compact) values[id] = this.s(values[id]);
      }
      // Captures can contain names, room IDs or device labels. Never translate them.
      return lead + p.target.replace(/\{(\d+)\}/g, (all,id) => values[id] ?? all) + trail;
    }
    // Legacy mixed text: match whole words only and never reprocess replacements.
    // In particular, German "Marker" must not match English "Markers".
    const parts = [];
    let out = raw;
    const word = c => !!c && /[\p{L}\p{N}_]/u.test(c);
    for (const source of this._uiSubKeys) {
      let cursor = 0, next = '', at;
      while ((at = out.indexOf(source, cursor)) !== -1) {
        const end = at + source.length;
        if ((word(source[0]) && word(out[at-1])) ||
            (word(source[source.length-1]) && word(out[end]))) {
          next += out.slice(cursor, end); cursor = end; continue;
        }
        next += out.slice(cursor, at) + '\u0000' + (parts.push(this.ui[source])-1) + '\u0000';
        cursor = end;
      }
      out = next + out.slice(cursor);
    }
    return out.replace(/\u0000(\d+)\u0000/g, (_,i) => parts[Number(i)]);
  },
  _excluded(el) {
    return !el || !!el.closest('script,style,textarea,code,pre,[translate="no"],[data-i18n-ignore]');
  },
  _translateNode(n) {
    if (this._excluded(n.parentElement) || !n.nodeValue.trim()) return;
    const prior = this._textSources.get(n);
    const source = prior && n.nodeValue === prior.output ? prior.source : n.nodeValue;
    const output = this.s(source);
    this._textSources.set(n, {source, output});
    if (n.nodeValue !== output) n.nodeValue = output;
  },
  _translateTextNodes(root) {
    if (root.nodeType === 3) { this._translateNode(root); return; }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) this._translateNode(walker.currentNode);
  },
  _elements(root, selector) {
    const result = root.querySelectorAll ? [...root.querySelectorAll(selector)] : [];
    if (root.matches && root.matches(selector)) result.unshift(root);
    return result;
  },
  _translateAttributes(root) {
    const attrs = ['placeholder','title','aria-label','alt','value'];
    for (const el of this._elements(root, attrs.map(a=>'['+a+']').join(','))) {
      if (this._excluded(el)) continue;
      const cache = this._attributeSources.get(el) || {};
      for (const a of attrs) {
        if (a === 'value' && !(el.tagName === 'INPUT' && ['button','submit'].includes(el.type))) continue;
        const value = el.getAttribute(a);
        if (value == null) continue;
        const source = cache[a] && value === cache[a].output ? cache[a].source : value;
        const output = this.s(source);
        cache[a] = {source, output};
        if (value !== output) el.setAttribute(a, output);
      }
      this._attributeSources.set(el, cache);
    }
  },
  apply(root=document) {
    if (!root) return;
    this._translateTextNodes(root);
    this._translateAttributes(root);
    for (const attr of ['data-i18n','data-i18n-html','data-i18n-placeholder','data-i18n-title','data-i18n-aria-label']) {
      for (const el of this._elements(root, '['+attr+']')) {
        if (this._excluded(el)) continue;
        const value = this.t(el.getAttribute(attr), null);
        if (value === el.getAttribute(attr) || value == null) continue;
        if (attr === 'data-i18n') { if (el.textContent !== value) el.textContent = value; }
        else if (attr === 'data-i18n-html') { if (el.innerHTML !== value) el.innerHTML = value; }
        else {
          const target = attr.slice('data-i18n-'.length);
          if (el.getAttribute(target) !== value) el.setAttribute(target, value);
        }
      }
    }
  },
  install() {
    if (this._installed) return;
    this._installed = true;
    const originalAlert = window.alert.bind(window), originalConfirm = window.confirm.bind(window);
    window.alert = message => originalAlert(this.translateMessage(message));
    window.confirm = message => originalConfirm(this.translateMessage(message));
    const start = () => {
      this._injectSkipLink();
      this.apply(document);
      const pending = new Set();
      const observer = new MutationObserver(records => {
        for (const record of records) {
          if (record.type === 'childList') {
            for (const node of record.addedNodes) pending.add(node);
          } else pending.add(record.target);
        }
        if (this._pending || !pending.size) return;
        this._pending = true;
        requestAnimationFrame(() => {
          this._pending = false;
          // Stop observing our own writes; only changed subtrees need work.
          observer.disconnect();
          try { for (const node of pending) if (node.isConnected) this.apply(node); }
          finally { pending.clear(); observer.observe(document.body, options); }
        });
      });
      const options = {childList:true, subtree:true, characterData:true, attributes:true,
        attributeFilter:['placeholder','title','aria-label','alt','value']};
      if (document.body) observer.observe(document.body, options);
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once:true});
    else start();
  },
  _injectSkipLink() {
    if (document.querySelector('.skip-link')) return;
    const main = document.querySelector('main, .wrap');
    if (!main) return;
    if (!main.id) main.id = 'main-content';
    const a = document.createElement('a');
    a.className = 'skip-link'; a.href = '#' + main.id;
    a.dataset.i18n = 'common.skip_to_content';
    a.textContent = this.t('common.skip_to_content', 'Zum Inhalt springen');
    document.body.insertBefore(a, document.body.firstChild);
    if (!main.hasAttribute('tabindex')) main.setAttribute('tabindex', '-1');
  },
  translateMessage(message) {
    const text = String(message ?? '');
    return this.data.messages?.[text] ?? this.s(text);
  }
};
window.OpenPodcastI18n.setData(__PAGE_LOCALE__, __LOCALE_DATA__);
window.OpenPodcastI18n.install();
// ---------------------------------------------------------------------------
// Branding-Logo des aktiven Presets
// ---------------------------------------------------------------------------
// Jedes Preset besitzt genau ein Logo. Eine automatische Hell-/Dunkel-Variante
// gibt es bewusst nicht mehr; das Logo wird zusammen mit dem Preset gepflegt.
window.OpenPodcastBranding = {
  get data() { return window.__BRANDING__ || {}; },
  logoUrl() {
    return this.data.logo || '';
  },
  brandName() { return this.data.name || 'Podcast Studio'; },
  // Setzt <img> auf das passende Logo; blendet es aus, wenn keins existiert.
  applyLogo(img) {
    if (!img) return false;
    const url = this.logoUrl();
    if (!url) { img.removeAttribute('src'); return false; }
    img.src = url;
    img.alt = this.brandName();
    return true;
  }
};

// ---------------------------------------------------------------------------
// Punkt 6: Dialog-Verhalten fuer alle Seiten an einer Stelle
// ---------------------------------------------------------------------------
// Die Modals (Gastlink, Clipping, Gast-Logs) waren nur per Maus bedienbar:
// kein Escape, kein Fokus im Dialog, und der Tab-Fokus lief hinter dem
// Overlay weiter. Statt das in jeder Seite einzeln zu loesen, beobachten wir
// hier zentral jedes [role="dialog"] und ergaenzen das fehlende Verhalten.
window.OpenPodcastDialogs = {
  _open: null,
  _lastFocus: null,
  FOCUSABLE: 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])',

  isVisible(el) {
    if (el.hidden) return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden';
  },

  focusables(dialog) {
    return [...dialog.querySelectorAll(this.FOCUSABLE)].filter(el => this.isVisible(el));
  },

  onOpen(dialog) {
    if (this._open === dialog) return;
    this._open = dialog;
    this._lastFocus = document.activeElement;
    // Alles ausserhalb des Dialogs fuer Screenreader stummschalten.
    [...document.body.children].forEach(node => {
      if (node !== dialog && !node.contains(dialog) && !node.classList.contains('skip-link')) {
        if (!node.hasAttribute('aria-hidden')) { node.setAttribute('aria-hidden', 'true'); node.dataset.opsInert = '1'; }
      }
    });
    const first = this.focusables(dialog)[0];
    (first || dialog).focus({ preventScroll: true });
    if (!first && !dialog.hasAttribute('tabindex')) dialog.setAttribute('tabindex', '-1');
  },

  onClose() {
    if (!this._open) return;
    document.querySelectorAll('[data-ops-inert]').forEach(node => {
      node.removeAttribute('aria-hidden'); delete node.dataset.opsInert;
    });
    this._open = null;
    // Fokus zurueck auf das Element, das den Dialog geoeffnet hat.
    if (this._lastFocus && document.contains(this._lastFocus)) {
      try { this._lastFocus.focus({ preventScroll: true }); } catch {}
    }
    this._lastFocus = null;
  },

  // Schliessen ohne die Schliess-Logik der Seite zu kennen: wir klicken den
  // vorhandenen Schliessen-Button. So bleibt jede Seite Herr ihres Zustands.
  requestClose(dialog) {
    const btn = dialog.querySelector('[data-dialog-close],[id$="Close"],[id^="close"],#closeModal');
    if (btn) { btn.click(); return; }
    dialog.style.display = 'none';
    dialog.classList.remove('show');
    this.onClose();
  },

  install() {
    document.addEventListener('keydown', (e) => {
      const dialog = this._open;
      if (!dialog) return;
      if (e.key === 'Escape') { e.preventDefault(); this.requestClose(dialog); return; }
      if (e.key !== 'Tab') return;
      const items = this.focusables(dialog);
      if (!items.length) { e.preventDefault(); return; }
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });

    const scan = () => {
      const dialogs = [...document.querySelectorAll('[role="dialog"]')];
      const open = dialogs.find(d => this.isVisible(d));
      if (open) this.onOpen(open); else this.onClose();
    };
    document.addEventListener('DOMContentLoaded', () => {
      scan();
      new MutationObserver(scan).observe(document.body, {
        attributes: true, attributeFilter: ['style', 'class', 'hidden'], subtree: true, childList: true,
      });
    });
  }
};
window.OpenPodcastDialogs.install();

</script>"""
    locale_bootstrap = locale_bootstrap.replace("__PAGE_LOCALE__", json_dumps(page_locale)).replace("__GLOBAL_LOCALE__", json_dumps(global_locale))
    locale_bootstrap = locale_bootstrap.replace("__LOCALE_DATA__", _script_json(_load_locale(page_locale)))
    block += locale_bootstrap
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


@app.get("/locales/{locale}")
def locale_data(locale: str):
    code = _normalise_locale(locale)
    return JSONResponse(_load_locale(code), headers={"Cache-Control": "no-store"})


@app.get("/locales")
def locales():
    return {"ok": True, "locales": _available_locales()}


@app.get("/branding")
def branding():
    """Oeffentliches Branding (Name/Farbe/Logo/Favicon) fuer alle Seiten."""
    return _branding_public()


@app.get("/branding/preset/{preset_id}/logo")
def branding_preset_logo(preset_id: str):
    preset = next((p for p in (_cfg_get("branding_presets") or [])
                   if isinstance(p, dict) and str(p.get("id")) == preset_id), None)
    meta = preset.get("logo_asset") if preset else None
    if not isinstance(meta, dict) or not SAFE_FILE.match(str(meta.get("file") or "")):
        raise HTTPException(404, "Kein Preset-Logo hinterlegt")
    path = BRANDING_DIR / str(meta["file"])
    if not path.is_file():
        raise HTTPException(404, "Preset-Logo fehlt")
    return FileResponse(path, media_type=str(meta.get("mime") or "application/octet-stream"),
                        headers={"Cache-Control": "public, max-age=604800, immutable"})


@app.get("/branding/global-preset/{preset_key}/{kind}")
def branding_global_preset_file(preset_key: str, kind: str):
    if kind not in BRANDING_KINDS or preset_key not in _global_presets():
        raise HTTPException(404, "Preset-Asset nicht gefunden")
    cfg = _cfg_load()
    meta = ((cfg.get("global_preset_assets") or {}).get(preset_key) or {}).get(kind)
    if not isinstance(meta, dict) or not SAFE_FILE.match(str(meta.get("file") or "")):
        raise HTTPException(404, "Preset-Asset nicht gefunden")
    path = BRANDING_DIR / str(meta["file"])
    if not path.is_file():
        raise HTTPException(404, "Preset-Asset fehlt")
    return FileResponse(path, media_type=str(meta.get("mime") or "application/octet-stream"),
                        headers={"Cache-Control": "public, max-age=604800, immutable"})


@app.post("/admin/branding/global-preset/{preset_key}/{kind}")
async def admin_global_preset_asset_upload(preset_key: str, kind: str,
                                           request: Request,
                                           _role=Depends(require_admin)):
    cfg = _cfg_load()
    if preset_key not in _global_presets(cfg) or kind not in BRANDING_KINDS:
        raise HTTPException(404, "Preset-Asset nicht gefunden")
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(400, "Feld 'file' fehlt")
    meta = _branding_store(kind, await upload.read(),
                           getattr(upload, "content_type", "") or "",
                           getattr(upload, "filename", "") or kind,
                           preset_key=preset_key)
    return {"ok": True, "kind": kind,
            "asset": {**meta, "url": _global_preset_asset_url(preset_key, kind)}}


@app.delete("/admin/branding/global-preset/{preset_key}/{kind}")
def admin_global_preset_asset_delete(preset_key: str, kind: str,
                                     _role=Depends(require_admin)):
    cfg = _cfg_load()
    if preset_key not in _global_presets(cfg) or kind not in BRANDING_KINDS:
        raise HTTPException(404, "Preset-Asset nicht gefunden")
    _branding_clear(kind, preset_key=preset_key)
    return {"ok": True, "kind": kind, "asset": None}


@app.get("/branding/{kind}")
def branding_file(kind: str):
    """Liefert ein verwaltetes Branding-Asset aus DATA_DIR/branding/ aus."""
    if kind not in BRANDING_KINDS:
        raise HTTPException(404, "Unbekannter Branding-Typ")
    meta = _branding_asset(kind)
    if not meta:
        raise HTTPException(404, "Kein Asset hinterlegt")
    path = BRANDING_DIR / str(meta["file"])
    return FileResponse(
        path,
        media_type=str(meta.get("mime") or "application/octet-stream"),
        headers={
            # Der Dateiname enthaelt ein Zufallstoken und die URL eine Version:
            # Ein neues Asset bekommt eine neue URL, daher ist langes Caching sicher.
            "Cache-Control": "public, max-age=604800, immutable",
        },
    )


@app.post("/admin/branding/preset/{preset_id}/logo")
async def admin_preset_logo_upload(preset_id: str, request: Request,
                                   _role=Depends(require_admin)):
    cfg = _cfg_load()
    presets = list(cfg.get("branding_presets") or [])
    idx = next((i for i, p in enumerate(presets)
                if isinstance(p, dict) and str(p.get("id")) == preset_id), -1)
    if idx < 0:
        raise HTTPException(404, "Branding-Preset nicht gefunden")
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(400, "Feld 'file' fehlt")
    data = await upload.read()
    mime = (getattr(upload, "content_type", "") or "").split(";")[0].lower()
    ext = BRANDING_MIME.get(mime)
    if not data or not ext or len(data) > BRANDING_MAX_BYTES:
        raise HTTPException(400, "Ungültige Logo-Datei")
    fname = f"preset-{preset_id}-{secrets.token_hex(8)}{ext}"
    (BRANDING_DIR / fname).write_bytes(data)
    meta = {"file": fname, "name": str(getattr(upload, "filename", "") or fname)[:120],
            "size": len(data), "mime": mime, "updated_at": int(time.time())}
    old = presets[idx].get("logo_asset")
    presets[idx] = {**presets[idx], "logo_asset": meta}
    cfg["branding_presets"] = presets
    _cfg_save(cfg)
    if isinstance(old, dict) and SAFE_FILE.match(str(old.get("file") or "")):
        try: (BRANDING_DIR / str(old["file"])).unlink()
        except OSError: pass
    return {"ok": True, "asset": {**meta, "url": _preset_asset_url(preset_id, meta)}}


@app.delete("/admin/branding/preset/{preset_id}/logo")
def admin_preset_logo_delete(preset_id: str, _role=Depends(require_admin)):
    cfg = _cfg_load()
    presets = list(cfg.get("branding_presets") or [])
    idx = next((i for i, p in enumerate(presets)
                if isinstance(p, dict) and str(p.get("id")) == preset_id), -1)
    if idx < 0:
        raise HTTPException(404, "Branding-Preset nicht gefunden")
    old = presets[idx].get("logo_asset")
    presets[idx] = {**presets[idx], "logo_asset": None}
    cfg["branding_presets"] = presets
    _cfg_save(cfg)
    if isinstance(old, dict) and SAFE_FILE.match(str(old.get("file") or "")):
        try: (BRANDING_DIR / str(old["file"])).unlink()
        except OSError: pass
    return {"ok": True}


@app.post("/admin/branding/{kind}")
async def admin_branding_upload(kind: str, request: Request,
                                _role=Depends(require_admin)):
    """Laedt Logo oder Favicon als verwaltete Datei hoch (multipart oder raw)."""
    if kind not in BRANDING_KINDS:
        raise HTTPException(404, "Unbekannter Branding-Typ")
    ctype = (request.headers.get("content-type") or "").lower()
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(400, "Feld 'file' fehlt")
        data = await upload.read()
        mime = getattr(upload, "content_type", "") or ""
        name = getattr(upload, "filename", "") or kind
    else:
        data = await request.body()
        mime = ctype
        name = request.headers.get("x-filename", kind)
    meta = _branding_store(kind, data, mime, name)
    return {"ok": True, "kind": kind, "asset": {**meta, "url": _branding_asset_url(kind)}}


@app.delete("/admin/branding/{kind}")
def admin_branding_delete(kind: str, _role=Depends(require_admin)):
    if kind not in BRANDING_KINDS:
        raise HTTPException(404, "Unbekannter Branding-Typ")
    _branding_clear(kind)
    return {"ok": True, "kind": kind, "asset": None}


@app.post("/admin/branding/theme/preview")
async def admin_branding_preview(request: Request, _role=Depends(require_admin)):
    """Berechnet die Theme-Tokens, ohne etwas zu speichern.

    Das Admin-Panel nutzt das fuer die Live-Vorschau: dieselbe Berechnung wie
    beim Ausliefern, damit die Vorschau nicht von der spaeteren Realitaet
    abweicht (inkl. Kontrastkorrektur der Buttonschrift).
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    cfg = dict(_cfg_load())
    for key in ("brand_preset", "brand_color", "brand_bg", "brand_text", "brand_on_brand"):
        if key in payload:
            cfg[key] = str(payload[key] or "")
    return {"ok": True, "theme": _theme_tokens(cfg)}


@app.post("/admin/branding/theme/reset")
def admin_branding_reset(_role=Depends(require_admin)):
    """Setzt Farben auf das Default-Preset zurueck; Dateien bleiben erhalten."""
    cfg = _cfg_load()
    cfg["brand_preset"]   = DEFAULT_PRESET
    cfg["brand_color"]    = ""
    cfg["brand_bg"]       = ""
    cfg["brand_text"]     = ""
    cfg["brand_on_brand"] = ""
    _cfg_save(cfg)
    return {"ok": True, "theme": _theme_tokens(cfg)}


@app.get("/admin.html")
@app.get("/admin")
def admin(_role=Depends(require_admin)):
    return _render_page("admin.html")


@app.get("/host.html")
@app.get("/host")
def host(request: Request, _auth=Depends(require_auth)):
    # Das Host-Studio bleibt global eingefärbt. Das gewählte Raum-Preset wird
    # dort nur als Label neben dem Raumnamen angezeigt; die Gastseite erhält
    # das eigentliche Raum-Theme über den tokengebundenen Recorder-Render.
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
    return _render_page("recorder.html", room=info.get("room"))


@app.post("/lobby/{room}")
async def lobby_ping(room: str, request: Request):
    """Recorder meldet Anwesenheit in der Lobby -- vor der Namenseingabe.

    Abgesichert ueber den Gast-Token: nur wer den Einladungslink hat, kann
    im Host-Panel auftauchen.
    """
    check_ident(room)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid lobby payload")
    token = str(payload.get("token") or "")
    info = _token_resolve(token) if token else None
    if info is None or info.get("room") != room:
        raise HTTPException(403, "Token ungueltig oder abgelaufen")
    client_id = str(payload.get("client_id") or "")[:64]
    if not SAFE.match(client_id):
        raise HTTPException(400, "client_id fehlt oder ist ungueltig")
    if payload.get("leave"):
        _lobby_drop(room, client_id)
    else:
        _lobby_touch(room, client_id, str(payload.get("stage") or "naming"))
    await _broadcast_host_status(room)
    return {"ok": True}


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
        "locale":     _room_locale(info["room"]),
        "branding":   ((_cfg_get("room_branding") or {}).get(info["room"]) or {}),
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
    try:
        dest.write_bytes(data)
    except OSError as e:
        # Roadmap 6: Schreibfehler (z. B. volles Volume) muss der Admin sehen.
        _diag_bump("upload_errors")
        _error_record("upload", "Chunk konnte nicht geschrieben werden",
                      room=room, detail=f"{guest}/{session}/{chunk}: {e}")
        raise HTTPException(507, "Chunk konnte nicht gespeichert werden")
    _diag_bump("upload_chunks")
    _diag_bump("upload_bytes", len(data))
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
        _diag_bump("finish_errors")
        _error_record("merge", "Finish ohne Chunks angefordert",
                      room=room, detail=f"{guest}/{session}")
        raise HTTPException(404, "Keine Chunks vorhanden")

    _diag_bump("finish_ok")

    # Feature 7: Marker in WAV schreiben (nur Marker dieser Session)
    try:
        _wav_add_markers(wav_path, _marker_list(room, session))
    except Exception as e:
        _error_record("markers", "Marker konnten nicht in die WAV geschrieben werden",
                      room=room, detail=str(e))

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


# ── Host-Lock-API (Roadmap 5) ────────────────────────────────────────────────
# Der Lock ist auch ohne WebSocket explizit steuerbar. Das macht Erwerb,
# Erneuerung, Freigabe und Wiederaufnahme testbar und erlaubt einen sauberen
# Release beim Schliessen des Tabs (sendBeacon/keepalive).

def _lock_client_id(payload: dict) -> str:
    cid = str(payload.get("client_id") or "")[:64]
    return cid if re.match(r"^[A-Za-z0-9_-]{8,64}$", cid) else ""


@app.get("/host/lock/{room}")
def host_lock_state(room, role=Depends(require_auth), client_id: str = ""):
    """Aktueller Lock-Zustand eines Raums (ohne ihn zu erwerben)."""
    check_ident(room)
    lock = _lock_get(room)
    cid = _lock_client_id({"client_id": client_id})
    mine = bool(lock and cid and lock["client_id"] == cid)
    return {"ok": True, "room": room, "locked": bool(lock), "mine": mine,
            "mode": "control" if mine else ("readonly" if lock else "free"),
            "lock": _lock_public(lock)}


@app.post("/host/lock/{room}/acquire")
async def host_lock_acquire(room, request: Request, role=Depends(require_auth)):
    """Lock erwerben. Body: { client_id, label?, force? }

    `force` ist nur fuer Admins erlaubt: ein Admin kann eine haengende, aber
    formal noch gueltige Host-Instanz uebernehmen. Hosts bekommen bei einem
    fremden Lock immer `mode: "readonly"`.
    """
    check_ident(room)
    _room_register(room)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    cid = _lock_client_id(payload)
    if not cid:
        raise HTTPException(400, "client_id fehlt oder ist ungueltig")
    label = str(payload.get("label") or "")[:80]
    force = bool(payload.get("force")) and role == "admin"
    res = _lock_acquire(room, cid, role=role, label=label, force=force)
    res.update({"ok": True, "room": room})
    if res["mode"] == "control":
        await _broadcast_host_status(room)
    return res


@app.post("/host/lock/{room}/renew")
async def host_lock_renew(room, request: Request, role=Depends(require_auth)):
    """Lock erneuern. Body: { client_id }"""
    check_ident(room)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    cid = _lock_client_id(payload)
    if not cid:
        raise HTTPException(400, "client_id fehlt oder ist ungueltig")
    res = _lock_renew(room, cid)
    res.update({"ok": True, "room": room})
    return res


@app.post("/host/lock/{room}/release")
async def host_lock_release(room, request: Request, role=Depends(require_auth)):
    """Lock freigeben. Body: { client_id }"""
    check_ident(room)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    cid = _lock_client_id(payload)
    if not cid:
        raise HTTPException(400, "client_id fehlt oder ist ungueltig")
    res = _lock_release(room, cid)
    res.update({"ok": True, "room": room})
    if res.get("released"):
        await _broadcast_host_status(room)
    return res


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


def _apply_trigger(room: str, action: str, issued_at: int | None = None,
                   force: bool = False) -> dict:
    """Fuehrt start/stop/clear aus -- idempotent und mit Start-Gate.

    Idempotenz: Jeder Trigger wird ueber (action, session, issued_at) im Raum
    vermerkt; eine wiederholte Zustellung desselben Tripels aendert nichts.
    Zusaetzlich zustandsbezogen: ein zweites "start" bei laufender Aufnahme
    erzeugt KEINE neue session_id, ein "stop" ohne Aufnahme bleibt folgenlos.
    """
    now_ms = int(time.time() * 1000)
    issued = int(issued_at or now_ms)

    if action not in ("start", "stop", "clear"):
        return {"ok": False, "blocked": True, "duplicate": False, "reason": "bad_action",
                "detail": "action muss start, stop oder clear sein", "command": None}

    if action == "start" and not force:
        gate = _start_gate(room)
        if not gate["ok"]:
            with _LOCK:
                cmd = dict(_room(room)["command"])
            return {"ok": False, "blocked": True, "duplicate": False,
                    "reason": gate["reason"], "detail": gate["detail"], "command": cmd}

    with _LOCK:
        r = _room(room)
        state = r.get("rec_state", "idle")
        cur_session = r.get("rec_session", "")

        # Zustandsbezogene Idempotenz.
        if action == "start" and state == "recording":
            return {"ok": True, "duplicate": True, "blocked": False,
                    "reason": "already_recording", "detail": "Aufnahme laeuft bereits.",
                    "command": dict(r["command"])}
        if action == "stop" and state != "recording":
            return {"ok": True, "duplicate": True, "blocked": False,
                    "reason": "not_recording", "detail": "Es laeuft keine Aufnahme.",
                    "command": dict(r["command"])}

        # Trigger-Dedupe ueber (action, session, issued_at).
        dedupe_session = cur_session if action in ("stop", "clear") else ""
        if not _cmd_remember(r, action, dedupe_session, issued):
            return {"ok": True, "duplicate": True, "blocked": False, "reason": "duplicate",
                    "detail": "Befehl bereits verarbeitet.", "command": dict(r["command"])}

        if action == "start":
            sid = _new_session_id()
            r["command"] = {"action": "start",
                            "start_at": now_ms + int(START_LEAD_SECONDS * 1000),
                            "session": sid, "issued_at": now_ms}
            r["rec_started_at"] = now_ms + int(START_LEAD_SECONDS * 1000)
            r["rec_session"]    = sid
            r["rec_state"]      = "recording"
        elif action == "stop":
            r["command"] = {"action": "stop", "start_at": None,
                            "session": cur_session, "issued_at": now_ms}
            r["rec_state"] = "idle"
            # rec_session bleibt bestehen: Marker und Clip-Events der gerade
            # beendeten Aufnahme brauchen die Session-Bindung weiterhin.
        else:  # clear
            r["command"] = {"action": None, "start_at": None,
                            "session": None, "issued_at": now_ms}
            r["rec_state"] = "idle"
        cmd = dict(r["command"])

    return {"ok": True, "duplicate": False, "blocked": False, "reason": "",
            "detail": "", "command": cmd}


@app.get("/host/start_check/{room}")
def host_start_check(room, _auth=Depends(require_auth)):
    """Vorabpruefung fuer den Host-Startknopf (Gaeste online/bereit, Geraete)."""
    check_ident(room)
    return {"ok": True, "room": room, "gate": _start_gate(room)}


@app.post("/host/trigger/{room}")
async def host_trigger(room, request: Request, _auth=Depends(require_auth)):
    check_ident(room)
    _room_register(room)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    action = str(payload.get("action") or "")
    try:
        issued_at = int(payload.get("issued_at") or 0) or None
    except (TypeError, ValueError):
        issued_at = None
    force  = bool(payload.get("force"))
    now_ms = int(time.time() * 1000)

    # Roadmap 5: Nur die steuernde Host-Instanz darf Aufnahmen starten/stoppen.
    cid = _lock_client_id(payload)
    if not _lock_holds(room, cid):
        cur = _lock_get(room)
        return JSONResponse(status_code=409, content={
            "ok": False, "reason": "readonly",
            "detail": "Dieser Raum wird bereits von einer anderen Host-Instanz gesteuert.",
            "mode": "readonly", "lock": _lock_public(cur), "server_time": now_ms})

    res = _apply_trigger(room, action, issued_at, force)
    if res.get("reason") == "bad_action":
        raise HTTPException(400, res["detail"])
    if res.get("blocked"):
        return JSONResponse(status_code=409, content={
            "ok": False, "reason": res["reason"], "detail": res["detail"],
            "command": res.get("command"), "server_time": now_ms})
    if not res.get("duplicate"):
        # Echtzeit-Push: Gaeste bekommen den Befehl sofort, Host-Panels den Status.
        await _broadcast_guests(room)
        await _broadcast_host_status(room)
    return {"ok": True, "command": res["command"],
            "duplicate": res.get("duplicate", False),
            "reason": res.get("reason", ""), "server_time": now_ms}


@app.post("/host/settings/{room}")
async def host_settings(room, request: Request, _auth=Depends(require_auth)):
    check_ident(room)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    now_ms = int(time.time() * 1000)
    # Roadmap 5: Settings sind ein Steuerbefehl -> Lock erforderlich.
    if not _lock_holds(room, _lock_client_id(payload)):
        return JSONResponse(status_code=409, content={
            "ok": False, "reason": "readonly",
            "detail": "Nur-Lesen-Modus: eine andere Host-Instanz steuert diesen Raum.",
            "mode": "readonly", "lock": _lock_public(_lock_get(room)),
            "server_time": now_ms})
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
        cur_session = r.get("rec_session", "")
    # Marker sind STRIKT an eine session_id gebunden. Der Client darf die
    # Session explizit mitschicken (z.B. Nachtrag zu einer beendeten Aufnahme);
    # ohne Session wird nichts gespeichert, damit kein Marker allein im
    # Raumzustand haengen bleibt.
    session = str(payload.get("session") or cur_session or "")[:40]
    if not session:
        raise HTTPException(409, "Kein aktiver session_id -- Marker koennen nur "
                                 "innerhalb einer Aufnahme-Session gesetzt werden.")
    if payload.get("session") and session != cur_session:
        if session not in _marker_sessions(room):
            raise HTTPException(400, "Unbekannte session_id fuer diesen Raum.")
    offset_ms = max(0, now_ms - int(started)) if session == cur_session else 0
    try:
        offset_ms = max(0, int(payload.get("offset_ms", offset_ms)))
    except (TypeError, ValueError):
        pass
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
    files_to_delete: list[Path] = []
    if "token_days" in payload:
        cfg["token_days"]     = max(1, min(365, int(payload["token_days"])))
    if "recording_days" in payload:
        cfg["recording_days"] = max(0, min(3650, int(payload["recording_days"])))
    if "chunk_hours" in payload:
        cfg["chunk_hours"]    = max(1, min(8760, int(payload["chunk_hours"])))
    if "log_days" in payload:
        cfg["log_days"]       = max(0, min(3650, int(payload["log_days"])))
    if "require_guest_online" in payload:
        cfg["require_guest_online"] = bool(payload["require_guest_online"])
    if "require_guest_ready" in payload:
        cfg["require_guest_ready"]  = bool(payload["require_guest_ready"])
    if "clip_threshold_dbfs" in payload:
        try:
            cfg["clip_threshold_dbfs"] = max(-12.0, min(0.0, float(payload["clip_threshold_dbfs"])))
        except (TypeError, ValueError):
            pass
    if "clip_min_samples" in payload:
        try:
            cfg["clip_min_samples"] = max(1, min(100, int(payload["clip_min_samples"])))
        except (TypeError, ValueError):
            pass
    if "locale" in payload:
        # _normalise_locale faellt auf DEFAULT_LOCALE zurueck, wenn keine
        # passende Datei in locale/ existiert -- ungueltige Codes koennen die
        # Oberflaeche also nicht unbrauchbar machen.
        cfg["locale"] = _normalise_locale(payload["locale"])
    if "room_locales" in payload and isinstance(payload["room_locales"], dict):
        cfg["room_locales"] = {
            str(room)[:64]: _normalise_locale(code)
            for room, code in payload["room_locales"].items()
            if SAFE.match(str(room))
        }
    if "brand_name" in payload:
        cfg["brand_name"]     = str(payload["brand_name"] or "Podcast Studio")[:60]
    if "brand_color" in payload:
        c = str(payload["brand_color"] or "").strip()
        if not c or re.match(r"^#[0-9a-fA-F]{6}$", c):
            cfg["brand_color"] = c
    if "brand_preset" in payload:
        key = str(payload["brand_preset"] or "").strip().lower()
        if key in _global_presets(cfg):
            cfg["brand_preset"] = key
            # Ein Presetwechsel bedeutet bewusst: exakt dieses Preset nutzen.
            if payload.get("reset_brand_overrides", True):
                cfg["brand_color"] = ""
                cfg["brand_bg"] = ""
                cfg["brand_text"] = ""
                cfg["brand_on_brand"] = ""
    if "global_presets" in payload and isinstance(payload["global_presets"], list):
        presets = []
        seen = set()
        for raw in payload["global_presets"][:50]:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("key") or "").strip().lower()
            if (not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", key)
                    or key in seen or key in BRAND_PRESETS):
                continue
            label = str(raw.get("label") or "").strip()[:60]
            if not label:
                continue
            def global_col(name, fallback):
                val = str(raw.get(name) or "").strip()
                return val if _hex_parse(val) else fallback
            presets.append({
                "key": key, "label": label,
                "version": max(1, int(raw.get("version") or 1)),
                "appearance": "light" if raw.get("appearance") == "light" else "dark",
                "brand": global_col("brand", "#30a46c"),
                "bg": global_col("bg", "#0f1115"),
                "text": global_col("text", "#e8eaed"),
                "on_brand": global_col("on_brand", "#ffffff"),
            })
            seen.add(key)
        old_custom_keys = {str(p.get("key")) for p in cfg.get("global_presets") or []
                           if isinstance(p, dict)}
        new_custom_keys = {p["key"] for p in presets}
        removed_keys = old_custom_keys - new_custom_keys
        per_preset_assets = dict(cfg.get("global_preset_assets") or {})
        for removed_key in removed_keys:
            for meta in (per_preset_assets.pop(removed_key, {}) or {}).values():
                if isinstance(meta, dict) and SAFE_FILE.match(str(meta.get("file") or "")):
                    files_to_delete.append(BRANDING_DIR / str(meta["file"]))
        cfg["global_preset_assets"] = per_preset_assets
        cfg["global_presets"] = presets
        if cfg.get("brand_preset") not in _global_presets(cfg):
            cfg["brand_preset"] = DEFAULT_PRESET
            cfg["brand_color"] = ""
            cfg["brand_bg"] = ""
            cfg["brand_text"] = ""
            cfg["brand_on_brand"] = ""
    # Leerer String setzt einen Override bewusst zurueck auf den Preset-Wert.
    for key in ("brand_bg", "brand_text", "brand_on_brand"):
        if key in payload:
            val = str(payload[key] or "").strip()
            if not val:
                cfg[key] = ""
            elif re.match(r"^#[0-9a-fA-F]{6}$", val):
                cfg[key] = val
    if "branding_presets" in payload and isinstance(payload["branding_presets"], list):
        old_room_presets = [p for p in cfg.get("branding_presets") or []
                            if isinstance(p, dict)]
        presets = []
        seen = set()
        for raw in payload["branding_presets"][:50]:
            if not isinstance(raw, dict):
                continue
            pid = str(raw.get("id") or "").strip().lower()
            if not re.match(r"^[a-z0-9_-]{2,40}$", pid) or pid in seen:
                continue
            name = str(raw.get("name") or "").strip()[:60]
            if not name:
                continue
            old = next((p for p in cfg.get("branding_presets") or []
                        if isinstance(p, dict) and str(p.get("id")) == pid), {})
            def col(key, fallback):
                val = str(raw.get(key) or "").strip()
                return val if _hex_parse(val) else fallback
            presets.append({
                "id": pid, "name": name,
                "appearance": "light" if raw.get("appearance") == "light" else "dark",
                "brand_color": col("brand_color", "#30a46c"),
                "bg": col("bg", "#0f1115"),
                "text": col("text", "#e8eaed"),
                "on_brand": col("on_brand", ""),
                "logo_asset": old.get("logo_asset"),
            })
            seen.add(pid)
        valid = {p["id"] for p in presets}
        for old_preset in old_room_presets:
            if str(old_preset.get("id")) in valid:
                continue
            meta = old_preset.get("logo_asset")
            if isinstance(meta, dict) and SAFE_FILE.match(str(meta.get("file") or "")):
                files_to_delete.append(BRANDING_DIR / str(meta["file"]))
        cfg["branding_presets"] = presets
        cfg["room_preset_assignments"] = {
            r: p for r, p in (cfg.get("room_preset_assignments") or {}).items()
            if p in valid
        }
    if "brand_favicon" in payload:
        cfg["brand_favicon"]  = str(payload["brand_favicon"] or "")[:200000]
    _cfg_save(cfg)
    for path in files_to_delete:
        try:
            path.unlink()
        except OSError:
            pass
    return {"ok": True, "config": cfg, "theme": _theme_tokens(cfg)}


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


# ── Roadmap 6: Health & Diagnostics + manueller WAV-Rebuild ──────────────────

def _dir_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _upload_backlog() -> dict:
    """Findet Sessions mit Chunks, aber ohne fertige full.wav.

    Das ist der eigentliche Rueckstand: Material liegt auf der Platte, ist aber
    noch nicht zusammengefuehrt. Genau diese Sessions sind Kandidaten fuer den
    manuellen WAV-Rebuild.
    """
    pending: list[dict] = []
    total_chunks = 0
    total_bytes = 0
    if UPLOADS.exists():
        for room_dir in sorted(UPLOADS.iterdir()):
            if not room_dir.is_dir():
                continue
            for guest_dir in sorted(room_dir.iterdir()):
                if not guest_dir.is_dir():
                    continue
                for sess_dir in sorted(guest_dir.iterdir()):
                    if not sess_dir.is_dir():
                        continue
                    chunks = sorted(sess_dir.glob("chunk-*.pcm")) or \
                             sorted(sess_dir.glob("chunk-*.webm"))
                    if not chunks:
                        continue
                    wav = sess_dir / "full.wav"
                    if wav.exists() and wav.stat().st_size > 44:
                        continue
                    size = 0
                    newest = 0.0
                    for c in chunks:
                        try:
                            st = c.stat()
                            size += st.st_size
                            newest = max(newest, st.st_mtime)
                        except OSError:
                            continue
                    total_chunks += len(chunks)
                    total_bytes += size
                    pending.append({
                        "room": room_dir.name,
                        "guest": guest_dir.name,
                        "session": sess_dir.name,
                        "chunks": len(chunks),
                        "size_mb": round(size / 1024 / 1024, 2),
                        "kind": "pcm" if chunks[0].suffix == ".pcm" else "webm",
                        "last_chunk_ts": round(newest, 1),
                        "age_minutes": round(max(0.0, time.time() - newest) / 60, 1),
                    })
    pending.sort(key=lambda x: x["last_chunk_ts"], reverse=True)
    return {"sessions": pending, "count": len(pending),
            "chunks": total_chunks, "size_mb": round(total_bytes / 1024 / 1024, 2)}


def _ws_connection_stats() -> dict:
    """Zaehlt offene WebSockets pro Rolle und Raum."""
    with _WS_LOCK:
        hosts = {room: len(s) for room, s in _WS_HOSTS.items() if s}
        guests = {room: len(s) for room, s in _WS_GUESTS.items() if s}
    return {
        "host_sockets": sum(hosts.values()),
        "guest_sockets": sum(guests.values()),
        "rooms_with_hosts": len(hosts),
        "rooms_with_guests": len(guests),
        "per_room_hosts": hosts,
        "per_room_guests": guests,
    }


def _active_rooms_diag() -> list[dict]:
    """Raeume mit aktueller Aktivitaet: Host verbunden, Gaeste online, Aufnahme."""
    now_s = time.time()
    ws = _ws_connection_stats()
    rows = []
    with _LOCK:
        names = list(ROOMS.keys())
    for name in names:
        status = _build_status(name)
        guests = status.get("guests", [])
        online = [g for g in guests if g.get("connection") == "online"]
        cmd = status.get("command") or {}
        now_ms = int(now_s * 1000)
        start_at = cmd.get("start_at")
        recording = bool(cmd.get("action") == "start" and start_at
                         and now_ms >= int(start_at))
        host_sockets = ws["per_room_hosts"].get(name, 0)
        if not (host_sockets or online or recording):
            continue
        lock = _lock_get(name)
        # Summe des noch nicht hochgeladenen Materials, wie die Gaeste es melden.
        queue = sum(int(g.get("queue") or 0) for g in guests)
        rows.append({
            "room": name,
            "host_sockets": host_sockets,
            "guest_sockets": ws["per_room_guests"].get(name, 0),
            "guests_online": len(online),
            "guests_known": len(guests),
            "recording": recording,
            "session": status.get("session", ""),
            "pending_chunks": queue,
            "locked": bool(lock),
            "lock_role": (lock or {}).get("role", ""),
            "lock_connected": bool((lock or {}).get("connected")),
        })
    rows.sort(key=lambda x: (not x["recording"], x["room"].lower()))
    return rows


def _ping_stats() -> dict:
    """Latenz-/Frische-Kennzahlen aus den Gast-Heartbeats.

    Wir messen keinen eigenen RTT, sondern wie alt der letzte Heartbeat je Gast
    ist. Das beantwortet die operative Frage: reagieren die Clients noch?
    """
    now_s = time.time()
    ages: list[float] = []
    stale = 0
    with _LOCK:
        for r in ROOMS.values():
            for info in r.get("guests", {}).values():
                last = info.get("last_seen") or 0
                if not last:
                    continue
                age = now_s - last
                ages.append(age)
                if age > GUEST_STALE_AFTER:
                    stale += 1
    if not ages:
        return {"guests_measured": 0, "avg_age_s": 0.0, "max_age_s": 0.0,
                "stale_guests": 0, "stale_after_s": GUEST_STALE_AFTER}
    return {
        "guests_measured": len(ages),
        "avg_age_s": round(sum(ages) / len(ages), 1),
        "max_age_s": round(max(ages), 1),
        "stale_guests": stale,
        "stale_after_s": GUEST_STALE_AFTER,
    }


@app.get("/admin/diagnostics")
def admin_diagnostics(_role=Depends(require_admin), errors: int = 30):
    """Zustandsbericht fuer die Health-&-Diagnostics-Ansicht im Admin-Panel."""
    now_s = time.time()
    uptime = now_s - SERVER_START_TS
    counters = _diag_snapshot()
    backlog = _upload_backlog()
    ws = _ws_connection_stats()
    ping = _ping_stats()
    active = _active_rooms_diag()

    # Ableitung eines Gesamtzustands. "warn" statt "ok", sobald etwas
    # Aufmerksamkeit braucht -- der Admin soll nicht Zahlen vergleichen muessen.
    recent_errors = _errors_recent(limit=max(1, min(int(errors or 30), 100)))
    problems = []
    if backlog["count"]:
        problems.append(f"{backlog['count']} Session(s) ohne fertige WAV")
    if ping["stale_guests"]:
        problems.append(f"{ping['stale_guests']} Gast/Gaeste ohne aktuellen Heartbeat")
    if counters["upload_errors"]:
        problems.append(f"{counters['upload_errors']} Upload-Fehler")
    if counters["room_list_denied"]:
        problems.append(f"{counters['room_list_denied']} abgelehnte Raumlisten-Zugriffe")
    fresh_errors = [e for e in recent_errors if now_s - e["ts"] < 900]
    if fresh_errors:
        problems.append(f"{len(fresh_errors)} Fehler in den letzten 15 Minuten")
    state = "ok" if not problems else "warn"

    data_bytes = _dir_size_bytes(UPLOADS)
    try:
        du = shutil.disk_usage(str(DATA_DIR))
        disk = {"total_gb": round(du.total / 1024 ** 3, 1),
                "used_gb": round(du.used / 1024 ** 3, 1),
                "free_gb": round(du.free / 1024 ** 3, 1),
                "free_pct": round(du.free / du.total * 100, 1) if du.total else 0.0}
    except Exception as e:
        disk = {"error": str(e)}

    return {
        "ok": True,
        "state": state,
        "problems": problems,
        "server_time": int(now_s * 1000),
        "uptime": {"seconds": round(uptime, 1), "human": _fmt_uptime(uptime),
                   "started_at": round(SERVER_START_TS, 1)},
        "websocket": {
            "backend": "uvicorn/websockets (FastAPI WebSocket)",
            "transport": "ws/wss (kein HTTP-Poll-Fallback)",
            **ws,
        },
        "active_rooms": active,
        "active_rooms_count": len(active),
        "registered_rooms": len(_room_registry_list()),
        "ping": ping,
        "upload_backlog": backlog,
        "counters": counters,
        "room_list": {
            "last": dict(_ROOM_LIST_LAST),
            "ok": counters["room_list_ok"],
            "denied": counters["room_list_denied"],
            "unauthenticated": counters["room_list_unauthenticated"],
            "errors": counters["room_list_error"],
        },
        "storage": {
            "data_dir": str(DATA_DIR),
            "uploads_mb": round(data_bytes / 1024 / 1024, 1),
            "disk": disk,
        },
        "locks": {
            "held": len(_HOST_LOCKS),
            "ttl_s": HOST_LOCK_TTL,
            "grace_s": HOST_LOCK_GRACE,
        },
        "recent_errors": recent_errors,
    }


@app.get("/admin/diagnostics/logs")
def admin_diagnostics_logs(_role=Depends(require_admin),
                           since: float = 0.0, limit: int = 300):
    """Client-Logs aller Raeume, jung zuerst -- fuer die Diagnose-Ansicht.

    Die raumbezogene Ansicht (/admin/room/{room}/logs) bleibt unveraendert;
    hier geht es um den instanzweiten Blick.
    """
    limit = max(1, min(int(limit or 300), 2000))
    rows: list[dict] = []
    try:
        with _DB_LOCK, _db_conn() as conn:
            cur = conn.execute(
                "SELECT room, guest, session, ts, level, msg FROM guest_logs "
                "WHERE ts > ? ORDER BY ts DESC LIMIT ?", (since, limit))
            rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        _error_record("diagnostics", "Client-Logs konnten nicht gelesen werden",
                      detail=str(e))
        raise HTTPException(500, "Client-Logs konnten nicht gelesen werden")
    counts = {"info": 0, "ok": 0, "err": 0}
    for r in rows:
        lvl = str(r.get("level") or "info")
        counts[lvl] = counts.get(lvl, 0) + 1
    return {"ok": True, "lines": rows, "count": len(rows), "levels": counts,
            "newest_ts": rows[0]["ts"] if rows else since}


@app.post("/admin/rebuild-wav/{room}/{guest}/{session}")
async def admin_rebuild_wav(room, guest, session, request: Request,
                            _role=Depends(require_admin)):
    """Baut `full.wav` aus den vorhandenen Chunks neu.

    Anwendungsfall: der Gast hat die Verbindung verloren, bevor `/finish` lief,
    oder die Zusammenfuehrung ist fehlgeschlagen. Die Chunks liegen aber noch
    auf der Platte.

    Body (optional): { "force": bool }
      force=false (Standard): eine vorhandene, gueltige WAV wird NICHT
      ueberschrieben -- der Aufruf meldet stattdessen 409.
    """
    check_ident(room, guest, session)
    dest_dir = safe(room, guest, session)
    if not dest_dir.exists():
        raise HTTPException(404, "Session nicht gefunden")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    force = bool(payload.get("force"))

    wav_path = dest_dir / "full.wav"
    existed = wav_path.exists() and wav_path.stat().st_size > 44
    if existed and not force:
        raise HTTPException(409, "Es existiert bereits eine WAV. "
                                 "Zum Ueberschreiben force=true senden.")

    pcm_chunks = sorted(dest_dir.glob("chunk-*.pcm"))
    webm_chunks = sorted(dest_dir.glob("chunk-*.webm"))
    if not pcm_chunks and not webm_chunks:
        raise HTTPException(404, "Keine Chunks vorhanden -- Neuaufbau nicht moeglich")

    # Vor dem Ueberschreiben sichern, damit ein Fehlschlag nichts vernichtet.
    backup = None
    if existed:
        backup = dest_dir / "full.wav.bak"
        try:
            shutil.copy2(wav_path, backup)
        except OSError as e:
            _error_record("rebuild", "Sicherungskopie fehlgeschlagen",
                          room=room, detail=str(e))
            backup = None

    try:
        if pcm_chunks:
            sample_rate, channels = DEFAULT_SAMPLE_RATE, DEFAULT_CHANNELS
            meta_file = dest_dir / "meta.json"
            if meta_file.exists():
                try:
                    m = json.loads(meta_file.read_text())
                    sample_rate = int(m.get("sample_rate", sample_rate))
                    channels = int(m.get("channels", channels))
                except Exception:
                    pass
            wav_path = _write_wav_from_pcm(pcm_chunks, dest_dir, sample_rate, channels)
            n_chunks, kind = len(pcm_chunks), "pcm"
        else:
            wav_path, tmp_webm = _transcode_webm_to_wav(webm_chunks, dest_dir)
            n_chunks, kind = len(webm_chunks), "webm"
            try:
                _maybe_make_mp4(tmp_webm, dest_dir)
            except Exception as e:
                _error_record("rebuild", "MP4-Fallback fehlgeschlagen",
                              room=room, detail=str(e))
            try:
                tmp_webm.unlink()
            except OSError:
                pass
    except HTTPException:
        # Rueckrollen: der alte Stand ist besser als eine kaputte Datei.
        if backup and backup.exists():
            try:
                shutil.move(str(backup), str(wav_path))
            except OSError:
                pass
        _diag_bump("finish_errors")
        _error_record("rebuild", "WAV-Neuaufbau fehlgeschlagen",
                      room=room, detail=f"{guest}/{session}")
        raise
    except Exception as e:
        if backup and backup.exists():
            try:
                shutil.move(str(backup), str(wav_path))
            except OSError:
                pass
        _diag_bump("finish_errors")
        _error_record("rebuild", "WAV-Neuaufbau fehlgeschlagen",
                      room=room, detail=f"{guest}/{session}: {e}")
        raise HTTPException(500, f"WAV-Neuaufbau fehlgeschlagen: {e}")

    # Marker dieser Session wieder einbetten.
    try:
        _wav_add_markers(wav_path, _marker_list(room, session))
    except Exception as e:
        _error_record("rebuild", "Marker konnten nicht eingebettet werden",
                      room=room, detail=str(e))

    if backup and backup.exists():
        try:
            backup.unlink()
        except OSError:
            pass

    with _LOCK:
        r = ROOMS.get(room)
        if r and guest in r.get("guests", {}):
            r["guests"][guest]["state"] = "done"
            r["guests"][guest]["queue"] = 0

    try:
        await _broadcast_host_status(room)
    except Exception:
        pass

    # Session-Mixdown neu erzeugen, damit die Vorschau zur neuen Spur passt.
    mixdown = None
    try:
        mixdown = _ensure_session_mixdown(room, session, force=True)
    except Exception as e:
        _error_record("rebuild", "Mixdown-Aktualisierung fehlgeschlagen",
                      room=room, detail=str(e))

    _diag_bump("wav_rebuilds")
    size_mb = round(wav_path.stat().st_size / 1024 / 1024, 2)
    _error_record("rebuild", f"WAV neu aufgebaut ({n_chunks} Chunks, {size_mb} MB)",
                  room=room, detail=f"{guest}/{session}")
    return {"ok": True, "room": room, "guest": guest, "session": session,
            "chunks": n_chunks, "kind": kind, "replaced": existed,
            "size_mb": size_mb,
            "mixdown": f"/host/mixdown/{room}/{session}" if mixdown else None}


# ── Raum-Management: Liste / Archivieren / Loeschen (Feature 6) ───────────────

def _rooms_overview(include_archived: bool = True) -> list[dict]:
    """Raumuebersicht mit Statistik, Archiv-Flag, Live-Status und Lock-Zustand.

    Gemeinsame Basis fuer die Host-Route (/rooms) und die Admin-Route
    (/admin/rooms). Die Daten sind identisch; nur die Verwaltungsaktionen
    (archivieren, loeschen, Downloads) bleiben Admin-only.
    """
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
        # Roadmap 5: Lock-Zustand fuer die Uebersicht.
        lock = _lock_get(name)
        info["locked"] = bool(lock)
        info["lock"] = _lock_public(lock) if lock else None
        info["lock_role"] = (lock or {}).get("role", "")
        info["lock_label"] = (lock or {}).get("label", "")
        # Ein Lock ohne offene Verbindung laeuft in der Kulanzzeit aus.
        info["lock_reconnecting"] = bool(lock and not lock.get("connected"))
        out.append(info)
    out.sort(key=lambda x: x["room"].lower())
    if not include_archived:
        out = [r for r in out if not r.get("archived")]
    return out


@app.get("/rooms/{room}/branding")
def room_branding_context(room: str, _role=Depends(require_auth)):
    check_ident(room)
    return {"ok": True, "room": room, "preset": _room_preset(room)}


@app.post("/rooms")
async def rooms_create(request: Request, _role=Depends(require_auth)):
    """Legt einen Raum an und weist das vom Host gewaehlte Gast-Preset zu."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    room = str(payload.get("room") or "").strip()
    check_ident(room)
    preset_id = str(payload.get("preset_id") or "").strip()
    cfg = _cfg_load()
    valid = {str(p.get("id")) for p in cfg.get("branding_presets") or [] if isinstance(p, dict)}
    if preset_id and preset_id not in valid:
        raise HTTPException(400, "Unbekanntes Branding-Preset")
    assignments = dict(cfg.get("room_preset_assignments") or {})
    if preset_id:
        assignments[room] = preset_id
    else:
        assignments.pop(room, None)
    cfg["room_preset_assignments"] = assignments
    _cfg_save(cfg)
    _room_register(room)
    return {"ok": True, "room": room, "preset_id": preset_id,
            "branding": _room_preset(room, cfg)}


@app.get("/rooms")
def rooms_list(role=Depends(require_auth)):
    """Raumuebersicht fuer authentifizierte Hosts UND Admins.

    Hosts brauchen die Liste, um bestehende Raeume zu finden und zu oeffnen,
    ohne den Raumnamen manuell zu tippen. Archivierte Raeume liefern wir nur
    an Admins aus; fuer Hosts ist die Liste reine Navigation.
    """
    is_admin = (role == "admin")
    try:
        rooms = _rooms_overview(include_archived=is_admin)
    except Exception as e:
        _diag_bump("room_list_error")
        _error_record("room-list", "Raumliste konnte nicht erstellt werden", detail=str(e))
        raise HTTPException(500, "Raumliste konnte nicht erstellt werden")
    _diag_bump("room_list_ok")
    _ROOM_LIST_LAST.update({"ts": time.time(), "role": role, "status": 200,
                            "count": len(rooms), "route": "/rooms"})
    return {"ok": True, "role": role, "is_admin": is_admin, "rooms": rooms}


@app.get("/admin/rooms")
def admin_rooms(_role=Depends(require_admin)):
    """Alle Raeume mit Statistik + Archiv-Flag (Admin-Sicht)."""
    try:
        rooms = _rooms_overview(include_archived=True)
    except Exception as e:
        _diag_bump("room_list_error")
        _error_record("room-list", "Raumliste konnte nicht erstellt werden", detail=str(e))
        raise HTTPException(500, "Raumliste konnte nicht erstellt werden")
    _diag_bump("room_list_ok")
    _ROOM_LIST_LAST.update({"ts": time.time(), "role": "admin", "status": 200,
                            "count": len(rooms), "route": "/admin/rooms"})
    return {"ok": True, "role": "admin", "is_admin": True, "rooms": rooms}


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
        conn.execute("DELETE FROM clip_events WHERE room=?", (room,))
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
                "guest", "client_id", "display_name", "session", "state",
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

@app.get("/admin/room/{room}/clips")
def admin_room_clips(room, session: str | None = None, since: float = 0.0,
                     _role=Depends(require_admin)):
    """Persistierte Clipping-Ereignisse eines Raums, optional je Session."""
    check_ident(room)
    rows = _clip_events_query(room, session=session, since=since)
    groups: dict[str, dict] = {}
    for r in rows:
        g = str(r.get("guest") or "")
        grp = groups.setdefault(g, {"guest": g, "count": 0, "worst_dbfs": -120.0,
                                    "last_ts": 0.0, "events": []})
        grp["count"] += 1
        grp["worst_dbfs"] = max(grp["worst_dbfs"], float(r.get("peak_dbfs") or -120.0))
        grp["last_ts"] = max(grp["last_ts"], float(r.get("ts") or 0.0))
        grp["events"].append({
            "ts": r.get("ts"), "session": r.get("session"),
            "offset_ms": r.get("offset_ms"), "peak_dbfs": r.get("peak_dbfs"),
            "samples": r.get("samples"), "duration_ms": r.get("duration_ms"),
        })
    out = sorted(groups.values(), key=lambda x: x["last_ts"], reverse=True)
    return {"ok": True, "room": room, "session": session or "",
            "server_time": time.time(), "guests": out,
            "total": sum(g["count"] for g in out)}


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


# ---------------------------------------------------------------------------
# Punkt 8: Lobby-Presence -- Gaeste erscheinen vor der Namenseingabe
# ---------------------------------------------------------------------------
# Bisher tauchte ein Gast erst im Host-Panel auf, nachdem er seinen Namen
# bestaetigt hatte. Der Host sah also nicht, dass jemand schon wartet.
# Jetzt meldet der Recorder direkt nach dem Aufloesen des Tokens eine Lobby-
# Praesenz. Die Identitaet kommt aus einer pro Recorder-Dokument
# erzeugten client_id -- getrennte Dokumente teilen keine Lobby-Identitaet.

LOBBY_FORGET_AFTER = 45.0   # Sekunden ohne Lobby-Ping -> Eintrag verfaellt


def _lobby_touch(room: str, client_id: str, stage: str = "naming") -> dict:
    """Legt eine Lobby-Praesenz an oder haelt sie frisch."""
    now = time.time()
    with _LOCK:
        r = _room(room)
        lobby = r.setdefault("lobby", {})
        retired = r.setdefault("lobby_retired", {})
        for cid, until in list(retired.items()):
            if until <= now:
                retired.pop(cid, None)
        # A delayed ping must never undo a leave or an authenticated join.
        if client_id in retired or any(
            g.get("client_id") == client_id for g in r["guests"].values()
        ):
            lobby.pop(client_id, None)
            return {}
        entry = lobby.get(client_id)
        if entry is None:
            # Kurzes, gut vorlesbares Kuerzel: der Host kann so ueber
            # "Gast A" / "Gast B" sprechen, bevor Namen existieren.
            used = {e.get("label") for e in lobby.values()}
            idx = 0
            while _lobby_label(idx) in used:
                idx += 1
            entry = {
                "client_id": client_id,
                "label": _lobby_label(idx),
                "joined_at": now,
            }
            lobby[client_id] = entry
        entry["stage"] = stage if stage in ("joining", "naming", "named") else "naming"
        entry["last_seen"] = now
        return dict(entry)


def _lobby_label(index: int) -> str:
    """A, B, ... Z, AA, AB ... -- stabil und ohne Zaehlerluecken im UI."""
    label = ""
    n = index
    while True:
        label = chr(ord("A") + (n % 26)) + label
        n = n // 26 - 1
        if n < 0:
            break
    return label


def _lobby_retire(room_obj: dict, client_id: str, now: float) -> None:
    retired = room_obj.setdefault("lobby_retired", {})
    for cid, until in list(retired.items()):
        if until <= now:
            retired.pop(cid, None)
    retired[client_id] = now + LOBBY_FORGET_AFTER * 2
    # Bound memory even for many short-lived invitation visits.
    while len(retired) > 4096:
        retired.pop(next(iter(retired)))


def _lobby_drop(room: str, client_id: str) -> None:
    with _LOCK:
        r = _room(room)
        r.get("lobby", {}).pop(client_id, None)
        _lobby_retire(r, client_id, time.time())


def _lobby_list(room_obj: dict, now: float | None = None) -> list[dict]:
    """Aktive Lobby-Eintraege; abgelaufene werden dabei entfernt."""
    now = time.time() if now is None else now
    lobby = room_obj.setdefault("lobby", {})
    joined = {g.get("client_id") for g in room_obj.get("guests", {}).values()}
    retired = room_obj.setdefault("lobby_retired", {})
    for cid, until in list(retired.items()):
        if until <= now:
            retired.pop(cid, None)
    stale = [cid for cid, e in lobby.items()
             if cid in joined or cid in retired
             or now - e.get("last_seen", 0) > LOBBY_FORGET_AFTER]
    for cid in stale:
        lobby.pop(cid, None)
    rows = []
    for e in lobby.values():
        rows.append({
            "client_id": e["client_id"],
            "label": e["label"],
            "stage": e.get("stage", "naming"),
            "waiting_seconds": round(now - e.get("joined_at", now), 1),
            "seconds_since_seen": round(now - e.get("last_seen", now), 1),
        })
    rows.sort(key=lambda x: x["label"])
    return rows


def _apply_guest_telemetry(room: str, guest: str, payload: dict) -> None:
    """Apply an authenticated WebSocket guest heartbeat to ROOMS."""
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
        client_id = str(payload.get("client_id") or "")[:64]
        if client_id and SAFE.fullmatch(client_id):
            g["client_id"] = client_id
            r.setdefault("lobby", {}).pop(client_id, None)
            _lobby_retire(r, client_id, now_s)
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
        # --- Guardrails: Berechtigungen, Kamera, Bereitschaft ---------------
        perms_in = payload.get("permissions")
        if isinstance(perms_in, dict):
            g["permissions"] = {
                "microphone": str(perms_in.get("microphone", "unknown"))[:10],
                "camera":     str(perms_in.get("camera", "unknown"))[:10],
            }
        if "cam_active" in payload:
            g["cam_active"] = bool(payload.get("cam_active"))
        if "audio_only" in payload:
            g["audio_only"] = bool(payload.get("audio_only"))
        if "declared_ready" in payload:
            g["declared_ready"] = bool(payload.get("declared_ready"))
        if "peak" in payload:
            try:
                g["peak"] = float(payload.get("peak") or 0.0)
            except (TypeError, ValueError):
                pass
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
                        "guardrails": {
                            "require_guest_ready": bool(_cfg_get("require_guest_ready")),
                            "clip_threshold_dbfs": float(_cfg_get("clip_threshold_dbfs")),
                            "clip_min_samples":    int(_cfg_get("clip_min_samples")),
                        },
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
            elif mtype == "ready":
                # Gast meldet sich aktiv bereit (oder widerruft die Meldung).
                ready = bool(data.get("ready"))
                with _LOCK:
                    gi = _room(room)["guests"].get(guest)
                    if gi is not None:
                        gi["declared_ready"] = ready
                        gi["last_seen"] = time.time()
                await _broadcast_host_status(room)
            elif mtype == "clip":
                # Clipping-Ereignis: an die Session gebunden persistieren und
                # den Host sofort informieren.
                now_s = time.time()
                try:
                    peak_dbfs = float(data.get("peak_dbfs", 0.0) or 0.0)
                except (TypeError, ValueError):
                    peak_dbfs = 0.0
                try:
                    samples = int(data.get("samples", 0) or 0)
                except (TypeError, ValueError):
                    samples = 0
                try:
                    duration_ms = max(0, int(data.get("duration_ms", 0) or 0))
                except (TypeError, ValueError):
                    duration_ms = 0
                with _LOCK:
                    r = _room(room)
                    session = str(data.get("session") or r.get("rec_session", ""))[:40]
                    started = r.get("rec_started_at") or int(now_s * 1000)
                    gi = r["guests"].get(guest)
                    if gi is not None:
                        gi["clipping"] = True
                        gi["clip_count"] = int(gi.get("clip_count", 0) or 0) + 1
                        gi["clip_last_dbfs"] = peak_dbfs
                        gi["clip_last_ts"] = now_s
                        gi["last_seen"] = now_s
                        display = gi.get("display_name") or guest
                    else:
                        display = guest
                offset_ms = max(0, int(now_s * 1000) - int(started))
                if session:
                    _clip_event_store(room, guest, session, now_s, offset_ms,
                                      peak_dbfs, samples, duration_ms)
                # Zusaetzlich in die Gast-Logs, damit das Ereignis in der
                # bestehenden Admin-Logansicht ohne Extra-Klick sichtbar ist.
                _guest_logs_store(room, guest, session, [{
                    "ts": now_s, "level": "err",
                    "msg": f"Clipping: {display} uebersteuert "
                           f"({peak_dbfs:.1f} dBFS, {duration_ms} ms)",
                }])
                await _broadcast_host_status(room)
            elif mtype == "clip_clear":
                with _LOCK:
                    gi = _room(room)["guests"].get(guest)
                    if gi is not None:
                        gi["clipping"] = False
                await _broadcast_host_status(room)
            elif mtype == "ping":
                await _ws_send(websocket, {"type": "pong",
                                          "server_time": int(time.time() * 1000)})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        _error_record("ws-guest", "Gast-WebSocket-Fehler", room=room, detail=str(e))
    finally:
        _ws_remove(_WS_GUESTS, room, websocket)


@app.websocket("/ws/host/{room}")
async def ws_host(websocket: WebSocket, room: str,
                  ps_session: str | None = Cookie(default=None)):
    # Host-/Admin-Auth ueber das Session-Cookie.
    if not SAFE.match(room) or _session_role(ps_session or "") is None:
        await websocket.close(code=4401)
        return
    role = _session_role(ps_session or "") or "host"
    await websocket.accept()
    _room_register(room)
    # Mark host presence for room overview (index + admin rooms list)
    with _LOCK:
        r = _room(room)
        r["host_online"] = True
        r["host_last_seen"] = time.time()
    _ws_add(_WS_HOSTS, room, websocket)

    # Roadmap 5: Lock-Zustand dieser Verbindung. Die Client-Kennung kommt mit
    # der ersten "hello"-Nachricht; bis dahin ist die Verbindung read-only.
    client_id = ""
    mode = "readonly"

    async def send_lock_state(extra: dict | None = None) -> None:
        lock = _lock_get(room)
        msg = {"type": "lock", "room": room, "mode": mode,
               "mine": bool(client_id and lock and lock["client_id"] == client_id),
               "locked": bool(lock), "lock": _lock_public(lock),
               "client_id": client_id,
               "server_time": int(time.time() * 1000)}
        if extra:
            msg.update(extra)
        await _ws_send(websocket, msg)

    try:
        # Initialer Status-Push.
        await _ws_send(websocket, {**_build_status(room), "type": "status"})
        await send_lock_state({"reason": "awaiting_hello"})
        while True:
            data = await websocket.receive_json()
            mtype = data.get("type")
            now_ms = int(time.time() * 1000)

            # ── Lock-Protokoll ────────────────────────────────────────────
            if mtype == "hello":
                cid = _lock_client_id(data)
                if not cid:
                    await send_lock_state({"reason": "bad_client_id"})
                    continue
                client_id = cid
                res = _lock_acquire(room, cid, role=role,
                                    label=str(data.get("label") or "")[:80],
                                    force=bool(data.get("force")) and role == "admin")
                mode = res["mode"]
                await send_lock_state({"reason": res["reason"],
                                       "takeover": res.get("takeover", False)})
                await _broadcast_host_status(room)
                continue
            if mtype == "lock_renew":
                if not client_id:
                    await send_lock_state({"reason": "no_client_id"})
                    continue
                res = _lock_renew(room, client_id)
                if res["mode"] == "none":
                    # Lock ist weg (Serverneustart oder stale) -> neu erwerben.
                    res = _lock_acquire(room, client_id, role=role)
                mode = res["mode"] if res["mode"] != "none" else "readonly"
                await send_lock_state({"reason": res["reason"]})
                continue
            if mtype == "lock_acquire":
                if not client_id:
                    client_id = _lock_client_id(data)
                if not client_id:
                    await send_lock_state({"reason": "no_client_id"})
                    continue
                res = _lock_acquire(room, client_id, role=role,
                                    label=str(data.get("label") or "")[:80],
                                    force=bool(data.get("force")) and role == "admin")
                mode = res["mode"]
                await send_lock_state({"reason": res["reason"],
                                       "takeover": res.get("takeover", False)})
                await _broadcast_host_status(room)
                continue
            if mtype == "lock_release":
                if client_id:
                    _lock_release(room, client_id)
                mode = "readonly"
                await send_lock_state({"reason": "released"})
                await _broadcast_host_status(room)
                continue

            # ── Steuerbefehle: nur mit gueltigem Lock ─────────────────────
            if mtype in LOCK_GUARDED_ACTIONS:
                if not client_id or not _lock_holds(room, client_id):
                    mode = "readonly"
                    await send_lock_state({"reason": "denied_readonly",
                                           "denied_action": mtype})
                    continue
                mode = "control"

            if mtype == "trigger":
                action = str(data.get("action") or "")
                try:
                    issued_at = int(data.get("issued_at") or 0) or None
                except (TypeError, ValueError):
                    issued_at = None
                res = _apply_trigger(room, action, issued_at, bool(data.get("force")))
                if res.get("blocked"):
                    await _ws_send(websocket, {
                        "type": "trigger_result", "ok": False, "action": action,
                        "reason": res.get("reason", ""), "detail": res.get("detail", ""),
                        "server_time": now_ms})
                else:
                    await _ws_send(websocket, {
                        "type": "trigger_result", "ok": True, "action": action,
                        "duplicate": bool(res.get("duplicate")),
                        "reason": res.get("reason", ""),
                        "command": res.get("command"), "server_time": now_ms})
                    if not res.get("duplicate"):
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
                        cur_session = r.get("rec_session", "")
                    session = str(data.get("session") or cur_session or "")[:40]
                    if not session:
                        # Ohne session_id kein Marker -- der Raumzustand allein
                        # ist keine gueltige Bindung.
                        await _ws_send(websocket, {
                            "type": "marker_result", "ok": False, "reason": "no_session",
                            "detail": "Marker brauchen eine laufende Aufnahme-Session.",
                            "server_time": now_ms})
                    else:
                        offset_ms = max(0, now_ms - int(started)) if session == cur_session else 0
                        m = _marker_create(room, session, kind, offset_ms, note)
                        await _ws_send(websocket, {"type": "marker_result", "ok": True,
                                                   "marker": m, "server_time": now_ms})
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
                # Der Host-Ping erneuert gleichzeitig den Lock (Heartbeat).
                if client_id:
                    res = _lock_renew(room, client_id)
                    if res["mode"] == "none":
                        res = _lock_acquire(room, client_id, role=role)
                    new_mode = res["mode"] if res["mode"] != "none" else "readonly"
                    if new_mode != mode:
                        mode = new_mode
                        await send_lock_state({"reason": res["reason"]})
                    else:
                        mode = new_mode
                await _ws_send(websocket, {"type": "pong", "server_time": now_ms,
                                          "mode": mode})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        _error_record("ws-host", "Host-WebSocket-Fehler", room=room, detail=str(e))
    finally:
        _ws_remove(_WS_HOSTS, room, websocket)
        # Roadmap 5: Der Lock bleibt fuer HOST_LOCK_GRACE reserviert, damit ein
        # Reload oder ein kurzer Netzausfall die Steuerung zurueckbekommt.
        if client_id:
            _lock_mark_disconnected(room, client_id)
        # Unmark host presence only when no other host socket remains.
        remaining = bool(_ws_targets(_WS_HOSTS, room))
        with _LOCK:
            r = _room(room)
            r["host_online"] = remaining
            r["host_last_seen"] = time.time()
        try:
            await _broadcast_host_status(room)
        except Exception:
            pass


# ── Static Uploads ─────────────────────────────────────────────────────────
@app.get("/uploads/{asset_path:path}")
def protected_upload(asset_path: str, _auth=Depends(require_auth)):
    base = UPLOADS.resolve()
    path = (base / asset_path).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(path, headers={"Cache-Control": "private, no-store"})


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
                    # Clipping-Ereignisse folgen derselben Aufbewahrungsfrist.
                    conn.execute("DELETE FROM clip_events WHERE ts < ?", (cutoff,))
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

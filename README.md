# Open Podcast Studio

Open Podcast Studio is a lightweight, self‑hosted web app (browser UI + FastAPI server) for **remote podcast recording with clean, per‑guest tracks**.

A host controls the session (start/stop + markers) in real time. Each guest records **locally in their browser** and uploads audio in chunks. The server assembles each guest/session into a final **WAV** (and optionally **MP4** when video is enabled).

## What it does

- **One room, multiple guests**: invite guests into a shared room/session
- **Local recording, separate tracks**: each guest records locally → better quality than a mixed call
- **Host‑controlled sync**: guests follow host start/stop commands via WebSockets
- **Chunked uploads**: resilient uploads during recording
- **Exports**: download individual tracks or a ZIP for the whole room
- **Markers**: drop markers during recording (e.g. `ad`, `cut_in`, `cut_out`) and write them into the WAV
- **Tokens + roles**:
  - `admin`: admin panel + host studio
  - `host`: host studio (no admin panel)

## Architecture (high level)

- **Backend**: `server.py` (FastAPI)
  - authentication (signed session cookie)
  - room/session control + WebSockets
  - upload/finish pipeline + downloads
  - admin functions
  - persistence via files (`config.json`, `auth.json`) and SQLite (`tokens.db`)
- **UI pages**
  - `login.html`: sign in (admin / host)
  - `index.html`: room entry + admin link (admin only)
  - `host.html`: host studio
  - `recorder.html`: guest recorder (token-based)
  - `admin.html`: configuration, rooms, tokens, recordings

## Quickstart

### 1) Requirements

- **Python 3.10+** (recommended)

### 2) Run the server

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate

pip install -r requirements.txt
python server.py
```

Then open:

- `http://localhost:8000/` (UI)

> If your repo does not include a `requirements.txt` yet, you can still run it by installing the dependencies used in `server.py` (FastAPI + ASGI server). Add a proper `requirements.txt` for reproducible installs.

### 3) Create a room and invite guests

1. Sign in as **admin** (or **host**)
2. Open the **Host Studio**
3. Create guest **invite tokens**
4. Share the guest link:

```
/recorder.html?token=<token>
```

The token does not reveal the room name; token → room mapping lives server‑side in `tokens.db`.

## Usage notes

### Recordings on disk

Recordings are stored under:

```
uploads/<room>/<guest>/<session>/
```

You’ll typically see:

- `chunk-XXXXXX.*` (raw chunk files)
- `meta.json` (sample rate / channels)
- `full.wav` (assembled final track)
- optional `full.mp4` (when video stream is enabled and transcoding is available)

### Cleanup

Optional background cleanup can remove:

- old recordings after `recording_days`
- raw chunks after `chunk_hours`

(See your config in the admin panel / server config.)

## Project status

This is a pragmatic, small-footprint studio tool. Expect iteration.

If you’d like to contribute, open an issue describing your use case and environment (OS + browser + Python version), and include logs.

## Security

- Admin/host access is protected by a **signed session cookie**.
- Passwords are stored as **bcrypt hashes** in `auth.json`.
- Guest access is via single‑use/managed **tokens** stored in `tokens.db`.

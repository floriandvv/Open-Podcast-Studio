# Open Podcast Studio

Open Podcast Studio is a lightweight, self-hosted web application for **remote podcast recording with separate audio tracks for each guest**.

The host opens a room, invites guests through secure token links, and controls start, stop, clear, and markers in real time. Guests record locally in their browsers and upload the data in chunks. The server combines the chunks for each guest and session into a WAV track, generates a combined MP3 mixdown for the session, and can additionally produce an MP4 file for video sessions.

> **Project status:** Functional development build focused on audio recording, uploads, real-time control, and administration. Before exposing it to the public internet, review deployment, TLS, backups, monitoring, and access controls.

## Current status

- Host and guest control via WebSockets
- Token-based guest invitations without exposing the room name in the token
- Local browser recording with chunked uploads and server-side WAV merging
- Host-to-guest microphone control v1: device inventory, change requests, pending state, and result reporting
- Reliable repeated microphone switching with command IDs and active-track verification
- Live level display with compact level frames, VU ballistics, peak hold, and clipping indication
- Automatic recorder device updates via `devicechange`, with diff-based rendering and stable selections
- Server-side branding injected into the `<head>` without visible FOUC; semantic colors remain independent
- Host guest states: `Connected`, `Connection problems`, and `Offline`
- Admin dashboard with recording, room, and storage-usage metrics
- Session lifecycle overview with authoritative `/sessions` state fields and cleanup handling
- Admin recording history with live, complete, WAV-only, chunks-only, prepared, failed, and archived states
- Session-based MP3 mixdowns with Host-panel audio previews
- Role-separated audio access: Hosts receive MP3 only; Admins receive WAV and ZIP exports
- Admin recording history grouped by session with nested guest tracks
- Shared absolute start/stop timestamps for synchronized guest recording boundaries
- Final-track duration normalization and reliable last-chunk/upload finalization

## Features

- **One room, multiple guests:** Invite guests through token links
- **Local recording and separate tracks:** Avoids relying on a mixed call recording
- **Host-controlled synchronization:** Guests receive commands through WebSockets
- **Chunked uploads:** Keeps uploads resilient during recording
- **Session mixdowns:** Generate one MP3 mixdown per recording session for Host/Admin preview
- **Role-separated exports:** Hosts can access the MP3 mixdown only; Admins can download individual WAV tracks and per-session or full-room ZIP archives
- **Markers:** Add markers such as `ad`, `cut_in`, and `cut_out` during recording and write them into WAV files
- **Roles:**
  - `admin`: Admin panel and host studio
  - `host`: Host studio only

## Architecture

- **Backend:** `server.py` (FastAPI/Uvicorn)
  - Authentication with signed session cookies
  - Room and session control via WebSockets
  - Chunk upload, WAV merge, and session MP3 mixdown pipeline
  - Role-separated MP3 preview, WAV downloads, and ZIP exports
  - Admin functions
  - Persistence through `config.json`, `auth.json`, and SQLite (`tokens.db`)
- **Frontend pages:**
  - `login.html`: Login
  - `index.html`: Room entry point
  - `host.html`: Host studio
  - `recorder.html`: Token-based guest recorder
  - `admin.html`: Configuration, rooms, tokens, and recordings
  - `token_error.html`: Invalid or expired guest-link page

## Quickstart

### 1. Requirements

#### Runtime requirements

- **Python 3.10 or newer**
- A modern browser with support for:
  - `MediaRecorder`
  - `getUserMedia`
  - WebSockets
  - microphone and camera permissions
- **FFmpeg including `ffprobe`** available in the system `PATH`
  - required for WebM processing, MP4 transcoding, and MP3 mixdown generation
  - FFmpeg must include the `libmp3lame` encoder:

    ```bash
    ffmpeg -hide_banner -encoders | grep -i mp3
    ```

#### Python dependencies

The application uses the following packages:

| Package | Purpose | Status |
|---|---|---|
| `fastapi` | HTTP API, WebSockets, and routing | required |
| `uvicorn` | ASGI server used to run the application | required |
| `passlib[bcrypt]` | Password hashing and verification | required |
| `itsdangerous` | Signed and time-limited session cookies | required |
| `websockets` | WebSocket implementation for Uvicorn | required |
| `python-dotenv` | Loads configuration from `.env` | optional, recommended |

The other Python modules used by the application (`sqlite3`, `wave`, `zipfile`, `threading`, `subprocess`, `pathlib`, and others) are part of the Python standard library and do not need to be installed separately.

Install the dependencies in a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate

python -m pip install --upgrade pip
python -m pip install fastapi uvicorn 'passlib[bcrypt]' itsdangerous websockets python-dotenv
```

You may omit `python-dotenv` if environment variables are provided by another mechanism. Without `passlib[bcrypt]` or `itsdangerous`, the server exits during startup and reports the missing package. Video output and MP3 mixdowns additionally require the `ffmpeg` and `ffprobe` system binaries.

Alternatively, the included Docker setup installs the Python and FFmpeg dependencies in the image. Runtime data must be persisted through a volume.

### 2. Initial configuration

No manual session-secret setup is required. On first startup, the server generates a cryptographically random secret and saves it in `DATA_DIR/session_secret` (by default, beside `server.py`). It reuses this file after restarts; the file is created with owner-only permissions on POSIX. Keep `DATA_DIR` writable and persistent, including in Docker. A storage error or invalid secret file stops startup rather than silently using a temporary secret.

An explicitly configured `SESSION_SECRET` takes precedence over the generated file. Keep an existing deployment's secret unchanged to preserve its sessions. `.env` is loaded only when `python-dotenv` is installed; otherwise use actual environment variables. Never commit or publicly serve `session_secret` or `.env`.

Keep all six HTML pages beside `server.py` and put `de.json` and `en.json` in a `locale/` subdirectory. Run `python server.py`, open `http://localhost:8000/`, and sign in using **`CHANGEME!`**. No username is required. Immediately use the Admin panel to set **different passwords for admin and host** before exposing the server. Both roles initially have the same password; because the admin password is checked first, this initial login gives admin access.

The application does **not** read an `ADMIN_PASSWORD_HASH` or any other password hash from `.env`. Password hashes stored in `.env` therefore have no effect, including during initial setup.

The active bcrypt hashes are stored in `auth.json` (`admin_hash` and `host_hash`). If `auth.json` already exists and contains both hashes, it is authoritative; values in `.env` do not override it.

For a fresh installation where `auth.json` does not yet exist, the current implementation creates the initial hashes from `DEFAULT_ADMIN_PASSWORD` and `DEFAULT_HOST_PASSWORD`. If those variables are not set, it falls back to `CHANGEME!` for both roles. After `auth.json` has been created, changing or removing the `.env` password variables has no effect.

Change passwords through the Admin panel. Keep `SESSION_SECRET` unchanged between restarts, otherwise existing sessions become invalid.

### 3. Start the server

```bash
python server.py
```

The server listens on `0.0.0.0:8000` by default. Open:

- Local: `http://localhost:8000/`
- LAN: `http://<server-ip>:8000/`

### 4. Create a room and invite guests

1. Sign in as **admin** or **host**.
2. Open a room.
3. Create guest invitation tokens in the host studio.
4. Share the generated link:

```text
/recorder.html?token=<token>
```

5. Guests open the link, grant microphone/camera permissions, and select their devices.
6. The host starts and stops the recording centrally.
7. After finishing, the Host can preview the session MP3. Admins can download individual guest WAV tracks and a ZIP containing all WAVs of the session.

## Runtime data and storage

The application creates the following runtime files and directories:

- `session_secret`: automatically generated persistent session-signing secret; **do not commit it**
- `auth.json`: bcrypt hashes for the admin and host passwords
- `config.json`: runtime configuration, branding, and cleanup settings
- `tokens.db`: guest tokens, room registry, and markers
- `uploads/`: chunks, metadata, and finished recordings
- `mixdowns/`: generated session MP3 mixdowns
- `session_secret`: automatically generated persistent session-signing secret; **do not commit it**
- `.env`: local secrets and initial-setup values; **do not commit it**

Recordings are stored under:

```text
uploads/<room>/<guest>/<session>/
```

Typical files include:

```text
chunk-XXXXXX.pcm     # Audio chunk
chunk-XXXXXX.webm    # Optional WebM chunk
meta.json            # Sample rate and channel count for PCM
full.wav             # Finished individual track
full.mp4             # Optional video output
```

Cleanup threads can automatically remove old finished recordings, raw chunks, and diagnostic logs. The limits are configured in the admin panel. Archived rooms are excluded from automatic deletion.

## Security and operations

- Admin and host sessions use signed, `HttpOnly`, `SameSite=strict` cookies.
- Passwords are stored as bcrypt hashes in `auth.json`.
- Guests access rooms through cryptographically random tokens; the token does not expose the room name.
- Login attempts are rate-limited per IP.
- Room, guest, and session path segments are validated to prevent unsafe paths.
- For public or production deployment, configure **HTTPS/TLS**, a reverse proxy, secure secrets, backups, a restrictive firewall, and regular updates.
- A missing `SESSION_SECRET` is generated once and persisted in `session_secret`; it is never regenerated on ordinary restarts.
- Keep `.env`, `auth.json`, `config.json`, `tokens.db`, and `uploads/` outside a public GitHub repository unless they are explicitly required and sanitized.

## Known limitations

- Part of the room state is held in memory and is lost when the server restarts.
- Browser device access requires user permission, and device-change behavior varies between browsers.
- MP4 generation and WebM processing depend on a working FFmpeg installation and available codecs.
- The UI has German source text and an English locale with German fallback; the Admin panel can select the available locale.

## Project structure

```text
server.py        # FastAPI/Uvicorn server
login.html       # Login page
index.html       # Room entry page
host.html        # Host studio
recorder.html    # Guest recorder
admin.html       # Admin panel
token_error.html  # Invalid or expired guest link
```

## Reverse proxy deployment

For production or LAN deployment, place the application behind a reverse proxy. The proxy should terminate TLS and forward both regular HTTP requests and WebSocket connections to the Open Podcast Studio server.

The application listens on port `8000` by default:

```text
Browser  <->  HTTPS reverse proxy  <->  http://127.0.0.1:8000
```

### Nginx example

Save the following as `/etc/nginx/sites-available/open-podcast-studio` and enable it with a symlink in `/etc/nginx/sites-enabled/`.

Replace `podcast.example.com` with your hostname and adjust the certificate paths after obtaining a certificate, for example with Certbot.

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name podcast.example.com;

    # Redirect all plain HTTP traffic to HTTPS.
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name podcast.example.com;

    ssl_certificate     /etc/letsencrypt/live/podcast.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/podcast.example.com/privkey.pem;

    # Uploads can contain large audio/video chunks.
    client_max_body_size 2G;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;

        # Required for WebSocket upgrades used by the host and guest clients.
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_buffering off;
    }
}
```

Test and reload Nginx:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

Do not expose port `8000` directly to the public internet when Nginx is used. Restrict it to localhost or the internal network with a firewall.


## License

This project is licensed under the **GNU General Public License v3.0 or later** (`GPL-3.0-or-later`). See the [`LICENSE`](LICENSE) file for the complete license text.

The software is provided **as is**, without warranty of any kind. See the warranty disclaimer and limitation of liability in the license for details.


## Branding

Branding is configured in the Admin panel. The instance supports a brand name, primary color, background color, general UI text color, button text color, versioned theme presets, managed logo/favicon files, and a light-logo variant. Assets are validated, stored under `DATA_DIR/branding/`, and served through versioned URLs; image bytes are not stored as data URLs in `config.json`.

A room may optionally select a different locale without changing the global instance theme.

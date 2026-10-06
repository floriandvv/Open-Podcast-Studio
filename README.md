# Open Podcast Studio

Open Podcast Studio is a lightweight, self-hosted web application for **remote podcast recording with separate audio tracks for each guest**.

The host opens a room, invites guests through secure token links, and controls recording, markers, and optional live calls in real time. Guests record locally in their browsers and upload the data in chunks. The server combines each guest's chunks into a WAV track, generates a combined MP3 mixdown for the session, and can additionally produce an MP4 file for video sessions.

> **Project status:** Functional development build focused on audio recording, uploads, live production, optional Jitsi calls, and administration. Before exposing it to the public internet, review deployment, TLS, backups, monitoring, retention, and access controls.

## Current status

- Host and guest control via WebSockets with reconnect handling and server-authoritative state
- Token-based guest invitations without exposing the room name in the token
- Lobby presence before name confirmation or media permission, with connected, stale, offline, and cleanup states
- Optional guest readiness confirmation and host-side start guardrails
- Local browser recording with separate guest tracks, chunked uploads, server-side WAV merging, and optional MP4 output
- Configurable audio profiles for sample rate, channels, bitrate, chunk size, duration, and video parameters
- Reliable microphone and camera switching with explicit `getUserMedia()` rebinding, active-track verification, and loss recovery
- Shared media ownership between recording and live-call audio, with upload throttling while a call is active
- Audio-only recording by default plus a responsive portrait video preview with vertical VU meter and rotation controls
- Live level display with VU ballistics, peak hold, clipping indication, persisted clipping events, and configurable thresholds
- Optional Jitsi-based live audio/video calls via public or self-hosted deployment, prejoin, JWTs, mode switching, participant state, and verified remote closure
- Server-side branding without first-paint flashes; global themes, guest-only room branding presets, managed logo/favicon assets, and semantic state colors
- German and English localization with German fallback, room-level locale selection, and configurable guest consent text
- Host multi-instance locking with control handover and a read-only mode for additional Host tabs
- Admin dashboard with recording, room, storage, diagnostics, guest-log, and clipping metrics
- Session lifecycle overview with authoritative `/sessions` state fields, cleanup handling, archiving, and state filters
- Admin recording history grouped by session with nested guest tracks and live, complete, WAV-only, chunks-only, prepared, failed, and archived states
- Session-based MP3 mixdowns with Host-panel audio previews and role-separated download permissions
- Persistent recording numbers and human-readable session labels
- Shared absolute start/stop timestamps, final-track duration normalization, and reliable last-chunk/upload finalization

## Features

### Recording and live production

- **One room, multiple guests:** Invite guests through cryptographically random token links.
- **Lobby and readiness:** Guests appear in the Host lobby before joining the room; optional readiness confirmation and server-side start gates prevent premature starts.
- **Separate tracks:** Guests record locally in their browsers instead of relying on a mixed call recording.
- **Audio profiles:** Admins can create and select reusable recording profiles for PCM format, sample rate, channels, bitrate, chunk size, duration, and video settings.
- **Device control:** Hosts can inspect guest devices and request microphone changes. The recorder verifies the active track, handles repeated switches, reacts to `devicechange`, and reports device loss.
- **Video preview:** Video sessions support a portrait preview, a vertical VU meter, and a 180° display rotation. The preview controls do not alter the recorded video format.
- **Synchronization:** Absolute server-side start and stop timestamps keep guest recording boundaries aligned.
- **Markers:** Add `ad`, `cut_in`, and `cut_out` markers during recording, add notes, shift marker positions, and associate markers with a recording session.
- **Audio quality monitoring:** Compact level frames, VU ballistics, peak hold, configurable clipping thresholds, and persisted clipping events make overloads visible during and after a session.
- **Resilient uploads:** Chunk uploads, IndexedDB persistence, upload queues, upload throttling during live calls, final-chunk handling, and server-side duration normalization protect against browser timing and connection differences.

### Optional live calls

- **Jitsi integration:** Hosts can enable or disable a live call and switch between audio and video modes. Guests join through the Jitsi External API with a prejoin screen; the Host does not join the call.
- **Deployment modes:** Use a public Jitsi provider or a self-hosted Jitsi deployment with signed JWTs and an authenticated lifecycle callback for verified room closure.
- **Independent lifecycle:** Call state, participants, deadlines, and provider errors are persisted independently from recording sessions.

### Administration and access

- **Session catalog:** `/sessions` is the authoritative recording inventory, with lifecycle states, persistent recording numbers, readable labels, filters, cleanup, archiving, and room grouping.
- **Exports and previews:** Hosts receive MP3 previews only. Admins can access individual guest WAV files, per-session ZIP exports, and full-room ZIP archives.
- **Diagnostics:** The Admin panel exposes health and storage metrics, guest console logs, clipping events, and recording/session state.
- **Branding:** Configure global dark, light, or contrast themes, custom colors, logos, favicons, and reusable guest branding presets. A room can select its guest-facing preset without changing the global Admin/Host theme.
- **Localization:** The interface supports German and English, with safe German fallback and optional room-level locale selection. Guest consent text is configurable in both languages.
- **Concurrent Hosts:** One Host instance controls a room at a time. Additional instances open read-only and can request or take over control when available.
- **Roles:**
  - `admin`: Admin panel, configuration, exports, diagnostics, and Host studio
  - `host`: Room entry and Host studio without Admin access
  - `guest`: Token-based recorder and optional live call only

## Architecture

- **Backend:** `server.py` (FastAPI/Uvicorn)
  - Authentication with signed session cookies and bcrypt password stores
  - Room, session, call, and recording control via WebSockets and HTTP APIs
  - Chunk upload, WAV merge, optional MP4 processing, and session MP3 mixdown pipeline
  - Role-separated MP3 preview, WAV downloads, and ZIP exports
  - Configuration, branding, localization, diagnostics, cleanup, and room lifecycle APIs
  - `jitsi/jitsi.py`: Optional public/self-hosted Jitsi orchestration and lifecycle verification
  - Persistence through `config.json`, `auth.json`, and SQLite (`tokens.db`)
- **Frontend pages:**
  - `login.html`: Login
  - `index.html`: Room entry point and guest branding-preset selection
  - `host.html`: Host studio, lobby, markers, history, live call, and control lock
  - `recorder.html`: Consent gate, token-based guest recorder, device controls, and live call
  - `admin.html`: Configuration, profiles, rooms, tokens, branding, diagnostics, and recordings
  - `token_error.html`: Invalid or expired guest-link page
- **Frontend modules:**
  - `recording-media.js`: Shared media ownership, device rebinding, and upload budgeting
  - `audio-profiles.js`: Admin-managed recording profiles and consent text
  - `jitsi/call.js`: Public/self-hosted Jitsi External API integration
  - `jitsi/admin.js`: Jitsi configuration UI helpers
  - `ux.js`: Shared localization, API, and UI helpers
- **Optional Jitsi deployment:** `jitsi/mod_openpodcast_lifecycle.lua` closes self-hosted Jitsi rooms through an authenticated Prosody/MUC lifecycle hook

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

Install the dependencies from the included requirements file:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The runtime dependencies are `fastapi`, `uvicorn[standard]`, `bcrypt`, `itsdangerous`, `python-dotenv`, and `python-multipart`. The other Python modules used by the application (`sqlite3`, `wave`, `zipfile`, `threading`, `subprocess`, `pathlib`, and others) are part of the Python standard library.

### 2. Initial configuration

`DATA_DIR` must point to an existing, writable, persistent directory. The server refuses to start when it is missing or does not exist. Copy `example.env` to `.env` or provide the same values through the process environment:

```dotenv
DATA_DIR=/srv/open-podcast-data
DEFAULT_ADMIN_PASSWORD=change-this-admin-password
DEFAULT_HOST_PASSWORD=change-this-host-password
# SESSION_SECRET=  # optional; otherwise DATA_DIR/session_secret is generated
```

On first startup, the server generates a cryptographically random session secret and saves it in `DATA_DIR/session_secret`. It reuses this file after restarts and creates it with owner-only permissions on POSIX. An explicitly configured `SESSION_SECRET` takes precedence. A storage error or invalid secret file stops startup rather than silently using a temporary secret.

Keep `DATA_DIR` writable and persistent, including in Docker or other container deployments. Never commit or publicly serve `session_secret`, `.env`, `auth.json`, `config.json`, `tokens.db`, or recordings.

Keep all HTML and generic frontend files in the repository root, put `de.json` and `en.json` in `locale/`, and keep the Jitsi components in `jitsi/`. Run `python server.py`, open `http://localhost:8000/`, and sign in using **`CHANGEME!`** if no initial passwords were configured. No username is required. Immediately use the Admin panel to set **different passwords for admin and host** before exposing the server.

The application does not read `ADMIN_PASSWORD_HASH` or any other password hash from `.env`. Active bcrypt hashes are stored in `auth.json`; after that file exists, changing the initial password variables has no effect. Keep `SESSION_SECRET` unchanged between restarts, otherwise existing sessions become invalid.

### 3. Optional Jitsi live calls

Live calls are disabled by default. Configure one of the following modes in `.env`; the selected mode and public URL can later be changed in the Admin panel when no call is active.

#### Public Jitsi

```dotenv
JITSI_MODE=public
JITSI_PUBLIC_URL=https://meet.jit.si
```

#### Self-hosted Jitsi

```dotenv
JITSI_MODE=self_hosted
JITSI_DOMAIN=meet.example.org
JITSI_APP_ID=openpodcast
JITSI_APP_SECRET=at-least-32-characters
JITSI_CONTROL_KEY=at-least-32-characters
JITSI_CONTROL_URL=https://meet.example.org/openpodcast/close
```

Self-hosted mode requires a configured Jitsi JWT secret and an HTTPS lifecycle endpoint that confirms room closure. Install `mod_openpodcast_lifecycle.lua` in the Prosody/MUC setup and configure the endpoint according to your deployment. Both modes use Jitsi `external_api.js` and the provider's prejoin flow; the application does not use a native local Jitsi SDK.

### 4. Start the server

```bash
python server.py
```

The server listens on `0.0.0.0:8000` by default. Open:

- Local: `http://localhost:8000/`
- LAN: `http://<server-ip>:8000/`

### 5. Create a room and invite guests

1. Sign in as **admin** or **host**.
2. Open or create a room and optionally choose a guest-facing branding preset.
3. Create a guest invitation token in the Host studio.
4. Share the generated link:

```text
/recorder.html?token=<token>
```

5. Guests open the link, accept the configured consent text, enter their name, grant microphone/camera permissions, and select their devices.
6. The host optionally starts a Jitsi call, then starts and stops the recording centrally.
7. After finishing, the Host can preview the session MP3. Admins can download individual guest WAV tracks and a ZIP containing all WAVs of the session.

## Runtime data and storage

The application creates the following runtime files and directories under `DATA_DIR`:

- `session_secret`: Automatically generated persistent session-signing secret; **do not commit it**
- `auth.json`: Bcrypt hashes for the admin and host passwords
- `config.json`: Runtime configuration, branding, profiles, call settings, and cleanup settings
- `tokens.db`: Guest tokens, room registry, recording sessions, markers, guest logs, clipping events, and Jitsi call state
- `uploads/`: Chunks, metadata, and finished recordings
- `mixdowns/`: Generated session MP3 mixdowns
- `branding/`: Managed logo and favicon assets
- `.env`: Local secrets and initial-setup values; **do not commit it**

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

Cleanup threads can automatically remove old finished recordings, raw chunks, guest logs, and diagnostic data. The limits are configured in the Admin panel. Archived rooms are excluded from automatic deletion.

## Security and operations

- Admin and host sessions use signed, `HttpOnly`, `SameSite=strict` cookies.
- Passwords are stored as bcrypt hashes in `auth.json`.
- Guests access rooms through cryptographically random tokens; the token does not expose the room name.
- Login attempts are rate-limited per IP.
- Room, guest, and session path segments are validated to prevent unsafe paths.
- Recording downloads and previews are protected by role-based access rules.
- Self-hosted Jitsi control callbacks require HTTPS and a separate control key.
- For public or production deployment, configure **HTTPS/TLS**, a reverse proxy, secure secrets, backups, a restrictive firewall, and regular updates.
- A missing `SESSION_SECRET` is generated once and persisted in `session_secret`; it is never regenerated on ordinary restarts.
- Keep runtime data outside the public application directory where possible.

## Known limitations

- Part of the live room state is held in memory and is lost when the server restarts.
- Browser device access requires user permission, and device-change behavior varies between browsers.
- MP4 generation and WebM processing depend on a working FFmpeg installation and available codecs.
- Live calls depend on the configured Jitsi provider and, in self-hosted mode, a correctly configured lifecycle callback.
- The UI has German source text and an English locale with German fallback; translations may be incomplete for newly added text.
- A room has one active Host controller at a time; additional Host instances are read-only until control is available.

## Project structure

The repository uses three logical areas: application and frontend files in the repository root, translation files in `locale/`, and all optional Jitsi components in `jitsi/`. Runtime data belongs in the persistent `DATA_DIR`, not in the source tree.

### Repository root

Keep the application entry point, shared recording logic, configuration examples, dependencies, HTML pages, and generic frontend helpers here:

- `server.py`, `studio_core.py`, `__init__.py`
- `requirements.txt`, `example.env`, and `README.md`
- `login.html`, `index.html`, `host.html`, `recorder.html`, `admin.html`, and `token_error.html`
- `recording-media.js`, `audio-profiles.js`, and `ux.js`

### `locale/`

Keep all interface translations in this directory:

```text
locale/
├── de.json    # German source/default locale
└── en.json    # English locale with German fallback
```

### `jitsi/`

Keep all optional live-call and Jitsi deployment components in this directory:

```text
jitsi/
├── jitsi.py                       # Jitsi call orchestration and API routes
├── call.js                        # Guest/Host Jitsi External API client
├── admin.js                       # Jitsi configuration UI helpers
└── mod_openpodcast_lifecycle.lua  # Prosody/MUC lifecycle hook for self-hosting
```

The `jitsi/` directory is optional when live calls are disabled. The `locale/` directory is required for the German/English interface. Do not place runtime files in either directory.

### Complete file listing

```text
server.py                    # FastAPI/Uvicorn server
jitsi/jitsi.py                # Optional Jitsi call orchestration
jitsi/mod_openpodcast_lifecycle.lua # Optional Prosody/MUC room lifecycle hook
login.html                   # Login page
index.html                   # Room entry page
host.html                    # Host studio
recorder.html                # Guest recorder
admin.html                   # Admin panel
token_error.html             # Invalid or expired guest link
recording-media.js           # Shared media source and upload budget
audio-profiles.js              # Admin-managed audio profiles and consent text
jitsi/call.js                 # Jitsi External API integration
jitsi/admin.js                # Jitsi configuration UI helpers
ux.js                        # Shared frontend helpers
locale/de.json / en.json     # Locales
example.env                  # Configuration example
requirements.txt             # Python dependencies
```

## Reverse proxy deployment

For production or LAN deployment, place the application behind a reverse proxy. The proxy should terminate TLS and forward both regular HTTP requests and WebSocket connections to the Open Podcast Studio server.

The application listens on port `8000` by default:

```text
Browser  <->  HTTPS reverse proxy  <->  http://127.0.0.1:8000
```

### Nginx example

Save the following as `/etc/nginx/sites-available/open-podcast-studio` and enable it with a symlink in `/etc/nginx/sites-enabled/`. Replace `podcast.example.com` with your hostname and adjust the certificate paths.

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name podcast.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name podcast.example.com;

    ssl_certificate     /etc/letsencrypt/live/podcast.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/podcast.example.com/privkey.pem;

    client_max_body_size 2G;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
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

A room may optionally select a different locale and a guest-facing branding preset without changing the global instance theme.

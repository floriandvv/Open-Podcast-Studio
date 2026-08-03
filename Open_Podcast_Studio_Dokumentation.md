# Open Podcast Studio — Technical Documentation

> This document describes the current implementation based on the Open Podcast Studio frontend files and `server.py`.
>
> **Language:** The current user interface is German-only. English localization is planned for an upcoming release.

## 1. Purpose and overview

Open Podcast Studio is a browser-based web application for **remote podcast recording**. It follows a simple model:

- The **Host** controls a recording in a room: start, stop, settings, and markers.
- **Guests record locally** in their browsers.
- Recordings are uploaded in **chunks** and merged server-side into one WAV file per guest and session.

The result is a collection of separate tracks suitable for editing and mixing.

## 2. Components and files

### 2.1 Server

- **`server.py`**: FastAPI/Uvicorn application
  - Authentication with signed session cookies and roles
  - Token-based guest invitations backed by SQLite
  - Chunk upload API
  - Finish/merge pipeline for WAV, session-wide MP3 mixdowns, and optional MP4 output
  - Role-separated Host and Admin audio APIs
  - WebSocket channels for real-time status and control
  - Background cleanup for old recordings, chunks, and logs
  - Configurable persistent data directory through `DATA_DIR`

### 2.2 Frontend pages

- **`login.html`**: Login page
- **`index.html`**: Room entry point; Admin link is shown only to Admin users
- **`host.html`**: Host studio
  - Create guest invitation tokens
  - Start, stop, and clear recording sessions
  - Monitor live guest status and levels
  - Select a guest microphone
  - Create and manage markers
- **`recorder.html`**: Guest recorder
  - Token-based access
  - Microphone and speaker selection
  - Local recording and chunk uploads
  - Optional video mode and portrait-oriented presentation
  - Automatic device-list refresh
- **`admin.html`**: Admin panel
  - Runtime configuration, cleanup, and branding
  - Rooms, tokens, recordings, and password management
  - Dashboard metrics and diagnostic information
- **`token_error.html`**: Invalid or expired invitation page

## 3. Runtime data and persistence

### 3.1 Data directory

The application keeps mutable runtime data in `DATA_DIR`:

```text
DATA_DIR/
  auth.json
  config.json
  tokens.db
  uploads/
```

By default, `DATA_DIR` is the directory containing `server.py`. Docker deployments should set:

```dotenv
DATA_DIR=/data
```

and mount `/data` as a persistent volume.

### 3.2 Upload filesystem

Recordings use the following layout:

```text
uploads/<room>/<guest>/<session>/
  chunk-000001.pcm   # Raw PCM audio chunk
  chunk-000002.pcm
  chunk-000001.webm  # Optional WebM chunk
  meta.json          # Sample rate and channels for PCM
  full.wav           # Finished individual track
  full.mp4           # Optional video output

DATA_DIR/mixdowns/<room>/<session>/
  mixdown.mp3        # Combined session mixdown for Host/Admin preview
```

Each guest recording belongs to a session. Session IDs are internal, path-safe identifiers. The MP3 mixdown is a derived session artifact and is stored outside the guest upload tree.

### 3.3 SQLite database: `tokens.db`

SQLite stores:

- **`guest_tokens`**: invitation tokens, room, label, expiry, and revocation state
- **`markers`**: room/session marker events, type, offset, and note
- **`rooms`**: room registry, including rooms without recordings
- **`guest_logs`**: client-side diagnostic logs

### 3.4 Configuration: `config.json`

Runtime configuration is persisted in `config.json`. Important settings include:

- `token_days`: default lifetime of newly created guest tokens
- `recording_days`: automatic deletion period for finished recordings; `0` disables it
- `chunk_hours`: deletion period for raw chunks; `0` disables it
- `log_days`: retention period for guest diagnostic logs; `0` disables it
- `brand_name`, `brand_color`, `brand_favicon`
- `archived_rooms`: rooms that do not accept new guest tokens and are excluded from cleanup

Defaults apply when the file is created for the first time.

### 3.5 Authentication store: `auth.json`

`auth.json` stores:

- `admin_hash`
- `host_hash`

Both passwords are stored as bcrypt hashes. The `.env` file supplies initial default passwords only.

## 4. Authentication and roles

### 4.1 Login flow

- `GET /login` serves `login.html`.
- `POST /login` expects the `password` form field.
- A successful login sets a signed `ps_session` cookie.

The cookie is configured as `HttpOnly` and `SameSite=strict`. The default local configuration does not enable the `Secure` flag; production deployments should run behind HTTPS and review cookie settings.

### 4.2 Roles

- **Admin**
  - Access to the room entry page, Host studio, and Admin panel
- **Host**
  - Access to the room entry page and Host studio
  - No access to the Admin panel; returns HTTP 403

### 4.3 Login rate limiting

Login attempts are rate-limited per client IP:

- maximum of 5 attempts per 60-second window
- further attempts receive HTTP 429 until the window expires

## 5. Guest invitations and tokens

### 5.1 Token properties

- Tokens are generated with `secrets.token_urlsafe(32)`.
- A token does not contain the room name.
- Token-to-room mapping is stored server-side in `tokens.db`.
- Token comparison uses `hmac.compare_digest`.
- Tokens can expire, be revoked, or be permanently deleted.

### 5.2 Relevant routes

- `POST /host/token/{room}`
  - Creates or returns an active token for a room.
  - Returns a link such as `/recorder.html?token=...`.
- `GET /host/tokens/{room}`
  - Lists tokens for a room.
  - `active_only=1` filters revoked and expired tokens.
- `DELETE /host/token/{token_id}`
  - Revokes a token.
- `DELETE /host/token/hard/{token_id}`
  - Permanently removes a token.
- `GET /token/resolve?token=...`
  - Publicly validates a token and returns the room and expiry.

### 5.3 Recorder access

- `GET /recorder.html?token=...`
  - Missing, invalid, or expired token: serves `token_error.html` with HTTP 403.
  - Valid token: serves `recorder.html`.
- Recorder JavaScript resolves the room through `/token/resolve` before opening the guest WebSocket.

## 6. Room model

### 6.1 Identifier rules

Room, guest, and session path components are restricted to:

```text
^[a-zA-Z0-9_-]+$
```

This prevents path traversal when building upload paths.

### 6.2 Room registry

A room is registered when:

- the Host opens it through `/host/status/{room}` or `/host/room/{room}/ensure`, or
- a guest token is created for it

This allows the Admin panel to show a room before the first recording exists.

### 6.3 Archived rooms

Admins can archive rooms. Archived rooms:

- are listed in `archived_rooms`
- cannot receive new guest tokens
- are excluded from automatic recording/chunk cleanup

## 7. Real-time architecture

### 7.1 Design

Large audio/video uploads use HTTP because it is robust and chunkable. Control and status use WebSockets for low-latency communication.

### 7.2 WebSocket channels

- `WS /ws/guest/{room}/{guest}?token=...`
  - Token-protected
  - Guests send heartbeats, device information, and level frames
  - Guests receive recording commands, settings, and microphone commands
- `WS /ws/host/{room}`
  - Cookie-protected
  - Hosts receive status and level updates
  - Hosts send triggers, settings, markers, and microphone requests

### 7.3 In-memory room state

The server maintains a volatile `ROOMS[room]` object containing:

- `command`: current start/stop/clear command and timing
- `settings`: for example `audio_only` and `debug_level`
- `guests`: presence, telemetry, recording state, device inventory, and microphone results

This state is updated through HTTP polling or WebSocket messages and pushed to connected Host panels. It is not persisted across server restarts.

### 7.4 Presence states

Guest presence is derived from the last heartbeat. Current defaults in `server.py` are:

- `online`: up to approximately 6 seconds
- `stale`: up to approximately 20 seconds
- `offline`: after that
- removed from the in-memory list after approximately 120 seconds

A presence tick also pushes status periodically, so stale/offline transitions remain visible even when a guest stops sending messages.

### 7.5 Microphone control and device reliability

The Recorder reports its available microphones and current device. Device lists are refreshed on browser `devicechange` events without unnecessarily discarding the current selection. The Host can request a device change for a specific guest:

- if the guest is not recording, the new device is applied immediately;
- if the guest is recording, the request is queued until recording stops;
- the Recorder explicitly requests a new stream with `getUserMedia()` and the selected `deviceId`;
- the preview and analyser graph are rebuilt against the new stream;
- the active audio track is verified before success is reported;
- each Host request carries a unique `command_id`, so repeated requests cannot be resolved by a stale result;
- the guest reports success or failure through `mic_last_result`;
- if a device disappears during recording, the Recorder reports the loss and applies the configured fallback behavior;
- the Host receives `mic_pending` and result status in the room state.

### 7.6 Level frames

Guests send small level frames independently of the regular heartbeat. The server forwards compact level messages to Host panels. The Host renders the display locally with attack/release smoothing, peak hold, clipping indication, reduced-motion handling, and background-tab pausing.

## 8. Recording control

### 8.1 Start, stop, and clear

Host control uses messages equivalent to:

```json
{"type":"trigger","action":"start"}
```

Supported actions are `start`, `stop`, and `clear`.

A start command includes a lead-in. The default `START_LEAD_SECONDS` is 5 seconds, allowing guests to start in sync.

### 8.2 Settings

- `POST /host/settings/{room}` accepts values such as:
  - `audio_only: true|false`
  - `debug_level: 0..2`
- Equivalent settings messages can be sent over WebSockets.

Settings are broadcast to all guests in the room.

### 8.3 Markers

Hosts can create marker kinds such as:

- `ad`
- `cut_in`
- `cut_out`

Markers store the room, recording session, creation time, offset in milliseconds, and optional note. During finish, markers are written to the WAV file using RIFF `cue ` and `LIST/adtl` chunks on a best-effort basis.

## 9. Upload protocol

### 9.1 Chunk upload

```text
PUT /upload/{room}/{guest}/{session}/{chunk}?ext=pcm|webm
```

Rules:

- `chunk` must be a six-digit number, for example `000001`.
- `ext` must be `pcm` or `webm`.
- The server stores the body below the session directory.

### 9.2 PCM metadata

```text
POST /meta/{room}/{guest}/{session}
```

Example body:

```json
{"sample_rate":48000,"channels":1}
```

The server validates and bounds the values before writing `meta.json`.

### 9.3 Heartbeat and telemetry

Guest clients send display name, session, RMS level, upload queue, recording/upload metrics, device labels, and optional console logs. This information is sent through the guest WebSocket and, where supported by the client, the polling endpoint:

```text
POST /poll/{room}/{guest}
```

## 10. Finish and merge

### 10.1 Finish route

```text
POST /finish/{room}/{guest}/{session}
```

Processing:

1. Find chunks in the session directory.
2. If PCM chunks exist:
   - read sample rate and channel count from `meta.json`;
   - write `full.wav` with Python’s `wave` module.
3. If WebM chunks exist:
   - concatenate them into a temporary WebM file;
   - transcode audio to `full.wav` through FFmpeg;
   - attempt to create `full.mp4` when a video stream is present.
4. Write session markers to the WAV file on a best-effort basis.
5. Push the updated state to Host panels.

## 11. Downloads and exports

The recording history is session-centered:

- Each session has one combined MP3 mixdown for preview.
- The Host panel exposes the MP3 mixdown only; it does not expose WAV or ZIP downloads.
- The Admin panel shows the MP3 mixdown, the nested guest tracks, individual WAV downloads, and a ZIP containing all WAVs of that session.
- The full-room ZIP remains an Admin-only export.

Relevant routes:

```text
GET /host/mixdown/{room}/{session}                 # Host/Admin MP3 preview
GET /admin/preview/{room}/{guest}/{session}        # Admin-only WAV preview
GET /download/{room}/{guest}/{session}             # Admin-only WAV download
GET /admin/session-export/{room}/{session}         # Admin-only session ZIP
GET /export/{room}                                 # Admin-only room ZIP
GET /sessions                                      # Session/guest inventory
```

The server generates or refreshes the MP3 mixdown after a guest track is finished, after marker updates, when a guest track is deleted, and lazily for existing sessions when the session inventory is loaded. Mixdown generation uses FFmpeg and the `libmp3lame` encoder at 192 kbit/s. The mixdown is updated atomically so an audio player cannot read a partial file.

The `/sessions` response remains one record per guest track for compatibility, but includes shared session fields such as `has_mixdown`, `mixdown_url`, `created_at`, `last_seen`, `state`, and `archived`. Host and Admin UIs group these records by session.

## 12. Admin functions

### 12.1 Global configuration

- `GET /admin/config`
- `POST /admin/config`

Configuration includes token lifetime, recording/chunk/log retention, branding, and archived rooms.

### 12.2 Password management

```text
POST /admin/password
```

Admin-only request body:

```json
{"role":"admin|host","new_password":"..."}
```

### 12.3 Rooms

```text
GET /admin/rooms
POST /admin/room/{room}/archive
DELETE /admin/room/{room}
```

The room overview combines information from uploads, the token database, and the room registry. It includes session counts, total size, archive state, and online state.

Deleting a room removes its uploads, tokens, markers, guest logs, and registry entry.

### 12.4 Token overview

```text
GET /admin/tokens
```

The Admin view receives token previews, not full token values.

### 12.5 Session management

```text
GET /sessions
DELETE /admin/session/{room}/{guest}/{session}
```

The latter is Admin-only and deletes the complete session directory.

### 12.6 Guest diagnostics

```text
GET /admin/room/{room}/console?since=...
```

Returns recent guest console logs stored through polling or WebSocket heartbeats.

## 13. Cleanup and retention

### 13.1 Finished recordings

A background thread removes old recording directories when `recording_days > 0`. The cutoff is based on recording metadata or file modification time. The task runs approximately every six hours.

### 13.2 Raw chunks

A separate background thread removes old `chunk-*.pcm` and `chunk-*.webm` files when `chunk_hours > 0`. It runs approximately hourly.

### 13.3 Guest logs

Old guest diagnostic logs are removed when `log_days > 0`. Cleanup runs approximately every six hours.

Archived rooms are excluded from automatic cleanup.

## 14. Security and limitations

### 14.1 Protections

- Signed session cookies with `HttpOnly` and `SameSite=strict`.
- Token access bound to the server-side room mapping.
- Constant-time token comparison using `hmac.compare_digest`.
- Validated path components using the safe identifier pattern.
- Login rate limiting per IP.
- Passwords stored as bcrypt hashes.

### 14.2 Operational limitations

- `ROOMS` is in-memory and is not persistent across restarts.
- The default cookie configuration uses `secure=False` for local HTTP setups; use HTTPS and review this for production.
- MP4 generation is best effort and depends on FFmpeg and available H.264/AAC codecs.
- Browser microphone and device behavior depends on permissions and browser implementation.
- The current UI is German-only; English localization is planned.

## 15. Docker deployment

The repository includes a Dockerfile and Compose example. The image contains FFmpeg and runs the application as a non-root user.

Build and start:

```bash
docker compose build
docker compose up -d
```

The Compose example mounts `./data` to `/data` and sets `DATA_DIR=/data`. This keeps `auth.json`, `config.json`, `tokens.db`, and `uploads/` outside the container layer.

The default Compose binding exposes port `8000` only on localhost, making it suitable for use behind Nginx or Traefik.

## 16. Reverse proxy deployment

The application listens on port `8000` by default. In production, terminate TLS at Nginx or Traefik and forward both HTTP and WebSocket traffic:

```text
Browser  <->  HTTPS reverse proxy  <->  http://127.0.0.1:8000
```

The reverse proxy must:

- forward WebSocket upgrades;
- allow sufficiently large request bodies for recording chunks;
- use long read/send timeouts;
- keep the application port private;
- provide HTTPS, which is generally required for browser microphone access.

The repository README contains complete Nginx and Traefik examples.

## 17. Quickstart

### Local Python

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
python -m pip install fastapi uvicorn passlib[bcrypt] itsdangerous python-dotenv websockets
python server.py
```

Open `http://localhost:8000/`.

### First recording

1. Sign in with the Admin or Host password.
2. Open or create a room.
3. Create a guest token in the Host studio.
4. Share the recorder link.
5. Guests grant device permissions and select their microphones.
6. Start the recording from the Host studio.
7. Stop the recording, wait for uploads to finish, and download the WAV tracks or ZIP export.

### Docker

```bash
cp .env.example .env  # if an example file is provided
# edit .env
docker compose up -d
```

## 18. API reference

### Public HTTP endpoints

- `GET /health`
- `GET /branding`
- `GET /token/resolve?token=...`
- `PUT /upload/{room}/{guest}/{session}/{chunk}?ext=pcm|webm`
- `POST /meta/{room}/{guest}/{session}`
- `POST /finish/{room}/{guest}/{session}`
- `POST /poll/{room}/{guest}`

### Host or Admin endpoints

- `GET /`, `/index.html`, `/host.html`
- `GET /me`
- `GET /host/status/{room}`
- `POST /host/room/{room}/ensure`
- `POST /host/trigger/{room}`
- `POST /host/settings/{room}`
- `POST /host/token/{room}`
- `GET /host/tokens/{room}`
- `DELETE /host/token/{token_id}`
- `DELETE /host/token/hard/{token_id}`
- `POST /host/marker/{room}`
- `GET /host/marker_sessions/{room}`
- `POST /host/marker/{marker_id}/note`
- `GET /host/markers/{room}`
- `DELETE /host/marker/{marker_id}`
- `GET /download/{room}/{guest}/{session}`
- `GET /preview/{room}/{guest}/{session}`
- `GET /export/{room}`
- `GET /sessions`

### Admin-only endpoints

- `GET /admin`, `/admin.html`
- `GET /admin/config`, `POST /admin/config`
- `POST /admin/password`
- `GET /admin/rooms`
- `POST /admin/room/{room}/archive`
- `DELETE /admin/room/{room}`
- `GET /admin/room/{room}/guests`
- `GET /admin/room/{room}/console`
- `GET /admin/tokens`
- `DELETE /admin/token/{token_id}`
- `DELETE /admin/token/hard/{token_id}`
- `DELETE /admin/session/{room}/{guest}/{session}`

### WebSockets

- `WS /ws/guest/{room}/{guest}?token=...`
- `WS /ws/host/{room}`

## 19. Glossary

- **Room:** Logical recording group for a Host and its guests.
- **Guest:** Browser-based Recorder client.
- **Session:** One recording run.
- **Chunk:** Uploaded audio or video segment.
- **Merge:** Server-side combination of chunks into `full.wav`.
- **Marker:** Timestamp used for editing, advertising, or production notes.

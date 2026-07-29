# Open Podcast Studio

Open Podcast Studio is a lightweight, self-hosted web application for **remote podcast recording with separate audio tracks for each guest**.

The host opens a room, invites guests through secure token links, and controls start, stop, clear, and markers in real time. Guests record locally in their browsers and upload the data in chunks. The server combines the chunks for each guest and session into a WAV track; video sessions can additionally produce an MP4 file.

> **Project status:** Functional development build focused on audio recording, uploads, real-time control, and administration. Before exposing it to the public internet, review deployment, TLS, backups, monitoring, and access controls.

## Current status

- **Localization:** The current release is available in German only. An English user interface is planned and will follow in an upcoming release. 
- ✅ Host and guest control via WebSockets
- ✅ Token-based guest invitations without exposing the room name in the token
- ✅ Local browser recording with chunked uploads and server-side WAV merging
- ✅ Host-to-guest microphone control v1: device inventory, change requests, pending state, and result reporting
- ✅ Live level display with compact level frames, VU ballistics, peak hold, and clipping indication
- ✅ Automatic recorder device updates via `devicechange`, with diff-based rendering and stable selections
- ✅ Server-side branding injected into the `<head>` without visible FOUC; semantic colors remain independent
- ✅ Emoji-free interface with consistent spacing and top-bar heights
- ✅ Host guest states: `Connected`, `Connection problems`, and `Offline`
- ✅ Admin dashboard with recording, room, and storage-usage metrics
- ✅ Recording markers, individual WAV downloads, and ZIP export

See `Roadmap_Open_Podcast_Studio.md` for the longer-term plan and `Open_Podcast_Studio_Dokumentation.md` for the technical documentation.

## Features

- **One room, multiple guests:** Invite guests through token links
- **Local recording and separate tracks:** Avoids relying on a mixed call recording
- **Host-controlled synchronization:** Guests receive commands through WebSockets
- **Chunked uploads:** Keeps uploads resilient during recording
- **Exports:** Download individual tracks or a ZIP archive
- **Markers:** Add markers such as `ad`, `cut_in`, and `cut_out` during recording and write them into WAV files
- **Roles:**
  - `admin`: Admin panel and host studio
  - `host`: Host studio only

## Architecture

- **Backend:** `server.py` (FastAPI/Uvicorn)
  - Authentication with signed session cookies
  - Room and session control via WebSockets
  - Chunk upload and finish/merge pipeline
  - Downloads and ZIP exports
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

- **Python 3.10 or newer**
- A browser with `MediaRecorder`, `getUserMedia`, and WebSocket support
- A Uvicorn WebSocket backend: `websockets` or `wsproto`
- **FFmpeg** is optional and required for WebM processing or MP4 transcoding

The repository currently does not include a `requirements.txt`. Install the dependencies directly:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate

python -m pip install --upgrade pip
python -m pip install fastapi uvicorn passlib[bcrypt] itsdangerous python-dotenv websockets
```

`python-dotenv` is optional but recommended when using a `.env` file. For video support, make sure `ffmpeg` and `ffprobe` are available in the system `PATH`.

### 2. Initial configuration

Set a persistent session secret before starting the server. Example `.env`:

```dotenv
SESSION_SECRET=replace-with-a-long-random-value
DEFAULT_ADMIN_PASSWORD=choose-a-strong-admin-password
DEFAULT_HOST_PASSWORD=choose-a-strong-host-password
```

The default passwords are used only during initial setup and are then stored as bcrypt hashes in `auth.json`. Keep `SESSION_SECRET` unchanged between restarts, otherwise existing sessions become invalid.

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
7. After finishing, WAV tracks are available per guest, along with a ZIP export.

## Runtime data and storage

The application creates the following runtime files and directories:

- `auth.json`: bcrypt hashes for the admin and host passwords
- `config.json`: runtime configuration, branding, and cleanup settings
- `tokens.db`: guest tokens, room registry, and markers
- `uploads/`: chunks, metadata, and finished recordings
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
- Do not allow `SESSION_SECRET` to be regenerated automatically on every start.
- Keep `.env`, `auth.json`, `config.json`, `tokens.db`, and `uploads/` outside a public GitHub repository unless they are explicitly required and sanitized.

## Known limitations

- Part of the room state is held in memory and is lost when the server restarts.
- Browser device access requires user permission, and device-change behavior varies between browsers.
- MP4 generation and WebM processing depend on a working FFmpeg installation and available codecs.
- The current UI and API are primarily German; English localization is still planned.

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

Additional documentation:

- `Open_Podcast_Studio_Dokumentation.md`: Technical documentation
- `Open_Podcast_Studio_Zusammenfassung.md`: Short project overview
- `Roadmap_Open_Podcast_Studio.md`: Roadmap, completed work, and open items

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

### Traefik example with Docker Compose

The following example assumes that `server.py` is running in a Docker container named `open-podcast-studio` and that Traefik is already connected to the external Docker network `proxy`.

Create a `docker-compose.yml` similar to this:

```yaml
services:
  open-podcast-studio:
    build: .
    container_name: open-podcast-studio
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./runtime:/app/runtime
      - ./uploads:/app/uploads
    expose:
      - "8000"
    networks:
      - proxy
    labels:
      - traefik.enable=true
      - traefik.docker.network=proxy
      - traefik.http.routers.open-podcast-studio.rule=Host(`podcast.example.com`)
      - traefik.http.routers.open-podcast-studio.entrypoints=websecure
      - traefik.http.routers.open-podcast-studio.tls=true
      - traefik.http.routers.open-podcast-studio.tls.certresolver=letsencrypt
      - traefik.http.services.open-podcast-studio.loadbalancer.server.port=8000

networks:
  proxy:
    external: true
```

Traefik supports WebSocket forwarding automatically when the router targets the HTTP service. If a separate static Traefik configuration is preferred, the equivalent dynamic configuration is:

```yaml
http:
  routers:
    open-podcast-studio:
      rule: Host(`podcast.example.com`)
      entryPoints:
        - websecure
      service: open-podcast-studio
      tls:
        certResolver: letsencrypt

  services:
    open-podcast-studio:
      loadBalancer:
        servers:
          - url: http://127.0.0.1:8000
```

The Docker example is a deployment pattern, not a complete container image. Add a project-specific `Dockerfile`, ensure that runtime data is stored on persistent volumes, and do not put `.env`, `auth.json`, `config.json`, `tokens.db`, or recordings into a public image layer.

### Reverse proxy checklist

- Use HTTPS for every guest and host connection; browser microphone access generally requires a secure context.
- Forward WebSocket upgrades and keep long-running connections open.
- Allow request bodies large enough for the selected chunk size.
- Set sufficiently long read and send timeouts for recording and upload operations.
- Persist `SESSION_SECRET` and all runtime data across restarts.
- Restrict direct access to the application port `8000`.
- Test both a guest WebSocket connection and an upload before going live.

## License

This project is licensed under the **GNU General Public License v3.0 or later** (`GPL-3.0-or-later`). See the [`LICENSE`](LICENSE) file for the complete license text.

The software is provided **as is**, without warranty of any kind. See the warranty disclaimer and limitation of liability in the license for details.

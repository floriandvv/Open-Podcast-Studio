# Open Podcast Studio

Open Podcast Studio is a self-hosted web application for remote podcast recording. Each guest records a separate browser-based track; the host manages the session, guests, markers and optional live calls.

> **Project status:** Functional development build. Before public deployment, configure HTTPS, secure credentials, persistent storage, backups, monitoring and retention.

## Features

### Recording and production

- **Separate guest tracks:** Each guest records in their browser; the server uploads chunks and finalizes individual WAV tracks.
- **Session outputs:** Create session MP3 mixdowns and optional video files, depending on the selected recording profile.
- **Host controls:** Manage the lobby, guest readiness, recording start/stop and session markers (`ad`, `cut_in`, `cut_out`).
- **Media setup:** Select reusable audio/video profiles and guest microphones or cameras. Portrait and rotation controls affect the preview layout, not the recorded video format.
- **Monitoring and recovery:** View live audio levels and clipping events; upload queues, persisted chunks, final-chunk handling and duration normalization help protect recordings from timing or connection issues.

### Live calls

- **Provider choice:** Optional calls with MiroTalk SFU, public Jitsi, self-hosted Jitsi or Jitsi as a Service (JaaS).
- **Host call modes:** Switch between off, audio and audio-plus-video. The Host does not join the guest call; calls remain separate from the recording pipeline.

### Admin and operations

- **Instance setup:** Manage rooms, guest invitation tokens, roles, consent text, upload policy and retention.
- **Recording profiles:** Configure audio settings (quality, sample rate, channels, bitrate, chunk size and maximum duration) and video settings (codec, resolution, frame rate and bitrate).
- **Session catalog:** Review recordings grouped by session and guest, filter by lifecycle state, inspect diagnostics, and export permitted tracks or archives.
- **Health and diagnostics:** Review storage and session indicators, guest console logs, clipping events and finalization state.
- **Branding and language:** Configure themes, colors, logos, favicons, German/English interface text, and optional room-level guest branding and locale.
- **Access and control:** Use role-separated Admin/Host/Guest access. One Host instance controls a room at a time; additional instances can be read-only until control is available.

### Deployment and security

- Self-hosted with persistent runtime storage, signed sessions and bcrypt password hashes.
- Designed to run behind an HTTPS reverse proxy that supports WebSockets and large media uploads.
- Provider credentials, private keys and runtime data must remain server-side and outside version control.
- Functional development build: configure backups, monitoring, retention and access controls before public deployment.

## Quick start

Requirements: Python 3.10+, FFmpeg with `ffprobe` and MP3 encoding support, and a modern browser with `MediaRecorder`, `getUserMedia` and WebSocket support.

1. Create a persistent writable data directory and configure `.env` (start from `example.env`). At minimum, set `DATA_DIR`, `DEFAULT_ADMIN_PASSWORD` and `DEFAULT_HOST_PASSWORD` to secure values.
2. Install dependencies and start the server:

   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   python -m pip install -r requirements.txt
   python server.py
   ```

3. Open `http://localhost:8000/`, sign in and finish the Admin setup before inviting guests.

Keep `.env`, `DATA_DIR`, `session_secret`, `auth.json`, `config.json`, `tokens.db` and recordings private. Do not expose the development server directly to the public internet.

## License

GNU General Public License v3.0 or later. See [`LICENSE`](LICENSE).

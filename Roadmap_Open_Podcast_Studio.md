# Open Podcast Studio Roadmap

> This roadmap lists **open work only**. Completed items have been removed from the active plan.

## Near-term priorities

### 1. Recording lifecycle and session consistency

- Make `/sessions` the authoritative source for recording state and expose consistent fields:
  - `has_wav`
  - `chunks_count`
  - `merged`
  - `deleted`
  - `last_seen`
  - `created_at`
  - `archived`
  - `online`
- Improve the Admin recording overview so it clearly distinguishes:
  - sessions with chunks and a WAV
  - sessions with only a WAV
  - sessions with only chunks
  - failed or incomplete sessions
  - prepared but unused sessions
  - archived sessions
  - sessions currently used by a live Host panel
- Ensure cleanup and manual deletion remove sessions completely and do not leave stale or “zombie” entries.
- Ensure archived rooms are excluded from recording and chunk cleanup.
- Replace confusing internal session identifiers in the UI with readable recording labels while keeping stable IDs internally.

### 2. Recording history and audio previews

- Generate a combined MP3 mixdown for each recording session.
- Add an audio preview player for the MP3 mixdown in the Host panel.
- Keep individual WAV files available as downloads rather than using them as the primary preview.
- Rework recording history so sessions are shown first, with:
  - one mixdown preview per session
  - the guest list nested below
  - an individual WAV download for each guest

### 3. Device reliability

- Fix the remaining hot-plug edge case where a newly detected microphone appears in the device list but is not actually active.
- Rebuild the preview/analyser graph reliably after a device switch.
- Explicitly rebind the audio stream with `getUserMedia()` and the selected `deviceId`.
- Add clear handling when a device disappears during an active recording.
- Test browser-specific behavior, especially Safari and iOS.

## Quality, safety, and administration

### 4. Recording guardrails

- Allow the Host to start only when at least one guest is online and ready.
- Validate microphone/camera permission and selected devices before starting.
- Make start/stop commands idempotent and deduplicate them using `issued_at`, `action`, and `session`.
- Bind every marker strictly to a `session_id`, never only to the current room state.
- Improve RMS and clipping detection.
- Show a clear warning when a guest clips and persist clipping events in the Admin logs.

### 5. Session locking

- Prevent simultaneous Host instances from controlling the same room.
- Show a read-only state to additional Host clients.
- Make lock acquisition, renewal, release, and recovery after disconnect explicit.

### 6. Admin and diagnostics

- Prevent recording accordions and other expandable panels from closing unexpectedly.
- Add a Health and Diagnostics view covering:
  - WebSocket backend and connection state
  - uptime
  - active rooms
  - recent errors
  - client logs
  - ping and upload backlog
- Add an Admin action to rebuild a WAV manually from existing chunks.

## UX and accessibility

- Complete the keyboard accessibility pass, including visible focus states and ARIA behavior.
- Finish responsive testing for the Host guest grid, marker list, recording history, and Recorder interface.
- Store logos and favicons as managed files rather than data URLs.
- Show in the Admin panel which logo and favicon are currently active.
- Show guests in the Host panel as soon as they enter the invitation lobby, before they submit a display name.
- Distinguish multiple lobby guests reliably and show a clear pending state.
- Add English localization for the user interface. The current release is German-only in the UI; English localization is planned for an upcoming release.

## Branding and theming

- Extend the branding configuration beyond the primary color:
  - background color
  - button text color
  - general UI text color
- Replace remaining hard-coded component colors with theme tokens.
- Add logo variants for light and dark backgrounds.
- Add Admin live preview and reset functionality.
- Consider versioned theme presets such as Default, Dark, and High Contrast.
- Evaluate optional per-room branding without changing the global instance theme.

## Future / V2

- Jitsi integration
- Speech-to-text integration
- Lead-in audio such as an intro after the recording countdown
- Editing dashboard with timeline, marker positioning, and small cut corrections

## Release and operations backlog

- Add a complete automated test suite for authentication, tokens, WebSockets, uploads, merge, cleanup, and permissions.
- Add a pinned dependency file and reproducible build checks.
- Add container image scanning and multi-architecture image builds.
- Document backup and restore procedures for the persistent data volume.
- Review production cookie settings and reverse-proxy deployment behind HTTPS.
- Add structured server logging and clearer operational error messages.

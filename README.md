# mcp-qmedia

Self-written MCP server that lets **text-only agents ask questions about media** — images,
audio, video. The agent passes file paths (or URLs) plus a question; the server sends them to
**Xiaomi MiMo-V2.5** (natively omni-modal: text/image/audio/video in, text out) via the OpenCode
Zen endpoints and returns the answer as text. Used from both opencode and Claude Code.

## Providers

Same code path for all (OpenAI-compatible `/chat/completions`, base64 content parts). Two are
built in; more can be added, removed and the default switched in the **web UI** (below) — or by
editing the store `~/.config/mcp-qmedia/providers.json` (0600, outside the repo; may hold keys).
Per call: `backend="<name>"`; `QMEDIA_BACKEND` overrides the stored default.

| provider | endpoint | model | cost / limits | key entry in opencode's `auth.json` |
|---|---|---|---|---|
| `mimo-v2.5 - opencode go` (**default**) | `https://opencode.ai/zen/go/v1` | `mimo-v2.5` | ~$0.14/M in, 1M ctx | `opencode-go` |
| `mimo-v2.5-free - opencode zen` | `https://opencode.ai/zen/v1` | `mimo-v2.5-free` | free, 200k ctx, rate limited (429 `FreeUsageLimitError` when exhausted) | `opencode` |

## API keys — never in this repo

Per provider, resolution order: the provider's own key in the store (set via the UI) →
`QMEDIA_API_KEY` → `OPENCODE_API_KEY` → `~/.local/share/opencode/auth.json` (`QMEDIA_AUTH_JSON`):
the provider's `auth_entry`, then any other entry with a key. So if you are logged in to opencode
go / zen (`opencode auth login`), there is nothing to configure. `deploy/env.example` holds only
non-secret settings; the store is written 0600 and lives under `~/.config`, not in any repo.

## Setup wizard + probe (on demand, not a daemon)

`mcp-qmedia ui` (= `server.py ui [port]`) serves **http://127.0.0.1:8938** while it runs — start it,
configure, close it (like mcp-imap's setup wizard): list of providers (default marked, key status + where the key comes list of providers (default marked, key status + where the key comes
from — the key itself is never sent to the browser), add/edit/remove (built-ins can be reset,
not removed), make default, and a **probe** panel: paste http(s) URLs or local paths (one per
line), a question (empty = the `describe` prompt), pick a provider, Ask — answer + timing +
per-page history. JSON API behind it: `GET/POST /api/providers`, `DELETE /api/providers/<name>`,
`POST /api/default`, `POST /api/ask {files, question, provider}`, and `GET /api/status` (CORS `*`,
same shape as the messaging daemons' status — the hub page (ai-agent-setup `mcp-hub.service`, http://127.0.0.1:8930/) polls it; a
same field set as the daemons — but qmedia is not a daemon and has no hub card; it shows up in the
hub's Bridge card). The page links to "All daemons ↗" (`http://127.0.0.1:8930/`); the page itself is `web/page.html`.
The MCP server reads the same store on every call, so changes apply immediately without a restart.

## Tools (prefix `qmedia_` in opencode, `mcp__qmedia__` in Claude Code)

- `ask(files, question, backend="", system="")` — the main tool. `files` is a list of **absolute** local paths (`~/…` ok; the server is a shared daemon, its cwd is not your project)
  or `http(s)://` URLs; several files in one call so the model can relate them. Returns a
  short header (`[mimo-v2.5 - opencode go/mimo-v2.5 · 2 file(s) · 8.8s]` + per-file type/size) and the answer.
- `describe(files, backend="")` — no question needed: thorough description; images incl. all visible
  text verbatim, audio as verbatim transcript, video scene-by-scene with timestamps + transcript.
- `backends()` — providers: endpoints, models, which is default, whether a key resolves for each
  (never prints the key), ffmpeg availability, limits, store path.

Permission: all three are read-only and cheap → **allow** in both agents. Note: `ask`/`describe` upload the given files to the configured provider (a paid API for the default) — an allow-list wildcard means an agent can do that without asking, plan mode included; keep the default on the free provider if that matters to you.

## Media handling

| kind | sent as | notes |
|---|---|---|
| image (png/jpg/gif/webp/heic/avif…) | `image_url` data URI | > `QMEDIA_MAX_BYTES` → downscaled to ≤2000 px jpeg via ffmpeg |
| audio (wav/mp3 direct; m4a/ogg/opus/flac/… transcoded) | `input_audio` `{data, format}` | non-wav/mp3 or oversized → mp3 mono 16 kHz 48 kbps via ffmpeg |
| video (mp4/webm direct; mov/mkv/avi/… re-encoded) | `video_url` data URI **+** `input_audio` of the soundtrack | the endpoint only looks at the frames of `video_url`, so the audio track is extracted (ffprobe/ffmpeg) and sent as a second part; oversized/other containers → 480p/15 fps h264 |
| anything else (pdf, docx…) | — | error listing what is supported |

Type detection: extension table → `mimetypes` → HTTP `Content-Type` → magic bytes.
ffmpeg is optional but strongly recommended (`dnf install ffmpeg`); without it, only files that
need no transcoding work.

Verified 2026-08-17 on `mimo-v2.5 - opencode go`: image OCR, wav transcription (espeak sample), 3 s mp4 with beep
(frames + soundtrack), and image+audio in one call.

## Setup

Layout: `server.py` (stdio MCP server + `ui` wizard mode), `web/page.html` (the wizard page),
`deploy/bin/mcp-qmedia` (launcher), `deploy/env.example` (optional non-secret settings). No systemd unit:
on one machine with several agent sessions, run it once via [mcp-proxy](https://github.com/sparfenyuk/mcp-proxy)
(ai-agent-setup's `mcp-bridge.service`) or let each client spawn it. Wire it in with symlinks so a `git pull` updates the machine.

```sh
git clone git@github.com:spookysys/mcp-qmedia.git ~/.local/src/mcp-qmedia
cd ~/.local/src/mcp-qmedia
uv venv .venv --python 3.14 && uv pip install --python .venv/bin/python 'mcp>=1.9,<2' httpx
ln -s "$PWD/deploy/bin/mcp-qmedia" ~/.local/bin/          # launcher
# setup: mcp-qmedia ui   -> http://127.0.0.1:8938 (close it when done)
# optional: mkdir -p ~/.config/mcp-qmedia && cp deploy/env.example ~/.config/mcp-qmedia/env
```

opencode (`opencode.jsonc`):

```jsonc
"qmedia": { "type": "local", "command": ["/home/YOU/.local/bin/mcp-qmedia"], "enabled": true }
// or, shared through mcp-bridge: { "type": "remote", "url": "http://127.0.0.1:8939/servers/qmedia/sse" }
// permission: "qmedia_*": "allow"
```

Claude Code (user scope):

```sh
claude mcp add -s user qmedia --transport stdio -- ~/.local/bin/mcp-qmedia
# or, shared through mcp-bridge: claude mcp add -s user qmedia --transport sse http://127.0.0.1:8939/servers/qmedia/sse
# settings.json permissions.allow: "mcp__qmedia__*"
```

## Environment variables

| var | default | meaning |
|---|---|---|
| `QMEDIA_BACKEND` | store default (`mimo-v2.5 - opencode go`) | default provider override; an unknown name is an error (never a silent fallback to the paid built-in) |
| `QMEDIA_STORE` | `~/.config/mcp-qmedia/providers.json` | provider store (0600, may hold keys) |
| `QMEDIA_UI_PORT` | `8938` | port of the on-demand setup wizard (`mcp-qmedia ui`, 127.0.0.1) |
| `QMEDIA_API_KEY`, `OPENCODE_API_KEY` | — | key override for providers without a stored key (else the provider's own `auth_entry` in opencode's `auth.json` — never another entry's key) |
| `QMEDIA_AUTH_JSON` | `~/.local/share/opencode/auth.json` | where opencode keeps provider keys |
| `QMEDIA_MAX_BYTES` | `20000000` | per-file cap before ffmpeg transcoding |
| `QMEDIA_TIMEOUT` | `300` | model call timeout, seconds |
| `MCP_QMEDIA_PYTHON` | `.venv/bin/python` in the checkout | launcher: interpreter override |
| `MCP_QMEDIA_ENV` | `~/.config/mcp-qmedia/env` | launcher: settings file to source (optional) |
| `MCP_QMEDIA_DIR` | checkout | systemd unit: where `server.py` is |

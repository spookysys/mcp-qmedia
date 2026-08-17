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
| `go` (**default**) | `https://opencode.ai/zen/go/v1` | `mimo-v2.5` | ~$0.14/M in, 1M ctx | `opencode-go` |
| `free` | `https://opencode.ai/zen/v1` | `mimo-v2.5-free` | free, 200k ctx, rate limited (429 `FreeUsageLimitError` when exhausted) | `opencode` |

## API keys — never in this repo

Per provider, resolution order: the provider's own key in the store (set via the UI) →
`QMEDIA_API_KEY` → `OPENCODE_API_KEY` → `~/.local/share/opencode/auth.json` (`QMEDIA_AUTH_JSON`):
the provider's `auth_entry`, then any other entry with a key. So if you are logged in to opencode
go / zen (`opencode auth login`), there is nothing to configure. `deploy/env.example` holds only
non-secret settings; the store is written 0600 and lives under `~/.config`, not in any repo.

## Web UI — providers, keys, probe

`mcp-qmedia ui` (or `server.py ui [port]`, systemd user unit `deploy/mcp-qmedia-ui.service`) serves
**http://127.0.0.1:8938**: list of providers (default marked, key status + where the key comes
from — the key itself is never sent to the browser), add/edit/remove (built-ins can be reset,
not removed), make default, and a **probe** panel: paste http(s) URLs or local paths (one per
line), a question (empty = the `describe` prompt), pick a provider, Ask — answer + timing +
per-page history. JSON API behind it: `GET/POST /api/providers`, `DELETE /api/providers/<name>`,
`POST /api/default`, `POST /api/ask {files, question, provider}`, and `GET /api/status` (CORS `*`,
same shape as the messaging daemons' status — the `/hub` page served by those daemons polls it; a
hub entry is `{ name: "qmedia", port: 8938, unit: "mcp-qmedia-ui.service" }`). The page links to
"All daemons ↗" (`http://127.0.0.1:8934/hub`).
The stdio MCP server reads the same store on every call, so UI changes apply immediately.

## Tools (prefix `qmedia_` in opencode, `mcp__qmedia__` in Claude Code)

- `ask(files, question, backend="", system="")` — the main tool. `files` is a list of local paths
  (`~` ok) or `http(s)://` URLs; several files in one call so the model can relate them. Returns a
  short header (`[go/mimo-v2.5 · 2 file(s) · 8.8s]` + per-file type/size) and the answer.
- `describe(files, backend="")` — no question needed: thorough description; images incl. all visible
  text verbatim, audio as verbatim transcript, video scene-by-scene with timestamps + transcript.
- `backends()` — providers: endpoints, models, which is default, whether a key resolves for each
  (never prints the key), ffmpeg availability, limits, store path.

Permission: all three are read-only and cheap → **allow** in both agents.

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

Verified 2026-08-17 on `go`: image OCR, wav transcription (espeak sample), 3 s mp4 with beep
(frames + soundtrack), and image+audio in one call.

## Setup

Layout: `server.py` (MCP server + web UI), `deploy/bin/mcp-qmedia` (stdio launcher),
`deploy/mcp-qmedia-ui.service` (systemd user unit for the UI), `deploy/env.example` (optional
non-secret settings). Wire it in with symlinks so a `git pull` updates the machine.

```sh
git clone git@github.com:spookysys/mcp-qmedia.git ~/.local/src/mcp-qmedia
cd ~/.local/src/mcp-qmedia
uv venv .venv --python 3.14 && uv pip install --python .venv/bin/python 'mcp>=1.9,<2' httpx
ln -s "$PWD/deploy/bin/mcp-qmedia" ~/.local/bin/          # launcher
ln -s "$PWD/deploy/mcp-qmedia-ui.service" ~/.config/systemd/user/   # web UI (edit MCP_QMEDIA_DIR if not this path)
systemctl --user daemon-reload && systemctl --user enable --now mcp-qmedia-ui
# optional: mkdir -p ~/.config/mcp-qmedia && cp deploy/env.example ~/.config/mcp-qmedia/env
```

opencode (`opencode.jsonc`):

```jsonc
"qmedia": { "type": "local", "command": ["/home/YOU/.local/bin/mcp-qmedia"], "enabled": true }
// permission: "qmedia_*": "allow"
```

Claude Code (user scope):

```sh
claude mcp add -s user qmedia --transport stdio -- ~/.local/bin/mcp-qmedia
# settings.json permissions.allow: "mcp__qmedia__*"
```

## Environment variables

| var | default | meaning |
|---|---|---|
| `QMEDIA_BACKEND` | store default (`go`) | default provider override |
| `QMEDIA_STORE` | `~/.config/mcp-qmedia/providers.json` | provider store (0600, may hold keys) |
| `QMEDIA_UI_PORT` | `8938` | web UI port (127.0.0.1) |
| `QMEDIA_API_KEY`, `OPENCODE_API_KEY` | — | key override for providers without a stored key (else opencode's `auth.json`) |
| `QMEDIA_AUTH_JSON` | `~/.local/share/opencode/auth.json` | where opencode keeps provider keys |
| `QMEDIA_MAX_BYTES` | `20000000` | per-file cap before ffmpeg transcoding |
| `QMEDIA_TIMEOUT` | `300` | model call timeout, seconds |
| `MCP_QMEDIA_PYTHON` | `.venv/bin/python` in the checkout | launcher: interpreter override |
| `MCP_QMEDIA_ENV` | `~/.config/mcp-qmedia/env` | launcher: settings file to source (optional) |
| `MCP_QMEDIA_DIR` | checkout | systemd unit: where `server.py` is |

# mcp-qmedia

Self-written MCP server that lets **text-only agents ask questions about media** — images,
audio, video. The agent passes file paths (or URLs) plus a question; the server sends them to
**Xiaomi MiMo-V2.5** (natively omni-modal: text/image/audio/video in, text out) via the OpenCode
Zen endpoints and returns the answer as text. Used from both opencode and Claude Code.

## Backends

Same code path (OpenAI-compatible `/chat/completions`, base64 content parts); pick per call or
set the default with `QMEDIA_BACKEND`.

| backend | endpoint | model | cost / limits | key entry in opencode's `auth.json` |
|---|---|---|---|---|
| `go` (**default**) | `https://opencode.ai/zen/go/v1` | `mimo-v2.5` | ~$0.14/M in, 1M ctx | `opencode-go` |
| `free` | `https://opencode.ai/zen/v1` | `mimo-v2.5-free` | free, 200k ctx, rate limited (429 `FreeUsageLimitError` when exhausted) | `opencode` |

## API key — never in this repo

Resolution order: `QMEDIA_API_KEY` → `OPENCODE_API_KEY` → `~/.local/share/opencode/auth.json`
(`QMEDIA_AUTH_JSON`): the entry for the chosen backend, then the other one. So if you are logged
in to opencode go / zen (`opencode auth login`), there is nothing to configure. `deploy/env.example`
holds only non-secret settings.

## Tools (prefix `qmedia_` in opencode, `mcp__qmedia__` in Claude Code)

- `ask(files, question, backend="", system="")` — the main tool. `files` is a list of local paths
  (`~` ok) or `http(s)://` URLs; several files in one call so the model can relate them. Returns a
  short header (`[go/mimo-v2.5 · 2 file(s) · 8.8s]` + per-file type/size) and the answer.
- `describe(files, backend="")` — no question needed: thorough description; images incl. all visible
  text verbatim, audio as verbatim transcript, video scene-by-scene with timestamps + transcript.
- `backends()` — endpoints, models, which is default, whether a key resolves for each (never prints
  the key), ffmpeg availability, limits.

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

Layout: `server.py` (the server), `deploy/bin/mcp-qmedia` (stdio launcher),
`deploy/env.example` (optional non-secret settings). Wire it in with symlinks so a `git pull`
updates the machine.

```sh
git clone git@github.com:spookysys/mcp-qmedia.git ~/.local/src/mcp-qmedia
cd ~/.local/src/mcp-qmedia
uv venv .venv --python 3.14 && uv pip install --python .venv/bin/python 'mcp>=1.9,<2' httpx
ln -s "$PWD/deploy/bin/mcp-qmedia" ~/.local/bin/          # launcher
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
| `QMEDIA_BACKEND` | `go` | default backend (`go` / `free`) |
| `QMEDIA_API_KEY`, `OPENCODE_API_KEY` | — | key override (else opencode's `auth.json`) |
| `QMEDIA_AUTH_JSON` | `~/.local/share/opencode/auth.json` | where opencode keeps provider keys |
| `QMEDIA_MAX_BYTES` | `20000000` | per-file cap before ffmpeg transcoding |
| `QMEDIA_TIMEOUT` | `300` | model call timeout, seconds |
| `MCP_QMEDIA_PYTHON` | `.venv/bin/python` in the checkout | launcher: interpreter override |
| `MCP_QMEDIA_ENV` | `~/.config/mcp-qmedia/env` | launcher: settings file to source (optional) |

# mcp-qmedia

Self-written MCP server that lets **text-only agents ask questions about media** — images,
audio, video. The agent passes file paths (or URLs) plus a question and gets the answer as text.
Used from both opencode and Claude Code.

**It is a thin client of [mcp-caption](https://github.com/spookysys/mcp-caption)** (the captioning
service on :8937). That service owns the models, the API keys, the ffmpeg handling and the caption
queue the chat daemons share. This process only speaks MCP on stdio and HTTP to loopback — 148
lines, no keys, no web page.

It used to carry all of that itself, alongside mcp-signal and signal-backup-merge carrying their
own copies. One place to configure a model beats three.

## Tools (prefix `qmedia_` in opencode, `mcp__qmedia__` in Claude Code)

- `ask(files, question, backend="", system="")` — the main tool. `files` is a list of **absolute**
  local paths (`~/…` ok; this is a shared daemon, its cwd is not your project) or `http(s)://`
  URLs; several files in one call so the model can relate them. Returns a short header
  (`[openrouter · mimo-v2.5/xiaomi/mimo-v2.5 · 1 file(s) · 11.1s]` + per-file type/size) and the answer.
- `describe(files, backend="")` — no question needed: thorough description; images incl. all
  visible text verbatim, audio as verbatim transcript, video scene-by-scene with timestamps.
- `backends()` — the models available for these tools, which is default, and whether a key
  resolves for each (never prints a key).

Permission: all three are read-only and cheap → **allow** in both agents. Note they upload the
given files to a configured provider (a paid API by default), so an allow-list wildcard means an
agent can do that without asking, plan mode included.

**These tools always call a model.** They do not answer from the captioning service's archive,
even when the file is in it: those captions are two searchable lines, while `ask`/`describe` are
asked for everything, verbatim. Different question, different call.

## Configuration

There is none here worth the name. Models, API keys, ffmpeg presets and limits all belong to the
captioning service — open **http://127.0.0.1:8937/**, Models tab (`mcp-qmedia ui` just prints
that URL now). The service re-reads its store on every call, so changes apply immediately.

| var | default | meaning |
|---|---|---|
| `QMEDIA_SERVICE` | `http://127.0.0.1:8937` | where the captioning service lives |
| `QMEDIA_TIMEOUT` | `600` | how long to wait for an answer (it may transcode a large video first) |
| `MCP_QMEDIA_PYTHON` | `.venv/bin/python` in the checkout | launcher: interpreter override |
| `MCP_QMEDIA_ENV` | `~/.config/mcp-qmedia/env` | launcher: settings file to source (optional) |

If the service is down, every tool says so and names the unit to start:
`systemctl --user start mcp-caption`.

## Setup

Layout: `server.py` (stdio MCP server), `deploy/bin/mcp-qmedia` (launcher), `deploy/env.example`.
No systemd unit: on one machine with several agent sessions, run it once via
[mcp-proxy](https://github.com/sparfenyuk/mcp-proxy) (ai-agent-setup's `mcp-bridge.service`) or let
each client spawn it. Wire it in with symlinks so a `git pull` updates the machine.

```sh
git clone git@github.com:spookysys/mcp-qmedia.git ~/.local/src/mcp-qmedia
cd ~/.local/src/mcp-qmedia
uv venv .venv --python 3.14 && uv pip install --python .venv/bin/python -r requirements.txt
ln -s "$PWD/deploy/bin/mcp-qmedia" ~/.local/bin/
```

It needs **mcp-caption** running (`systemctl --user status mcp-caption`); that is where ffmpeg and
the API keys live now.

opencode (`opencode.jsonc`):

```jsonc
"qmedia": { "type": "local", "command": ["/home/YOU/.local/bin/mcp-qmedia"], "enabled": true }
// or, shared through mcp-bridge: { "type": "remote", "url": "http://127.0.0.1:8939/servers/qmedia/mcp" }
// permission: "qmedia_*": "allow"
```

Claude Code (user scope):

```sh
claude mcp add -s user qmedia --transport stdio -- ~/.local/bin/mcp-qmedia
# or, shared through mcp-bridge: claude mcp add -s user qmedia --transport http http://127.0.0.1:8939/servers/qmedia/mcp
# settings.json permissions.allow: "mcp__qmedia__*"
```

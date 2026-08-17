#!/usr/bin/env python3
"""qmedia - ask questions about media (images, audio, video) for text-only agents.

The agent hands over one or more files (local paths or http(s) URLs) plus a
question; the server sends them to Xiaomi MiMo-V2.5 (natively omni-modal:
text/image/audio/video in, text out) through the OpenCode Zen endpoints and
returns the answer as plain text.

Providers (same code path, OpenAI-compatible /chat/completions). Built in:
* go    - https://opencode.ai/zen/go/v1  model mimo-v2.5       (opencode go, paid, 1M ctx)   DEFAULT
* free  - https://opencode.ai/zen/v1     model mimo-v2.5-free  (opencode zen, free, 200k ctx)
More can be added/removed and the default switched in the web UI
(`server.py ui`, http://127.0.0.1:8938) or by editing the store file
~/.config/mcp-qmedia/providers.json (0600, outside the repo - it may hold keys).

API key per provider - never stored in this repo. Resolution order:
  provider's own api_key in the store -> QMEDIA_API_KEY -> OPENCODE_API_KEY ->
  ~/.local/share/opencode/auth.json (the provider's auth_entry, then any other
  entry that has a key).

Media is base64-encoded into multimodal content parts (image_url / input_audio /
video_url). Files larger than QMEDIA_MAX_BYTES, or audio in a format the API
does not take directly, are transcoded with ffmpeg first (if installed).

stdout is the MCP protocol channel: all diagnostics go to stderr.
"""

import base64
import json
import logging
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------- config

BUILTIN_PROVIDERS: dict[str, dict[str, str]] = {
    "go": {
        "base": "https://opencode.ai/zen/go/v1",
        "model": "mimo-v2.5",
        "auth_entry": "opencode-go",
        "note": "opencode go, paid ($0.14/M in), 1M ctx",
        "api_key": "",
    },
    "free": {
        "base": "https://opencode.ai/zen/v1",
        "model": "mimo-v2.5-free",
        "auth_entry": "opencode",
        "note": "opencode zen, free, 200k ctx",
        "api_key": "",
    },
}
BUILTIN_DEFAULT = "go"
STORE = Path(os.environ.get("QMEDIA_STORE", "~/.config/mcp-qmedia/providers.json")).expanduser()
UI_PORT = int(os.environ.get("QMEDIA_UI_PORT", "8938"))
AUTH_JSON = Path(
    os.environ.get("QMEDIA_AUTH_JSON", "~/.local/share/opencode/auth.json")
).expanduser()
MAX_BYTES = int(os.environ.get("QMEDIA_MAX_BYTES", str(20_000_000)))
TIMEOUT = float(os.environ.get("QMEDIA_TIMEOUT", "300"))
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

# Extensions mimetypes may not know / kinds we care about.
_EXT_MIME = {
    ".heic": "image/heic", ".heif": "image/heif", ".avif": "image/avif", ".webp": "image/webp",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/opus", ".flac": "audio/flac", ".wav": "audio/wav", ".mp3": "audio/mpeg",
    ".weba": "audio/webm", ".amr": "audio/amr",
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".3gp": "video/3gpp",
}
# Audio formats we hand to input_audio as-is; everything else is transcoded to mp3.
_AUDIO_DIRECT = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/mpeg": "mp3", "audio/mp3": "mp3"}
_MAGIC = (
    (b"\x89PNG", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF8", "image/gif"),
    (b"RIFF", None), (b"ID3", "audio/mpeg"), (b"\xff\xfb", "audio/mpeg"), (b"fLaC", "audio/flac"),
    (b"OggS", "audio/ogg"), (b"\x1a\x45\xdf\xa3", "video/webm"),
)

mcp = FastMCP("qmedia")


def _log(msg: str) -> None:
    print(f"qmedia: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- key / backend

_STORE_LOCK = threading.Lock()


def _load_store() -> dict:
    """{"default": name, "providers": {name: {base, model, auth_entry, note, api_key}}}.

    Built-ins are always present (a deleted built-in comes back without key -
    remove it from the UI list by making another one default; it stays usable).
    Env QMEDIA_BACKEND overrides the stored default."""
    data: dict = {}
    try:
        data = json.loads(STORE.read_text())
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        _log(f"cannot read {STORE}: {e}")
    provs: dict[str, dict] = {}
    for n, p in (data.get("providers") or {}).items():
        if isinstance(p, dict) and p.get("base") and p.get("model"):
            provs[n] = {"base": p["base"].rstrip("/"), "model": p["model"],
                        "auth_entry": p.get("auth_entry", ""), "note": p.get("note", ""),
                        "api_key": p.get("api_key", "")}
    for n, p in BUILTIN_PROVIDERS.items():
        provs.setdefault(n, dict(p))
    default = os.environ.get("QMEDIA_BACKEND", "").strip().lower() or data.get("default") or BUILTIN_DEFAULT
    if default not in provs:
        default = BUILTIN_DEFAULT
    return {"default": default, "providers": provs}


def _save_store(store: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, STORE)


def _backend(name: str) -> tuple[str, dict[str, str]]:
    store = _load_store()
    n = (name or store["default"]).strip().lower()
    if n not in store["providers"]:
        raise ValueError(f"unknown provider {name!r}; use one of {', '.join(store['providers'])}")
    return n, store["providers"][n]


def _auth_json() -> dict:
    try:
        return json.loads(AUTH_JSON.read_text())
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        _log(f"cannot read {AUTH_JSON}: {e}")
        return {}


def _api_key(name: str) -> tuple[str, str]:
    """Return (key, source-description). Never log or return the key elsewhere."""
    _, prov = _backend(name)
    if prov.get("api_key"):
        return prov["api_key"], "provider store"
    for var in ("QMEDIA_API_KEY", "OPENCODE_API_KEY"):
        v = os.environ.get(var, "").strip()
        if v:
            return v, f"env {var}"
    auth = _auth_json()
    entry = prov.get("auth_entry", "")
    order = ([entry] if entry else []) + [e for e in auth if e != entry]
    for e in order:
        k = (auth.get(e) or {}).get("key", "")
        if k:
            return k, f"{AUTH_JSON.name} [{e}]"
    raise RuntimeError(
        f"no API key for provider {name!r}: set one in the UI/store, or QMEDIA_API_KEY / "
        f"OPENCODE_API_KEY, or log in to opencode ('opencode auth login' -> {AUTH_JSON})"
    )


# ---------------------------------------------------------------- media

def _sniff(name: str, data: bytes, header_mime: str = "") -> str:
    ext = Path(name.split("?")[0]).suffix.lower()
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]
    guess = mimetypes.guess_type(name)[0]
    if guess and guess.split("/")[0] in ("image", "audio", "video"):
        return guess
    if header_mime and header_mime.split("/")[0] in ("image", "audio", "video"):
        return header_mime.split(";")[0].strip()
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            if mime:
                return mime
            if data[8:12] == b"WAVE":
                return "audio/wav"
            if data[8:12] == b"AVI ":
                return "video/x-msvideo"
            if data[8:12] == b"WEBP":
                return "image/webp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        return "audio/mp4" if brand in (b"M4A ", b"M4B ") else "video/mp4"
    return guess or "application/octet-stream"


def _fetch(src: str) -> tuple[str, bytes, str]:
    """Return (display name, bytes, content-type header if any)."""
    if src.startswith(("http://", "https://")):
        r = httpx.get(src, follow_redirects=True, timeout=60,
                      headers={"User-Agent": "mcp-qmedia/1.0 (+https://github.com/spookysys/mcp-qmedia)"})
        r.raise_for_status()
        return src, r.content, r.headers.get("content-type", "")
    p = Path(src).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"not a file: {src}")
    return str(p), p.read_bytes(), ""


def _ffmpeg(data: bytes, in_suffix: str, args: list[str], out_suffix: str) -> bytes:
    if not FFMPEG:
        raise RuntimeError("ffmpeg not installed - needed to transcode this file (dnf install ffmpeg)")
    with tempfile.TemporaryDirectory(prefix="qmedia-") as d:
        src = Path(d, "in" + in_suffix)
        dst = Path(d, "out" + out_suffix)
        src.write_bytes(data)
        cmd = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), *args, str(dst)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {r.stderr.strip()[-800:]}")
        return dst.read_bytes()


def _has_audio(data: bytes, suffix: str) -> bool:
    if not FFPROBE:
        return False
    with tempfile.TemporaryDirectory(prefix="qmedia-") as d:
        f = Path(d, "in" + suffix)
        f.write_bytes(data)
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(f)],
            capture_output=True, text=True, timeout=120,
        )
    return "audio" in r.stdout


def _prepare(src: str) -> tuple[str, list[dict]]:
    """Load one media source and turn it into chat content part(s).

    Returns (summary line, content parts). A video with an audio track yields
    two parts (video_url + input_audio) because the endpoint only looks at the
    frames of video_url."""
    name, data, ctype = _fetch(src)
    mime = _sniff(name, data, ctype)
    kind = mime.split("/")[0]
    ext = Path(name.split("?")[0]).suffix or mimetypes.guess_extension(mime) or ""
    orig = len(data)
    note = ""

    if kind == "image":
        if len(data) > MAX_BYTES:
            data = _ffmpeg(data, ext, ["-vf", "scale='min(2000,iw)':-2", "-q:v", "4"], ".jpg")
            mime, note = "image/jpeg", " (downscaled)"
        parts = [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}]
    elif kind == "audio":
        fmt = _AUDIO_DIRECT.get(mime)
        if not fmt or len(data) > MAX_BYTES:
            data = _ffmpeg(data, ext, ["-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k"], ".mp3")
            fmt, note = "mp3", " (transcoded to mp3)"
        parts = [{"type": "input_audio", "input_audio": {"data": base64.b64encode(data).decode(), "format": fmt}}]
    elif kind == "video":
        if len(data) > MAX_BYTES or mime not in ("video/mp4", "video/webm"):
            data = _ffmpeg(
                data, ext,
                ["-vf", "scale='min(854,iw)':-2", "-r", "15", "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", "30", "-c:a", "aac", "-b:a", "48k", "-movflags", "+faststart"],
                ".mp4",
            )
            mime, note = "video/mp4", " (re-encoded 480p/15fps)"
        parts = [{"type": "video_url", "video_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}]
        # The model only sees the frames of video_url; ship the soundtrack separately.
        if FFMPEG and _has_audio(data, ".mp4" if note else ext):
            try:
                audio = _ffmpeg(data, ".mp4" if note else ext, ["-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k"], ".mp3")
                parts.append({"type": "input_audio", "input_audio": {"data": base64.b64encode(audio).decode(), "format": "mp3"}})
                note += " + audio track"
            except RuntimeError as e:
                _log(f"{src}: could not extract audio track: {e}")
    else:
        raise ValueError(
            f"{src}: unsupported type {mime}; supported are images, audio and video "
            "(png/jpg/gif/webp/heic, wav/mp3/m4a/ogg/flac/opus, mp4/mov/webm/mkv/...)"
        )
    summary = f"{name} [{mime}, {orig/1e6:.1f} MB{note}]"
    return summary, parts


# ---------------------------------------------------------------- model call

def _chat(name: str, parts: list[dict], question: str, system: str) -> str:
    key, _src = _api_key(name)
    _, b = _backend(name)
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": [*parts, {"type": "text", "text": question}]})
    body = {"model": b["model"], "messages": messages}
    r = httpx.post(
        f"{b['base']}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=body,
        timeout=TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"{name}/{b['model']} HTTP {r.status_code}: {r.text[:1500]}")
    data = r.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"unexpected response: {json.dumps(data)[:1500]}") from None
    if isinstance(content, list):  # some servers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return (content or "").strip()


def _run(files: list[str], question: str, backend: str, system: str) -> str:
    if not files:
        raise ValueError("files must contain at least one path or URL")
    if isinstance(files, str):
        files = [files]
    name, b = _backend(backend)
    t0 = time.time()
    summaries, parts = [], []
    for f in files:
        s, p = _prepare(f)
        summaries.append(s)
        parts.extend(p)
    answer = _chat(name, parts, question, system)
    head = f"[{name}/{b['model']} · {len(files)} file(s) · {time.time()-t0:.1f}s]\n" + "\n".join(f"  {s}" for s in summaries)
    return f"{head}\n\n{answer}"


# ---------------------------------------------------------------- tools

@mcp.tool()
def ask(files: list[str], question: str, backend: str = "", system: str = "") -> str:
    """Ask a question about one or more media files (images, audio, video).

    files: local paths (~ ok) or http(s) URLs. Several files go in one call, so
    the model can compare/relate them. question: what you want to know
    (describe, read the text, transcribe, what happens at 0:42, is X visible...).
    backend: '' = default provider, or a name from backends() (built in: 'go', 'free'). system: optional
    system prompt. Returns the model's text answer with a short header."""
    return _run(files, question, backend, system)


@mcp.tool()
def describe(files: list[str], backend: str = "") -> str:
    """Thorough default description of media files - no question needed.

    Images: layout, objects, people, all visible text verbatim. Audio: verbatim
    transcript with speakers, plus non-speech sounds. Video: scene-by-scene with
    timestamps, on-screen text and speech transcript."""
    return _run(files, DESCRIBE_PROMPT, backend, "")


DESCRIBE_PROMPT = (
    "Describe this media thoroughly for someone who cannot see or hear it. "
    "Images: overall content, layout, notable objects/people, and ALL visible text verbatim. "
    "Audio: a verbatim transcript (label speakers if several) plus notable non-speech sounds, "
    "language and tone. Video: scene-by-scene description with timestamps, on-screen text, and a "
    "transcript of the speech (for video the soundtrack is attached as a separate audio part). "
    "Be concrete and complete; do not speculate beyond what is there."
)


def _provider_rows() -> list[dict]:
    store = _load_store()
    rows = []
    for n, b in store["providers"].items():
        try:
            _, src = _api_key(n)
            key_ok, key_src = True, src
        except RuntimeError as e:
            key_ok, key_src = False, str(e)
        rows.append({"name": n, "base": b["base"], "model": b["model"], "note": b.get("note", ""),
                     "auth_entry": b.get("auth_entry", ""), "builtin": n in BUILTIN_PROVIDERS,
                     "default": n == store["default"], "key_ok": key_ok, "key_source": key_src})
    return rows


@mcp.tool()
def backends() -> str:
    """List providers (endpoint, model, which is default, whether an API key resolves) and ffmpeg
    availability. Never prints a key. Manage providers in the web UI (mcp-qmedia ui, :8938)."""
    lines = []
    for r in _provider_rows():
        mark = " (default)" if r["default"] else ""
        key = f"key: yes ({r['key_source']})" if r["key_ok"] else f"key: NO - {r['key_source']}"
        lines.append(f"{r['name']}{mark}: {r['model']} @ {r['base']} - {r['note']}; {key}")
    lines.append(f"ffmpeg: {FFMPEG or 'not found (large/odd formats cannot be transcoded)'}")
    lines.append(f"max bytes per file before transcoding: {MAX_BYTES}; timeout: {TIMEOUT:.0f}s")
    lines.append(f"provider store: {STORE}")
    return "\n".join(lines)


# ---------------------------------------------------------------- web UI (server.py ui)

UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>qmedia · providers & probe</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font: 14px/1.45 system-ui, sans-serif; margin: 0; background: #14161a; color: #e8e8ea;
         display: flex; height: 100vh; }
  #side { width: 420px; border-right: 1px solid #26292f; padding: 12px; overflow: auto; }
  #side h1 { font-size: 15px; margin: 0 0 10px; }
  h2 { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: #8b90a0; margin: 14px 0 6px; }
  #providers li { list-style: none; padding: 7px 8px; border-radius: 6px; background: #1c1f26; margin: 4px 0; }
  #providers li.default { outline: 1px solid #4f7cff; }
  #providers .name { font-weight: 600; }
  #providers .meta { color: #8b90a0; font-family: ui-monospace, monospace; font-size: 12px; word-break: break-all; }
  #providers .key { font-size: 12px; }
  #providers .key.ok { color: #4ade80; } #providers .key.bad { color: #f87171; }
  #providers .row { display: flex; gap: 6px; margin-top: 5px; }
  input, textarea, select { background: #1c1f26; border: 1px solid #2a2e37; color: #e8e8ea; border-radius: 8px; padding: 7px 10px; font: inherit; }
  input:focus, textarea:focus, select:focus { outline: none; border-color: #4f7cff; }
  button { background: #4f7cff; border: 0; color: #fff; border-radius: 8px; padding: 7px 12px; cursor: pointer; font: inherit; }
  button.ghost { background: none; border: 1px solid #2a2e37; color: #8b90a0; }
  button.danger { background: none; border: 1px solid #5b2a2a; color: #f87171; }
  button:disabled { opacity: .5; cursor: default; }
  form.stack { display: flex; flex-direction: column; gap: 6px; }
  #main { flex: 1; display: flex; flex-direction: column; min-width: 0; padding: 16px; gap: 10px; overflow: auto; }
  #main textarea { width: 100%; }
  #files { min-height: 64px; font-family: ui-monospace, monospace; font-size: 12px; }
  #question { min-height: 56px; }
  #probeRow { display: flex; gap: 8px; align-items: center; }
  #answer { flex: 1; white-space: pre-wrap; word-break: break-word; background: #1c1f26; border-radius: 8px; padding: 12px; overflow: auto; font-size: 13px; }
  #answer.err { color: #f87171; }
  #answer .head { color: #8b90a0; font-family: ui-monospace, monospace; font-size: 12px; margin-bottom: 8px; }
  #history { max-height: 30vh; overflow: auto; }
  #history div { color: #8b90a0; font-size: 12px; padding: 3px 0; border-top: 1px solid #26292f; cursor: pointer; }
  small { color: #8b90a0; }
</style>
</head>
<body>
<div id="side">
  <h1>qmedia · providers <a href="http://127.0.0.1:8934/hub" style="font-weight:400;font-size:12px;color:#60a5fa;text-decoration:none">All daemons ↗</a></h1>
  <ul id="providers"></ul>
  <h2>Add / update provider</h2>
  <form class="stack" id="addForm">
    <input name="name" placeholder="name (e.g. go, free, openrouter)" required>
    <input name="base" placeholder="base URL, e.g. https://opencode.ai/zen/go/v1" required>
    <input name="model" placeholder="model, e.g. mimo-v2.5" required>
    <input name="api_key" type="password" placeholder="API key (empty = env / opencode auth.json)">
    <input name="auth_entry" placeholder="opencode auth.json entry to fall back to (e.g. opencode-go)">
    <input name="note" placeholder="note">
    <div class="row"><button type="submit">Save provider</button>
      <label><input type="checkbox" name="make_default"> make default</label></div>
  </form>
  <h2>Info</h2>
  <div id="info" class="meta" style="font-size:12px;color:#8b90a0;white-space:pre-wrap"></div>
</div>
<div id="main">
  <h2>Probe</h2>
  <textarea id="files" placeholder="one media source per line: http(s):// URL or local path (image / audio / video)"></textarea>
  <textarea id="question" placeholder="question (empty = thorough describe)"></textarea>
  <div id="probeRow">
    <select id="provider"></select>
    <button id="ask">Ask</button>
    <small id="status"></small>
  </div>
  <div id="answer"><small>answers appear here · keys are never sent to the browser</small></div>
  <h2>History (this page)</h2>
  <div id="history"></div>
</div>
<script>
const $ = s => document.querySelector(s);
const api = (m, u, b) => fetch(u, {method: m, headers: {'content-type': 'application/json'}, body: b ? JSON.stringify(b) : undefined}).then(async r => { const t = await r.text(); let j; try { j = JSON.parse(t); } catch { j = {error: t}; } if (!r.ok) throw new Error(j.error || t); return j; });
async function load() {
  const d = await api('GET', '/api/providers');
  const ul = $('#providers'); ul.innerHTML = '';
  const sel = $('#provider'); const cur = sel.value; sel.innerHTML = '';
  for (const p of d.providers) {
    const li = document.createElement('li'); if (p.default) li.className = 'default';
    li.innerHTML = `<div><span class="name">${p.name}</span> ${p.default ? '<small>· default</small>' : ''} ${p.builtin ? '<small>· built-in</small>' : ''}</div>
      <div class="meta">${p.model} @ ${p.base}</div>${p.note ? `<div class="meta">${p.note}</div>` : ''}
      <div class="key ${p.key_ok ? 'ok' : 'bad'}">${p.key_ok ? 'key: ' + p.key_source : 'no key: ' + p.key_source}</div>
      <div class="row">${p.default ? '' : `<button class="ghost" data-def="${p.name}">make default</button>`}
        <button class="ghost" data-edit="${p.name}">edit</button>
        <button class="danger" data-del="${p.name}">${p.builtin ? 'reset' : 'remove'}</button></div>`;
    ul.appendChild(li);
    const o = document.createElement('option'); o.value = p.name; o.textContent = p.name + (p.default ? ' (default)' : ''); sel.appendChild(o);
  }
  sel.value = cur && [...sel.options].some(o => o.value === cur) ? cur : d.default;
  $('#info').textContent = `store: ${d.store}\nffmpeg: ${d.ffmpeg || 'not found'}\nmax bytes/file: ${d.max_bytes} · timeout ${d.timeout}s`;
  ul.querySelectorAll('[data-def]').forEach(b => b.onclick = () => api('POST', '/api/default', {name: b.dataset.def}).then(load, e => alert(e.message)));
  ul.querySelectorAll('[data-del]').forEach(b => b.onclick = () => confirm(`Remove provider ${b.dataset.del}?`) && api('DELETE', '/api/providers/' + encodeURIComponent(b.dataset.del)).then(load, e => alert(e.message)));
  ul.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => { const p = d.providers.find(x => x.name === b.dataset.edit); const f = $('#addForm'); f.name.value = p.name; f.base.value = p.base; f.model.value = p.model; f.auth_entry.value = p.auth_entry; f.note.value = p.note; f.api_key.value = ''; f.api_key.placeholder = p.key_ok && p.key_source === 'provider store' ? '(key stored - leave empty to keep)' : 'API key (empty = env / opencode auth.json)'; f.name.focus(); });
}
$('#addForm').onsubmit = async e => {
  e.preventDefault(); const f = e.target; const b = Object.fromEntries(new FormData(f).entries()); b.make_default = f.make_default.checked;
  try { await api('POST', '/api/providers', b); f.reset(); f.api_key.placeholder = 'API key (empty = env / opencode auth.json)'; load(); } catch (err) { alert(err.message); }
};
const hist = [];
$('#ask').onclick = async () => {
  const files = $('#files').value.split('\n').map(s => s.trim()).filter(Boolean);
  const question = $('#question').value.trim(); const provider = $('#provider').value;
  if (!files.length) { alert('add at least one media URL/path'); return; }
  $('#ask').disabled = true; $('#status').textContent = 'asking ' + provider + '…'; const t0 = Date.now();
  const a = $('#answer'); a.className = ''; a.textContent = '';
  try {
    const r = await api('POST', '/api/ask', {files, question, provider});
    a.innerHTML = `<div class="head">${r.header.replace(/</g, '&lt;')}</div>` + r.answer.replace(/</g, '&lt;');
    hist.unshift({files, question, provider, ms: Date.now() - t0});
  } catch (err) { a.className = 'err'; a.textContent = err.message; }
  $('#status').textContent = ((Date.now() - t0) / 1000).toFixed(1) + 's'; $('#ask').disabled = false;
  $('#history').innerHTML = hist.map((h, i) => `<div data-i="${i}">${h.provider} · ${(h.ms / 1000).toFixed(1)}s · ${h.files.length} file(s) · ${(h.question || '(describe)').slice(0, 80).replace(/</g, '&lt;')}</div>`).join('');
  $('#history').querySelectorAll('div').forEach(dv => dv.onclick = () => { const h = hist[dv.dataset.i]; $('#files').value = h.files.join('\n'); $('#question').value = h.question; $('#provider').value = h.provider; });
};
load();
</script>
</body>
</html>
"""


_STARTED = time.time()
_STATS = {"asks": 0, "errors": 0, "last_ask": 0.0, "running": 0}
_STATS_LOCK = threading.Lock()


def _unit_state() -> dict:
    out = {}
    for what in ("enabled", "active"):
        try:
            r = subprocess.run(["systemctl", "--user", f"is-{what}", "mcp-qmedia-ui.service"],
                               capture_output=True, text=True, timeout=5)
            out[what] = (r.stdout or r.stderr).strip() or "?"
        except Exception:  # noqa: BLE001
            out[what] = "?"
    return out


def _status() -> dict:
    """Same shape the messaging daemons expose - the /hub page polls it."""
    store = _load_store()
    default = store["default"]
    prov = store["providers"][default]
    try:
        _, key_src = _api_key(default)
        ok, state = True, "ready"
    except RuntimeError:
        key_src, ok, state = "", False, "no API key"
    with _STATS_LOCK:
        st = dict(_STATS)
    return {
        "name": "qmedia",
        "uptime_s": round(time.time() - _STARTED),
        "connection": {"ok": ok, "state": state},
        "account": {"label": f"default {default} · {prov['model']}" + (f" · key from {key_src}" if key_src else "")},
        "providers": _provider_rows(),
        "store": {"total": st["asks"], "newest": st["last_ask"] or None, "errors": st["errors"], "path": str(STORE)},
        "jobs_running": st["running"],
        "mcp_sessions": None,  # stdio server: one process per agent, not tracked here
        "ffmpeg": FFMPEG,
        "service": _unit_state(),
        "tools": [{"name": "ask"}, {"name": "describe"}, {"name": "backends"}],
    }


class UIHandler(BaseHTTPRequestHandler):
    server_version = "qmedia-ui"

    def log_message(self, fmt, *args):  # quiet; stderr only on errors
        pass

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")  # the /hub page on the other daemons polls this
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            d = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise ValueError("body is not JSON") from None
        if not isinstance(d, dict):
            raise ValueError("body must be a JSON object")
        return d

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            body = UI_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/providers":
            store = _load_store()
            self._json(200, {"default": store["default"], "providers": _provider_rows(),
                             "store": str(STORE), "ffmpeg": FFMPEG, "max_bytes": MAX_BYTES, "timeout": TIMEOUT})
        elif path == "/api/status":
            self._json(200, _status())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            d = self._read_json()
            if path == "/api/providers":
                name = str(d.get("name", "")).strip().lower()
                base = str(d.get("base", "")).strip().rstrip("/")
                model = str(d.get("model", "")).strip()
                if not name or not base or not model:
                    raise ValueError("name, base and model are required")
                if not base.startswith(("http://", "https://")):
                    raise ValueError("base must be an http(s) URL")
                with _STORE_LOCK:
                    store = _load_store()
                    prev = store["providers"].get(name, {})
                    key = str(d.get("api_key", ""))
                    store["providers"][name] = {
                        "base": base, "model": model,
                        "auth_entry": str(d.get("auth_entry", "")).strip(),
                        "note": str(d.get("note", "")).strip(),
                        "api_key": key if key else prev.get("api_key", ""),
                    }
                    if d.get("make_default"):
                        store["default"] = name
                    _save_store(store)
                self._json(200, {"ok": True})
            elif path == "/api/default":
                name = str(d.get("name", "")).strip().lower()
                with _STORE_LOCK:
                    store = _load_store()
                    if name not in store["providers"]:
                        raise ValueError(f"unknown provider {name!r}")
                    store["default"] = name
                    _save_store(store)
                self._json(200, {"ok": True})
            elif path == "/api/ask":
                files = d.get("files") or []
                if isinstance(files, str):
                    files = [files]
                files = [str(f).strip() for f in files if str(f).strip()]
                question = str(d.get("question", "")).strip()
                provider = str(d.get("provider", "")).strip()
                if not question:
                    question = DESCRIBE_PROMPT
                with _STATS_LOCK:
                    _STATS["running"] += 1
                try:
                    out = _run(files, question, provider, str(d.get("system", "")))
                    with _STATS_LOCK:
                        _STATS["asks"] += 1
                        _STATS["last_ask"] = time.time()
                except Exception:
                    with _STATS_LOCK:
                        _STATS["errors"] += 1
                    raise
                finally:
                    with _STATS_LOCK:
                        _STATS["running"] -= 1
                header, _, answer = out.partition("\n\n")
                self._json(200, {"header": header, "answer": answer})
            else:
                self._json(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self._json(400, {"error": str(e)})

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if not path.startswith("/api/providers/"):
            self._json(404, {"error": "not found"})
            return
        from urllib.parse import unquote
        name = unquote(path[len("/api/providers/"):]).strip().lower()
        with _STORE_LOCK:
            store = _load_store()
            if name not in store["providers"]:
                self._json(404, {"error": f"unknown provider {name!r}"})
                return
            store["providers"].pop(name, None)  # built-ins reappear (reset) via _load_store
            if store["default"] == name:
                store["default"] = BUILTIN_DEFAULT
            _save_store(store)
        self._json(200, {"ok": True})


def _ui_server(port: int) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", port), UIHandler)
    srv.daemon_threads = True
    _log(f"web UI on http://127.0.0.1:{port}")
    srv.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "ui":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else UI_PORT
        try:
            _ui_server(port)
        except KeyboardInterrupt:
            pass
    else:
        mcp.run()

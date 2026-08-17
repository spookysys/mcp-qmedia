#!/usr/bin/env python3
"""qmedia - ask questions about media (images, audio, video) for text-only agents.

The agent hands over one or more files (local paths or http(s) URLs) plus a
question; the server sends them to Xiaomi MiMo-V2.5 (natively omni-modal:
text/image/audio/video in, text out) through the OpenCode Zen endpoints and
returns the answer as plain text.

Providers (same code path, OpenAI-compatible /chat/completions). Built in:
* mimo-v2.5 - opencode go       - https://opencode.ai/zen/go/v1  model mimo-v2.5       (opencode go, paid, 1M ctx)   DEFAULT
* mimo-v2.5-free - opencode zen - https://opencode.ai/zen/v1     model mimo-v2.5-free  (opencode zen, free, 200k ctx)
More can be added/removed and the default switched in the web UI
(`server.py ui`, http://127.0.0.1:8938 while it runs) or by editing the store file
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
import asyncio
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
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------- config

BUILTIN_PROVIDERS: dict[str, dict[str, str]] = {
    "mimo-v2.5 - opencode go": {
        "base": "https://opencode.ai/zen/go/v1",
        "model": "mimo-v2.5",
        "auth_entry": "opencode-go",
        "note": "",
        "api_key": "",
    },
    "mimo-v2.5-free - opencode zen": {
        "base": "https://opencode.ai/zen/v1",
        "model": "mimo-v2.5-free",
        "auth_entry": "opencode",
        "note": "",
        "api_key": "",
    },
}
BUILTIN_DEFAULT = "mimo-v2.5 - opencode go"
STORE = Path(os.environ.get("QMEDIA_STORE", "~/.config/mcp-qmedia/providers.json")).expanduser()
UI_PORT = int(os.environ.get("QMEDIA_UI_PORT", "8938"))  # `server.py ui` only (on demand, not a daemon)
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
        n = str(n).strip().lower()  # names are compared lower-cased everywhere; a hand-edited store must not crash lookups
        if isinstance(p, dict) and p.get("base") and p.get("model"):
            provs[n] = {"base": p["base"].rstrip("/"), "model": p["model"],
                        "auth_entry": p.get("auth_entry", ""), "note": p.get("note", ""),
                        "api_key": p.get("api_key", "")}
    for n, p in BUILTIN_PROVIDERS.items():
        provs.setdefault(n, dict(p))
    env_default = os.environ.get("QMEDIA_BACKEND", "").strip().lower()
    if env_default and env_default not in provs:
        # Never fall back silently to the paid built-in when the operator asked for something else.
        raise ValueError(f"QMEDIA_BACKEND={env_default!r} is not a known provider; known: {', '.join(provs)}")
    default = env_default or str(data.get("default") or "").strip().lower() or BUILTIN_DEFAULT
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
    # Only the provider's OWN auth.json entry — never "the first key of any
    # entry": that would send an unrelated provider's secret to this base URL.
    entry = prov.get("auth_entry", "")
    if entry:
        k = (_auth_json().get(entry) or {}).get("key", "")
        if k:
            return k, f"{AUTH_JSON.name} [{entry}]"
    raise RuntimeError(
        f"no API key for provider {name!r}: set one in the UI/store, or QMEDIA_API_KEY / OPENCODE_API_KEY"
        + (f", or log in to opencode so {AUTH_JSON} has an '{entry}' entry ('opencode auth login')" if entry else
           f" (this provider has no auth_entry, so opencode's {AUTH_JSON.name} is not consulted)")
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
    if not p.is_absolute():
        raise ValueError(f"local paths must be absolute (or ~/…): {src!r} — the server runs as a shared daemon, "
                         "its working directory is not your project")
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
async def ask(files: list[str], question: str, backend: str = "", system: str = "") -> str:
    """Ask a question about one or more media files (images, audio, video).

    files: ABSOLUTE local paths (~ ok; the server is a shared daemon, its cwd is
    not your project) or http(s) URLs. Several files go in one call, so the
    model can compare/relate them. question: what you want to know (describe,
    read the text, transcribe, what happens at 0:42, is X visible...).
    backend: '' = default provider, or a name from backends() (built in:
    'mimo-v2.5 - opencode go' and 'mimo-v2.5-free - opencode zen'). system:
    optional system prompt. Returns the model's text answer with a short header."""
    return await asyncio.to_thread(_run, files, question, backend, system)


@mcp.tool()
async def describe(files: list[str], backend: str = "") -> str:
    """Thorough default description of media files - no question needed.

    Images: layout, objects, people, all visible text verbatim. Audio: verbatim
    transcript with speakers, plus non-speech sounds. Video: scene-by-scene with
    timestamps, on-screen text and speech transcript. files: absolute paths or URLs."""
    return await asyncio.to_thread(_run, files, DESCRIBE_PROMPT, backend, "")


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
async def backends() -> str:
    """List providers (endpoint, model, which is default, whether an API key resolves) and ffmpeg
    availability. Never prints a key. Manage providers in the web UI (`mcp-qmedia ui`, http://127.0.0.1:8938/)."""
    return await asyncio.to_thread(_backends_text)


def _backends_text() -> str:
    lines = []
    for r in _provider_rows():
        mark = " (default)" if r["default"] else ""
        key = f"key: yes ({r['key_source']})" if r["key_ok"] else f"key: NO - {r['key_source']}"
        lines.append(f"{r['name']}{mark}: {r['model']} @ {r['base']}"
                     + (f" - {r['note']}" if r['note'] else "") + f"; {key}")
    lines.append(f"ffmpeg: {FFMPEG or 'not found (large/odd formats cannot be transcoded)'}")
    lines.append(f"max bytes per file before transcoding: {MAX_BYTES}; timeout: {TIMEOUT:.0f}s")
    lines.append(f"provider store: {STORE}")
    return "\n".join(lines)


# ---------------------------------------------------------------- web UI: on-demand setup wizard + probe
# `server.py ui [port]` serves web/page.html (providers, keys, default, probe) while you need it and is
# then closed - like mcp-imap's setup wizard. It is NOT a daemon: the MCP server is a plain stdio
# process (run once, shared, by ai-agent-setup's mcp-bridge); it re-reads the store on every call.

WEB_DIR = Path(__file__).resolve().parent / "web"
_STARTED = time.time()
_STATS = {"asks": 0, "errors": 0, "last_ask": 0.0, "running": 0}
_STATS_LOCK = threading.Lock()


def _unit_state() -> dict:
    out = {}
    for what in ("enabled", "active"):
        try:
            r = subprocess.run(["systemctl", "--user", f"is-{what}", "mcp-bridge.service"],
                               capture_output=True, text=True, timeout=5)
            out[what] = (r.stdout or r.stderr).strip() or "?"
        except Exception:  # noqa: BLE001
            out[what] = "?"
    return out


def _status() -> dict:
    """Same field set as the daemons' /api/status (ai-agent-setup/web/STATUS.md) - here for the wizard page only."""
    try:
        store = _load_store()
    except ValueError as e:  # e.g. QMEDIA_BACKEND names an unknown provider
        return {"name": "qmedia", "ok": False, "busy": False, "state": "misconfigured", "detail": str(e),
                "uptime_s": round(time.time() - _STARTED), "providers": [], "store": None, "jobs_running": 0,
                "mcp_sessions": None, "ffmpeg": FFMPEG, "service": _unit_state(), "tools": []}
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
        "ok": ok,
        "busy": False,
        "state": state,
        "detail": f"default {default} · {prov['model']}" + (f" · key from {key_src}" if key_src else ""),
        "uptime_s": round(time.time() - _STARTED),
        "providers": _provider_rows(),
        "store": {"total": st["asks"], "newest": st["last_ask"] or None, "errors": st["errors"], "path": str(STORE)},
        "jobs_running": st["running"],
        "mcp_sessions": None,  # served via mcp-bridge; not tracked here
        "ffmpeg": FFMPEG,
        "service": _unit_state(),
        "tools": [{"name": "ask"}, {"name": "describe"}, {"name": "backends"}],
    }


def _json_response(obj, code: int = 200):
    from starlette.responses import JSONResponse

    return JSONResponse(obj, status_code=code)


async def _read_json(request) -> dict:
    raw = await request.body()
    try:
        d = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise ValueError("body is not JSON") from None
    if not isinstance(d, dict):
        raise ValueError("body must be a JSON object")
    return d


async def _page(request):
    from starlette.responses import HTMLResponse

    return HTMLResponse((WEB_DIR / "page.html").read_text(encoding="utf-8"))


async def _api_status(request):
    import anyio

    return _json_response(await anyio.to_thread.run_sync(_status))


async def _api_providers_get(request):
    store = _load_store()
    return _json_response({"default": store["default"], "providers": _provider_rows(),
                           "store": str(STORE), "ffmpeg": FFMPEG, "max_bytes": MAX_BYTES, "timeout": TIMEOUT})


async def _api_providers_post(request):
    try:
        d = await _read_json(request)
        name = str(d.get("name", "")).strip().lower()
        old_name = str(d.get("old_name", "")).strip().lower()
        base = str(d.get("base", "")).strip().rstrip("/")
        model = str(d.get("model", "")).strip()
        if not name or not base or not model:
            raise ValueError("name, base and model are required")
        if not base.startswith(("http://", "https://")):
            raise ValueError("base must be an http(s) URL")
        with _STORE_LOCK:
            store = _load_store()
            prev = store["providers"].get(name) or store["providers"].get(old_name) or {}
            key = str(d.get("api_key", ""))
            store["providers"][name] = {
                "base": base, "model": model,
                "auth_entry": str(d.get("auth_entry", "")).strip(),
                "note": str(d.get("note", "")).strip(),
                "api_key": key if key else prev.get("api_key", ""),
            }
            if old_name and old_name != name:
                store["providers"].pop(old_name, None)  # built-in old name reverts to defaults
                if store["default"] == old_name:
                    store["default"] = name
            if d.get("make_default"):
                store["default"] = name
            _save_store(store)
        return _json_response({"ok": True})
    except Exception as e:  # noqa: BLE001
        return _json_response({"error": str(e)}, 400)


async def _api_providers_delete(request):
    from urllib.parse import unquote

    name = unquote(request.path_params["name"]).strip().lower()
    with _STORE_LOCK:
        store = _load_store()
        if name not in store["providers"]:
            return _json_response({"error": f"unknown provider {name!r}"}, 404)
        store["providers"].pop(name, None)  # built-ins reappear (reset) via _load_store
        if store["default"] == name:
            store["default"] = BUILTIN_DEFAULT
        _save_store(store)
    return _json_response({"ok": True})


async def _api_default(request):
    try:
        d = await _read_json(request)
        name = str(d.get("name", "")).strip().lower()
        with _STORE_LOCK:
            store = _load_store()
            if name not in store["providers"]:
                raise ValueError(f"unknown provider {name!r}")
            store["default"] = name
            _save_store(store)
        return _json_response({"ok": True})
    except Exception as e:  # noqa: BLE001
        return _json_response({"error": str(e)}, 400)


def _ask_blocking(files: list[str], question: str, provider: str, system: str) -> dict:
    with _STATS_LOCK:
        _STATS["running"] += 1
    try:
        out = _run(files, question, provider, system)
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
    return {"header": header, "answer": answer}


async def _api_ask(request):
    import anyio

    try:
        d = await _read_json(request)
        files = d.get("files") or []
        if isinstance(files, str):
            files = [files]
        files = [str(f).strip() for f in files if str(f).strip()]
        question = str(d.get("question", "")).strip() or DESCRIBE_PROMPT
        provider = str(d.get("provider", "")).strip()
        res = await anyio.to_thread.run_sync(_ask_blocking, files, question, provider, str(d.get("system", "")))
        return _json_response(res)
    except Exception as e:  # noqa: BLE001
        return _json_response({"error": str(e)}, 400)


class _OriginGuard:
    """The setup wizard is for the browser tab that opened it (same origin) —
    another web page must not be able to register a provider (attacker base URL)
    and POST /api/ask to upload local files + a key there. Non-GET requests with a
    foreign Origin → 403; no CORS."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("method", "GET") not in ("GET", "HEAD"):
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            origin = headers.get("origin")
            if origin:
                from urllib.parse import urlsplit

                try:
                    same = urlsplit(origin).netloc == headers.get("host", "")
                except Exception:  # noqa: BLE001
                    same = False
                if not same:
                    await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json")]})
                    await send({"type": "http.response.body", "body": b'{"error":"cross-origin write refused"}'})
                    return
        await self.app(scope, receive, send)


def _build_app():
    from starlette.routing import Route

    from starlette.applications import Starlette
    from starlette.middleware import Middleware

    return Starlette(middleware=[Middleware(_OriginGuard)], routes=[
        Route("/", _page, methods=["GET"]),
        Route("/api/status", _api_status, methods=["GET"]),
        Route("/api/providers", _api_providers_get, methods=["GET"]),
        Route("/api/providers", _api_providers_post, methods=["POST"]),
        Route("/api/providers/{name:path}", _api_providers_delete, methods=["DELETE"]),
        Route("/api/default", _api_default, methods=["POST"]),
        Route("/api/ask", _api_ask, methods=["POST"]),
    ])


def ui(port: int) -> None:
    import uvicorn

    _log(f"qmedia setup wizard + probe: http://127.0.0.1:{port}/  (Ctrl-C to stop)")
    uvicorn.run(_build_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "ui":
        try:
            ui(int(sys.argv[2]) if len(sys.argv) > 2 else UI_PORT)
        except KeyboardInterrupt:
            pass
    elif len(sys.argv) > 1:
        sys.exit("usage: server.py            # stdio MCP server\n       server.py ui [port]  # setup wizard + probe in the browser")
    else:
        mcp.run()

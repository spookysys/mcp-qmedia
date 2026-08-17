#!/usr/bin/env python3
"""qmedia - ask questions about media (images, audio, video) for text-only agents.

The agent hands over one or more files (local paths or http(s) URLs) plus a
question; the server sends them to Xiaomi MiMo-V2.5 (natively omni-modal:
text/image/audio/video in, text out) through the OpenCode Zen endpoints and
returns the answer as plain text.

Backends (same code path, OpenAI-compatible /chat/completions):
* go    - https://opencode.ai/zen/go/v1  model mimo-v2.5       (opencode go, paid, 1M ctx)   DEFAULT
* free  - https://opencode.ai/zen/v1     model mimo-v2.5-free  (opencode zen, free, 200k ctx)

API key - never stored in this repo. Resolution order:
  QMEDIA_API_KEY -> OPENCODE_API_KEY -> ~/.local/share/opencode/auth.json
  (entry "opencode-go" for go, "opencode" for free; falls back to the other one).

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
import time
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------- config

BACKENDS: dict[str, dict[str, str]] = {
    "go": {
        "base": "https://opencode.ai/zen/go/v1",
        "model": "mimo-v2.5",
        "auth_entry": "opencode-go",
        "note": "opencode go, paid ($0.14/M in), 1M ctx",
    },
    "free": {
        "base": "https://opencode.ai/zen/v1",
        "model": "mimo-v2.5-free",
        "auth_entry": "opencode",
        "note": "opencode zen, free, 200k ctx",
    },
}
DEFAULT_BACKEND = os.environ.get("QMEDIA_BACKEND", "go").strip().lower() or "go"
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

def _backend(name: str) -> tuple[str, dict[str, str]]:
    n = (name or DEFAULT_BACKEND).strip().lower()
    if n not in BACKENDS:
        raise ValueError(f"unknown backend {name!r}; use one of {', '.join(BACKENDS)}")
    return n, BACKENDS[n]


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
    for var in ("QMEDIA_API_KEY", "OPENCODE_API_KEY"):
        v = os.environ.get(var, "").strip()
        if v:
            return v, f"env {var}"
    auth = _auth_json()
    entry = BACKENDS[name]["auth_entry"]
    order = [entry] + [b["auth_entry"] for b in BACKENDS.values() if b["auth_entry"] != entry]
    for e in order:
        k = (auth.get(e) or {}).get("key", "")
        if k:
            return k, f"{AUTH_JSON} [{e}]"
    raise RuntimeError(
        f"no API key for backend {name!r}: set QMEDIA_API_KEY / OPENCODE_API_KEY, or log in "
        f"to opencode ('opencode auth login' -> {AUTH_JSON} entry {entry!r})"
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
        r = httpx.get(src, follow_redirects=True, timeout=60)
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
    b = BACKENDS[name]
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
    backend: '' = default (go), or 'go' / 'free' (see backends()). system: optional
    system prompt. Returns the model's text answer with a short header."""
    return _run(files, question, backend, system)


@mcp.tool()
def describe(files: list[str], backend: str = "") -> str:
    """Thorough default description of media files - no question needed.

    Images: layout, objects, people, all visible text verbatim. Audio: verbatim
    transcript with speakers, plus non-speech sounds. Video: scene-by-scene with
    timestamps, on-screen text and speech transcript."""
    q = (
        "Describe this media thoroughly for someone who cannot see or hear it. "
        "Images: overall content, layout, notable objects/people, and ALL visible text verbatim. "
        "Audio: a verbatim transcript (label speakers if several) plus notable non-speech sounds, "
        "language and tone. Video: scene-by-scene description with timestamps, on-screen text, and a "
        "transcript of the speech. Be concrete and complete; do not speculate beyond what is there."
    )
    return _run(files, q, backend, "")


@mcp.tool()
def backends() -> str:
    """List backends (endpoint, model, default, whether an API key resolves) and ffmpeg availability.
    Never prints the key itself."""
    lines = []
    for n, b in BACKENDS.items():
        try:
            _, src = _api_key(n)
            key = f"key: yes ({src})"
        except RuntimeError as e:
            key = f"key: NO - {e}"
        mark = " (default)" if n == DEFAULT_BACKEND else ""
        lines.append(f"{n}{mark}: {b['model']} @ {b['base']} - {b['note']}; {key}")
    lines.append(f"ffmpeg: {FFMPEG or 'not found (large/odd formats cannot be transcoded)'}")
    lines.append(f"max bytes per file before transcoding: {MAX_BYTES}; timeout: {TIMEOUT:.0f}s")
    return "\n".join(lines)


if __name__ == "__main__":
    if DEFAULT_BACKEND not in BACKENDS:
        _log(f"QMEDIA_BACKEND={DEFAULT_BACKEND!r} unknown; use one of {', '.join(BACKENDS)}")
        sys.exit(2)
    mcp.run()

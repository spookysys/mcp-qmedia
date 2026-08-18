#!/usr/bin/env python3
"""qmedia — let text-only agents ask questions about media (images, audio, video).

**A thin client of mcp-caption.** This used to carry its own ffmpeg handling,
its own provider store with keys, and its own setup wizard on :8938. All of that
now lives once, in the captioning service on :8937, together with the caption
queue the chat daemons use — so a file the archive has already described answers
`describe` for free, and there is one place to configure a model.

What stays here is the part that has to: the three tool names
(`ask` / `describe` / `backends`, already in both agents' allow-lists) and the
stdio MCP shape that lets mcp-bridge run this once and share it.

stdout is the MCP protocol channel: all diagnostics go to stderr.
"""

import asyncio
import os
import sys

import httpx
from mcp.server.fastmcp import FastMCP

SERVICE = os.environ.get("QMEDIA_SERVICE", "http://127.0.0.1:8937").rstrip("/")
# Generous: the service may be transcoding a large video before it even calls a
# model, and a caller that asked about a 200 MB file expects to wait.
TIMEOUT = float(os.environ.get("QMEDIA_TIMEOUT", "600"))

mcp = FastMCP("qmedia")

DESCRIBE_HINT = ""  # the service supplies its own default prompt when we send none


def _log(msg: str) -> None:
    print(f"qmedia: {msg}", file=sys.stderr, flush=True)


def _unreachable(e: Exception) -> str:
    return (
        f"the captioning service at {SERVICE} is not answering ({e}).\n"
        "It owns the models, the API keys and the media handling for this tool.\n"
        "  systemctl --user status mcp-caption\n"
        "  systemctl --user start mcp-caption"
    )


def _call(path: str, body: dict) -> dict:
    try:
        r = httpx.post(f"{SERVICE}{path}", json=body, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise RuntimeError(_unreachable(e)) from None
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"{SERVICE}{path} HTTP {r.status_code}: {r.text[:500]}") from None
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data["error"]))
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {str(data)[:500]}")
    return data


def _get(path: str) -> dict:
    try:
        r = httpx.get(f"{SERVICE}{path}", timeout=30)
    except httpx.HTTPError as e:
        raise RuntimeError(_unreachable(e)) from None
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(f"{SERVICE}{path} HTTP {r.status_code}: {r.text[:500]}") from None


def _ask(files: list[str], question: str, backend: str, system: str) -> str:
    if isinstance(files, str):
        files = [files]
    files = [str(f).strip() for f in files if str(f).strip()]
    if not files:
        raise ValueError("files must contain at least one path or URL")
    res = _call("/api/caption", {"files": files, "question": question,
                                 "provider": backend, "system": system})
    return f"{res.get('header', '')}\n\n{res.get('answer', '')}".strip()


@mcp.tool()
async def ask(files: list[str], question: str, backend: str = "", system: str = "") -> str:
    """Ask a question about one or more media files (images, audio, video).

    files: ABSOLUTE local paths (~ ok; this is a shared daemon, its cwd is not
    your project) or http(s) URLs. Several files go in one call so the model can
    compare or relate them. question: what you want to know (describe, read the
    text, transcribe, what happens at 0:42, is X visible…). backend: '' = the
    configured default, or a name from backends(). system: optional system prompt.
    Returns the model's answer with a short header naming what was sent."""
    return await asyncio.to_thread(_ask, files, question, backend, system)


@mcp.tool()
async def describe(files: list[str], backend: str = "") -> str:
    """Thorough default description of media files — no question needed.

    Images: layout, objects, people, all visible text verbatim. Audio: verbatim
    transcript with speakers, plus non-speech sounds. Video: scene-by-scene with
    timestamps, on-screen text and a speech transcript. files: absolute paths or URLs."""
    return await asyncio.to_thread(_ask, files, DESCRIBE_HINT, backend, "")


@mcp.tool()
async def backends() -> str:
    """List the models available for asking about media: endpoint, which is the
    default, and whether an API key resolves for each. Never prints a key.
    Configure them in the captioning service's UI."""
    return await asyncio.to_thread(_backends_text)


def _backends_text() -> str:
    data = _get("/api/providers")
    if data.get("error"):
        return f"the captioning service reports: {data['error']}"
    rows = [p for p in data.get("providers", []) if p.get("role") == "sync"]
    lines = []
    for p in rows:
        mark = " (default)" if p.get("default") else ""
        key = f"key: yes ({p['key_source']})" if p.get("key_ok") else f"key: NO — {p['key_source']}"
        note = f" — {p['note']}" if p.get("note") else ""
        lines.append(f"{p['name']}{mark}: {p['model']} @ {p['base']}{note}; {key}")
    if not lines:
        lines.append("no models configured for asking about single files")
    st = _get("/api/status")
    lines.append(f"ffmpeg: {'yes' if st.get('ffmpeg') else 'NOT INSTALLED — large/odd formats cannot be transcoded'}")
    # Deliberately not advertised as a cache hit for these tools: the queue's
    # captions are two searchable lines, ask/describe are asked for everything
    # verbatim. Different question, different call — this is context, not a
    # promise of free answers.
    lines.append(f"the service's caption archive holds "
                 f"{(st.get('store') or {}).get('total', '?')} item(s) "
                 "(the chat daemons' queue; ask/describe always call the model)")
    lines.append(f"configure at {st.get('hub_url') or SERVICE + '/'} (the Captions daemon, Models tab)")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "ui":
        # The wizard moved: providers, keys and the probe are part of the
        # captioning service's own page now, next to the queue they share.
        print(f"qmedia's settings live in the captioning service: {SERVICE}/\n"
              "  Models tab — providers and API keys\n"
              "  Probe tab  — ask about a file from the browser")
        sys.exit(0)
    if len(sys.argv) > 1:
        sys.exit("usage: server.py        # stdio MCP server\n"
                 "       server.py ui     # where the settings UI went")
    mcp.run()

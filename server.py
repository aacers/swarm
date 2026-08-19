#!/usr/bin/env python3
"""iMac → iPhone window remote. No TeamViewer. Safari on Wi‑Fi or 5G."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

from datetime import datetime, timezone

import Quartz
import roster as rosterlib
import agents_tmux

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
STATE_DIR = Path.home() / ".grok" / "imac-phone"
MEMORY_FILE = STATE_DIR / "shared-memory.md"
SETTINGS_FILE = STATE_DIR / "settings.json"
SECRETS_FILE = Path.home() / ".grok" / "secrets.env"
DEFAULT_SETTINGS = {
    "terminals": True,
    "browser": True,
    "overleg": True,
    "memory": True,
    "secrets": True,
    "new_terminal": True,
    "push": False,
    "awake": True,
    "drive": False,
    "theme": "light",
}
BROWSE = "http://127.0.0.1:8791"
TOKEN_FILE = STATE_DIR / "token"
URL_FILE = STATE_DIR / "url.txt"
GROK_AUTH = Path.home() / ".grok" / "auth.json"
BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
VERSION = "1.8.61"
TTS_VOICE = "eve"
TTS_CACHE = STATE_DIR / "tts-cache"
STABLE_PUB = "https://bumblly.com/s"  # Tim's tunnel; clones use LAN unless public-url.txt is set


def public_base() -> str:
    env = (os.environ.get("SWARM_PUBLIC_URL") or "").strip().rstrip("/")
    if env:
        return env
    p = STATE_DIR / "public-url.txt"
    try:
        if p.is_file():
            return p.read_text(encoding="utf-8").splitlines()[0].strip().rstrip("/")
    except Exception:
        pass
    return ""
JSON_BODY_MAX = 200_000
UPLOAD_BODY_MAX = 16_800_000  # 12 MB file + base64 JSON
UPLOAD_FILE_MAX = 12_000_000


def json_body_limit(path: str) -> int:
    if path in {"/api/upload", "/api/stt"}:
        return UPLOAD_BODY_MAX
    return JSON_BODY_MAX

SKIP_OWNERS = {
    "Window Server",
    "Dock",
    "Finder",
    "Control Centre",
    "Control Center",
    "Notification Center",
    "NotificationCentre",
    "SystemUIServer",
    "Spotlight",
    "TextInputMenuAgent",
    "Wi‑Fi",
    "ControlCenter",
    "WindowManager",
    "loginwindow",
}

TERMINAL_OWNERS = {
    "terminal",
    "iterm",
    "iterm2",
    "ghostty",
    "warp",
    "kitty",
    "alacritty",
    "wezterm",
}

PREFERRED_APPS = [
    "Safari",
    "Google Chrome",
    "Terminal",
    "Cursor",
    "Grok Bot",
    "Finder",
    "Preview",
    "Notes",
    "Messages",
    "Mail",
    "Calendar",
    "WhatsApp",
    "ChatGPT",
    "Simulator",
    "Music",
    "Photos",
    "System Settings",
]


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    for iface in ("en0", "en1"):
        try:
            out = subprocess.check_output(["ipconfig", "getifaddr", iface], text=True).strip()
            if out:
                return out
        except Exception:
            pass
    return "127.0.0.1"


def pick_port(preferred: int) -> int:
    for port in range(preferred, preferred + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port")


def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.is_file():
        try:
            data.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return data


def save_settings(data: dict) -> dict:
    cur = load_settings()
    for k, default in DEFAULT_SETTINGS.items():
        if k not in data:
            continue
        if k == "theme":
            cur[k] = "dark" if str(data[k]).lower() == "dark" else "light"
        elif isinstance(default, bool):
            cur[k] = bool(data[k])
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    return cur


def list_secret_names() -> list[str]:
    if not SECRETS_FILE.is_file():
        return []
    names = []
    for line in SECRETS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        names.append(line.split("=", 1)[0].strip())
    return names


def add_secret(name: str, value: str) -> None:
    name = re.sub(r"[^A-Z0-9_]", "", name.upper())
    if not name or not value:
        raise ValueError("naam of waarde ontbreekt")
    lines = []
    if SECRETS_FILE.is_file():
        lines = SECRETS_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    out, replaced = [], False
    for line in lines:
        if line.startswith(name + "="):
            out.append(f"{name}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{name}={value}")
    SECRETS_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    SECRETS_FILE.chmod(0o600)


def browse_proxy(
    method: str,
    subpath: str,
    body: dict | None = None,
    timeout: float = 25,
    query: str = "",
) -> tuple[int, str, bytes]:
    import urllib.error
    import urllib.request

    url = BROWSE + subpath
    if query:
        url += ("&" if "?" in url else "?") + query
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type") or "application/octet-stream", resp.read()
    except urllib.error.HTTPError as e:
        return e.code, "application/json", e.read()
    except Exception as e:
        return 503, "application/json", json.dumps({"ok": False, "error": str(e)}).encode()


_usage_cache: tuple[float, dict | None] = (0.0, None)
_usage_lock = threading.Lock()


def _grok_auth_record() -> dict:
    try:
        data = json.loads(GROK_AUTH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    for rec in data.values():
        if isinstance(rec, dict) and rec.get("key"):
            return rec
    return {}


def _grok_refresh_access(rec: dict) -> str:
    refresh = str(rec.get("refresh_token") or "")
    client = str(rec.get("oidc_client_id") or "")
    if not refresh:
        return str(rec.get("key") or "")
    body = urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client}
    ).encode()
    req = urllib.request.Request(
        "https://auth.x.ai/oauth2/token",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode())
    return str(data.get("access_token") or rec.get("key") or "")


def _xai_key() -> str:
    env = Path.home() / ".grok" / "secrets.env"
    if env.is_file():
        try:
            for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("XAI_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"')
        except Exception:
            pass
    try:
        data = json.loads(GROK_AUTH.read_text(encoding="utf-8"))
        first = next(iter(data.values())) if isinstance(data, dict) else {}
        if isinstance(first, dict) and first.get("key"):
            return str(first.get("key") or "")
    except Exception:
        pass
    return _grok_bearer()


def speakable(text: str) -> str:
    t = str(text or "")
    t = re.sub(r"```[\s\S]*?```", " ", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"[#*_>|]+", " ", t)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:1200]


def tts_mp3(text: str, voice: str = TTS_VOICE) -> tuple[bytes, str]:
    """Grok / xAI voice. Cached so greetings start instantly the second time."""
    text = speakable(text)
    if not text:
        raise RuntimeError("no text")
    voice = re.sub(r"[^a-z0-9_-]+", "", (voice or TTS_VOICE).lower())[:32] or TTS_VOICE
    TTS_CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{voice}|{text}".encode("utf-8")).hexdigest()
    cache = TTS_CACHE / f"{key}.mp3"
    if cache.is_file() and cache.stat().st_size > 400:
        return cache.read_bytes(), "audio/mpeg"
    token = _xai_key()
    if not token:
        raise RuntimeError("no xAI key for voice")
    body = json.dumps(
        {
            "text": text,
            "voice_id": voice,
            "language": "auto",
            "speed": 1.04,
            "text_normalization": True,
            "output_format": {
                "codec": "mp3",
                "sample_rate": 44100,
                "bit_rate": 192000,
            },
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.x.ai/v1/tts",
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    if not data or len(data) < 200:
        raise RuntimeError("tts empty")
    try:
        cache.write_bytes(data)
    except Exception:
        pass
    return data, "audio/mpeg"


def stt_bytes(data: bytes, filename: str = "talk.m4a") -> str:
    if not data:
        return ""
    token = _xai_key()
    if not token:
        raise RuntimeError("no xAI key for speech")
    fname = filename or "talk.m4a"
    ctype = "audio/mp4"
    if fname.endswith(".webm"):
        ctype = "audio/webm"
    elif fname.endswith(".mp3"):
        ctype = "audio/mpeg"
    elif fname.endswith(".wav"):
        ctype = "audio/wav"
    boundary = "----Swarm" + uuid.uuid4().hex
    parts: list[bytes] = []

    def field(name: str, val: str) -> None:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode()
        )

    field("model", "grok-stt")
    field("language", "nl")
    field("format", "true")
    parts.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n"
        ).encode()
        + data
        + b"\r\n"
        + f"--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(
        "https://api.x.ai/v1/stt",
        data=b"".join(parts),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        payload = json.loads(resp.read().decode())
    if isinstance(payload, dict):
        return str(payload.get("text") or payload.get("transcript") or "").strip()
    return ""


def _grok_bearer() -> str:
    rec = _grok_auth_record()
    key = str(rec.get("key") or "")
    exp = rec.get("expires_at")
    stale = False
    if exp:
        try:
            dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            stale = dt.timestamp() < time.time() + 90
        except Exception:
            stale = False
    if stale:
        try:
            return _grok_refresh_access(rec) or key
        except Exception:
            return key
    return key


def _pull_weekly_usage() -> dict | None:
    token = _grok_bearer()
    if not token:
        return None
    req = urllib.request.Request(
        BILLING_URL,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode())
    cfg = data.get("config") if isinstance(data, dict) else None
    if not isinstance(cfg, dict):
        cfg = data if isinstance(data, dict) else {}
    period = cfg.get("currentPeriod") if isinstance(cfg.get("currentPeriod"), dict) else {}
    products = cfg.get("productUsage") if isinstance(cfg.get("productUsage"), list) else []
    build = None
    for p in products:
        if isinstance(p, dict) and str(p.get("product") or "") == "GrokBuild":
            try:
                build = float(p.get("usagePercent"))
            except (TypeError, ValueError):
                build = None
            break
    try:
        pct = float(cfg.get("creditUsagePercent"))
    except (TypeError, ValueError):
        pct = build
    return {
        "percent": pct,
        "build": build,
        "reset": period.get("end") or cfg.get("billingPeriodEnd") or "",
        "start": period.get("start") or cfg.get("billingPeriodStart") or "",
        "tier": str(data.get("subscriptionTier") or ""),
    }


def weekly_usage(force: bool = False) -> dict | None:
    """SuperGrok weekly pool — same source as Grok /usage. Never blocks the UI."""
    global _usage_cache
    now = time.time()
    with _usage_lock:
        hit, val = _usage_cache
        fresh = val is not None and now - hit < 90
        if fresh and not force:
            return val
        stale = val

    def _bg() -> None:
        global _usage_cache
        try:
            out = _pull_weekly_usage()
        except Exception:
            return
        if out:
            with _usage_lock:
                _usage_cache = (time.time(), out)

    threading.Thread(target=_bg, daemon=True).start()
    return stale


def load_or_create_token() -> str:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(8)
    TOKEN_FILE.write_text(tok + "\n", encoding="utf-8")
    TOKEN_FILE.chmod(0o600)
    return tok


def is_grok_agent_window(title: str, w: float, h: float) -> bool:
    t = (title or "").strip().lower()
    if "grok" not in t:
        return False
    if t in {"terminal", "grok"}:
        return False
    if w and h and w < 200 and h < 80:
        return False
    return True


_win_cache: tuple[float, list[dict]] = (0.0, [])
_win_lock = threading.Lock()


def _refresh_windows_bg() -> None:
    global _win_cache
    if not _win_lock.acquire(blocking=False):
        return
    try:
        rows = list_windows()
        _win_cache = (time.time(), rows)
    except Exception:
        pass
    finally:
        _win_lock.release()


def list_windows_cached(ttl: float = 1.2) -> list[dict]:
    now = time.time()
    if now - _win_cache[0] > ttl:
        threading.Thread(target=_refresh_windows_bg, daemon=True).start()
    return _win_cache[1]


def list_windows() -> list[dict]:
    # Include miniaturized (yellow button) windows — they are off-screen, not gone.
    opts = Quartz.kCGWindowListExcludeDesktopElements
    raw = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
    out: list[dict] = []
    seen: set[int] = set()
    for item in raw:
        try:
            layer = int(item.get("kCGWindowLayer", 99))
            if layer not in (0, 8):
                continue
            owner = str(item.get("kCGWindowOwnerName") or "")
            if owner in SKIP_OWNERS:
                continue
            if owner.lower() not in TERMINAL_OWNERS:
                continue
            wid = int(item.get("kCGWindowNumber"))
            if wid in seen:
                continue
            title = str(item.get("kCGWindowName") or "").strip()
            bounds = item.get("kCGWindowBounds") or {}
            w = float(bounds.get("Width") or 0)
            h = float(bounds.get("Height") or 0)
            onscreen = bool(item.get("kCGWindowIsOnscreen", False))
            if ".command" in title:
                continue
            if not is_grok_agent_window(title, w, h):
                continue
            seen.add(wid)
            if rosterlib.is_forgotten(window_id=wid):
                continue
            out.append(
                {
                    "id": wid,
                    "app": owner,
                    "title": title or owner,
                    "x": float(bounds.get("X") or 0),
                    "y": float(bounds.get("Y") or 0),
                    "w": w,
                    "h": h,
                    "pid": int(item.get("kCGWindowOwnerPID") or 0),
                    "minimized": not onscreen,
                    "busy": (_tb := agents_tmux.title_busy(title)),
                    "activity": agents_tmux.activity_from_text(title, _tb),
                }
            )
        except Exception:
            continue
    # Hidden tmux agents (no Terminal.app window). Skip a tmux clone
    # when that bot already has a real Terminal.app window.
    have = {w["id"] for w in out}
    have_slugs = set()
    for w in out:
        sl = slug_for_window(w)
        if sl:
            have_slugs.add(sl)
    for sess in agents_tmux.list_sessions():
        if sess["id"] in have:
            continue
        sl = str(sess.get("slug") or "")
        if sl and sl in have_slugs:
            continue
        if rosterlib.is_forgotten(
            slug=sl,
            window_id=sess.get("id"),
            tmux=str(sess.get("tmux") or ""),
            session_id=str(sess.get("session_id") or ""),
        ):
            continue
        out.append(sess)
    return out


def merge_roster_windows(wins: list[dict]) -> list[dict]:
    try:
        rost = rosterlib.load_roster()
    except Exception:
        return wins
    have = {int(w["id"]) for w in wins}
    for slug, meta in (rost.get("agents") or {}).items():
        wid = meta.get("window_id")
        if not wid:
            continue
        try:
            wid = int(wid)
        except Exception:
            continue
        if wid in have:
            continue
        if rosterlib.is_forgotten(slug=slug, window_id=wid, tmux=str(meta.get("tmux") or ""), session_id=str(meta.get("session_id") or "")):
            continue
        title = meta.get("title") or meta.get("label") or ""
        if "grok" not in title.lower():
            continue
        wins.append(
            {
                "id": wid,
                "app": "Terminal",
                "title": meta.get("title") or meta.get("label") or "Agent",
                "x": 0,
                "y": 0,
                "w": 0,
                "h": 0,
                "pid": 0,
                "minimized": True,
            }
        )
        have.add(wid)
    return wins


def close_terminal_by_id(wid: int) -> None:
    """Close one leftover Terminal.app window. Quartz id first, then title."""
    try:
        wid = int(wid)
    except (TypeError, ValueError):
        return
    title = ""
    try:
        for w in list_windows():
            if int(w.get("id") or 0) == wid:
                title = str(w.get("title") or "")
                break
    except Exception:
        title = ""
    hint = unique_title_hint(title) if title else ""
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                "on run argv",
                "-e",
                "set hint to item 1 of argv",
                "-e",
                'tell application "Terminal"',
                "-e",
                "repeat with w in windows",
                "-e",
                "try",
                "-e",
                "set n to name of w as text",
                "-e",
                "if hint is not \"\" and n contains hint then",
                "-e",
                "close w",
                "-e",
                "return",
                "-e",
                "end if",
                "-e",
                "end try",
                "-e",
                "end repeat",
                "-e",
                "end tell",
                "-e",
                "end run",
                hint or "___none___",
            ],
            capture_output=True,
            timeout=2.0,
        )
    except Exception:
        pass


def hide_terminal_windows() -> None:
    """Hide Terminal.app grok windows without quitting them — they stay in Swarm."""
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "Terminal" to set miniaturized of every window to true',
            "-e",
            'tell application "System Events" to set visible of process "Terminal" to false',
        ],
        capture_output=True,
        timeout=6,
    )


def unminimize_terminals() -> None:
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "Terminal" to set miniaturized of every window to false',
        ],
        capture_output=True,
        timeout=5,
    )


SESSIONS_ROOT = Path.home() / ".grok" / "sessions"
USER_Q = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)


def window_label(w: dict) -> str:
    t = w.get("title") or "Agent"
    t = re.sub(r"^timgrootes\s*[—–-]\s*", "", t, flags=re.I)
    t = re.sub(r"\s*[—–-]\s*grok.*$", "", t, flags=re.I)
    t = re.sub(r"\s*▸.*$", "", t)
    t = re.sub(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏●○]", "", t)
    t = re.sub(
        r"^(Thinking|Waiting for response…|Waiting|Dump|Open|Preparing)[^-—]*[-—]\s*",
        "",
        t,
        flags=re.I,
    )
    t = re.sub(r"\s+", " ", t).strip()
    return t or "Agent"


def _norm(s: str) -> str:
    s = re.sub(r"^timgrootes\s*[—–-]\s*", "", s or "", flags=re.I)
    s = re.sub(r"\s*[—–-]\s*grok.*$", "", s, flags=re.I)
    s = re.sub(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏●○]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return s.strip()


_STOP = {
    "grok", "bot", "the", "and", "voor", "van", "het", "een", "met", "naar",
    "plus", "update", "waiting", "thinking", "response", "terminal",
}


def _tokens(s: str) -> set[str]:
    return {t for t in _norm(s).split() if len(t) > 2 and t not in _STOP}


_session_cache: tuple[float, list[dict]] = (0.0, [])


def session_index() -> list[dict]:
    global _session_cache
    now = time.time()
    if now - _session_cache[0] < 4:
        return _session_cache[1]
    out: list[dict] = []
    if not SESSIONS_ROOT.is_dir():
        return out
    for summary in SESSIONS_ROOT.rglob("summary.json"):
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
        except Exception:
            continue
        title = str(data.get("generated_title") or data.get("session_summary") or "")
        out.append(
            {
                "id": str((data.get("info") or {}).get("id") or summary.parent.name),
                "title": title,
                "summary": str(data.get("last_turn_summary") or ""),
                "path": summary.parent,
                "mtime": summary.stat().st_mtime,
                "busy": False,
            }
        )
    out.sort(key=lambda x: x["mtime"], reverse=True)
    out = out[:200]
    _session_cache = (now, out)
    return out


def match_session(win_title: str) -> dict | None:
    want = _tokens(win_title)
    if not want:
        return None
    best, score = None, 0
    for s in session_index():
        have = _tokens(s["title"] + " " + s["summary"])
        sc = len(want & have)
        if sc > score:
            best, score = s, sc
    return best if score >= 2 else None


def session_dir_by_id(sid: str) -> Path | None:
    """Newest copy of this session. Same id can exist under home and a workspace."""
    if not sid:
        return None
    hits: list[tuple[float, Path]] = []
    try:
        for cwd_dir in SESSIONS_ROOT.iterdir():
            if not cwd_dir.is_dir():
                continue
            cand = cwd_dir / sid
            if not cand.is_dir():
                continue
            hist = cand / "chat_history.jsonl"
            if hist.is_file() or (cand / "summary.json").is_file():
                mtime = hist.stat().st_mtime if hist.is_file() else cand.stat().st_mtime
                hits.append((mtime, cand))
    except Exception:
        return None
    if not hits:
        return None
    hits.sort(key=lambda x: -x[0])
    return hits[0][1]


def _iter_session_owners(rost: dict | None = None):
    rost = rost or rosterlib.load_roster()
    for slug, meta in (rost.get("agents") or {}).items():
        sid = str(meta.get("session_id") or "").strip()
        if sid:
            yield sid, slug
        for h in meta.get("helpers") or []:
            hs = str((h or {}).get("session_id") or "").strip()
            if hs:
                yield hs, str((h or {}).get("slug") or slug)


def taken_session_ids() -> set[str]:
    return {sid for sid, _ in _iter_session_owners()}


def session_owner(sid: str) -> str | None:
    sid = str(sid or "").strip()
    if not sid:
        return None
    for have, owner in _iter_session_owners():
        if have == sid:
            return owner
    return None


def cwd_is_private(slug: str, cwd: str) -> bool:
    """True only when cwd is this bot's own workspace — never the shared home folder."""
    if not slug or not cwd:
        return False
    try:
        root = (agents_tmux.WORK_ROOT / str(slug)).resolve()
        path = Path(cwd).expanduser().resolve()
    except Exception:
        return False
    if not root.exists():
        return False
    return path == root or root in path.parents


def slug_for_window(win: dict) -> str | None:
    """Prefer tmux / title slug over a recycled Quartz window id."""
    if not win:
        return None
    raw = str(win.get("slug") or "").strip()
    if raw:
        return raw
    rost = rosterlib.load_roster()
    agents = rost.get("agents") or {}
    tmux = str(win.get("tmux") or "").strip()
    if tmux:
        for slug, meta in agents.items():
            if str(meta.get("tmux") or "") == tmux:
                return slug
    title_slug = rosterlib.slug_from_title(str(win.get("title") or ""))
    if title_slug and title_slug in agents:
        return title_slug
    try:
        wid = int(win.get("id"))
    except (TypeError, ValueError):
        wid = None
    if wid is None:
        return None
    for slug, meta in agents.items():
        try:
            mid = int(meta.get("window_id")) if meta.get("window_id") is not None else None
        except (TypeError, ValueError):
            mid = None
        if mid == wid:
            return slug
    return None


def window_for_slug(slug: str) -> dict | None:
    """Resolve a bot by slug even when its Terminal window id changed."""
    slug = str(slug or "").strip()
    if not slug:
        return None
    rost = rosterlib.load_roster()
    agents = rost.get("agents") or {}
    if slug not in agents:
        return None
    meta = agents.get(slug) or {}
    wid = None
    try:
        if meta.get("window_id") is not None:
            wid = int(meta["window_id"])
    except (TypeError, ValueError):
        wid = None
    if wid is not None:
        win = find_window(wid)
        if win:
            got = str(win.get("slug") or "") or slug_for_window({**win, "slug": ""})
            if not got or got == slug:
                out = dict(win)
                out["slug"] = slug
                if meta.get("tmux"):
                    out["tmux"] = meta["tmux"]
                return out
    return {
        "id": wid if wid is not None else 0,
        "app": "Terminal",
        "title": meta.get("title") or meta.get("label") or slug,
        "tmux": meta.get("tmux"),
        "slug": slug,
        "x": 0,
        "y": 0,
        "w": 800,
        "h": 600,
        "pid": 0,
        "minimized": True,
    }


def session_dirs_for_cwd(cwd: str) -> list[Path]:
    if not cwd:
        return []
    try:
        resolved = str(Path(cwd).expanduser().resolve())
    except Exception:
        resolved = cwd
    enc = quote(resolved, safe="")
    root = SESSIONS_ROOT / enc
    if not root.is_dir():
        return []
    dirs = [p for p in root.iterdir() if p.is_dir() and (p / "chat_history.jsonl").is_file()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return dirs


def latest_grok_session_for_cwd(cwd: str) -> Path | None:
    dirs = session_dirs_for_cwd(cwd)
    return dirs[0] if dirs else None


def claude_history_path(cwd: str, sid: str) -> Path | None:
    if not cwd or not sid:
        return None
    try:
        resolved = str(Path(cwd).expanduser().resolve())
    except Exception:
        resolved = cwd
    enc = resolved.replace("/", "-")
    if not enc.startswith("-"):
        enc = "-" + enc
    p = Path.home() / ".claude" / "projects" / enc / f"{sid}.jsonl"
    return p if p.is_file() else None


def _blocks_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") in {"text", "input_text"} and b.get("text"):
                    parts.append(str(b["text"]))
                elif isinstance(b.get("text"), str):
                    parts.append(b["text"])
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(p for p in parts if p).strip()
    return ""


def parse_claude_chat(path: Path, limit: int = 200) -> list[dict]:
    msgs: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    for line in lines[-500:]:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        kind = obj.get("type")
        if kind not in {"user", "assistant"}:
            continue
        if obj.get("toolUseResult") or obj.get("isSidechain"):
            continue
        msg = obj.get("message") or {}
        text = _blocks_text(msg.get("content"))
        if not text or text.startswith("{"):
            continue
        if kind == "user" and ("tool_result" in text or text.startswith("<")):
            continue
        row = {"role": kind, "text": text[:8000]}
        at = obj.get("timestamp") or obj.get("created_at") or obj.get("at")
        if at is not None and at != "":
            row["at"] = at
        msgs.append(row)
    return msgs[-limit:]


def parse_codex_chat(cwd: str, limit: int = 200) -> list[dict]:
    root = Path.home() / ".codex" / "sessions"
    if not root.is_dir() or not cwd:
        return []
    try:
        want = str(Path(cwd).expanduser().resolve())
    except Exception:
        want = cwd
    files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    pick = None
    slug = Path(cwd).name
    for fp in files[:160]:
        try:
            first = fp.read_text(encoding="utf-8", errors="replace").splitlines()[:8]
        except Exception:
            continue
        blob = "\n".join(first)
        if want and want in blob:
            pick = fp
            break
        if slug and len(slug) >= 8 and slug in blob:
            pick = fp
            break
    if not pick:
        return []
    msgs: list[dict] = []
    try:
        lines = pick.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    for line in lines[-400:]:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "event_msg":
            continue
        payload = obj.get("payload") or {}
        kind = payload.get("type")
        text = str(payload.get("message") or "").strip()
        if not text:
            continue
        if kind == "user_message":
            msgs.append({"role": "user", "text": text[:4000]})
        elif kind == "agent_message":
            msgs.append({"role": "assistant", "text": text[:8000]})
    return msgs[-limit:]


def strict_match_session(slug: str, meta: dict, win: dict | None) -> Path | None:
    taken = taken_session_ids() - {str(meta.get("session_id") or "")}
    label = str(meta.get("label") or "")
    title = str((win or {}).get("title") or meta.get("title") or "")
    want = _tokens(label) | _tokens(title)
    want -= {"thinking", "waiting", "preparing", "response", "inspect", "remote", "iphone"}
    if len(want) < 2:
        return None
    scored: list[tuple[int, dict]] = []
    for s in session_index():
        if s["id"] in taken:
            continue
        have = _tokens(s["title"] + " " + s["summary"])
        sc = len(want & have)
        if sc >= 2:
            scored.append((sc, s))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    if scored[0][0] < 3 and (not label or rosterlib.is_status_label(label)):
        return None
    return scored[0][1]["path"]


def bind_new_session(slug: str, started: float) -> None:
    """Attach only a new, still-unbound Grok session — never steal another bot's."""
    global _session_cache
    for _ in range(40):
        time.sleep(0.5)
        _session_cache = (0.0, [])
        rost = rosterlib.load_roster()
        if slug not in rost.get("agents", {}):
            return
        if rost["agents"][slug].get("session_id"):
            return
        taken = taken_session_ids()
        fresh = [
            s
            for s in session_index()
            if s["mtime"] >= started - 2 and s["id"] not in taken
        ]
        if not fresh:
            continue
        fresh.sort(key=lambda s: s["mtime"], reverse=True)
        rost["agents"][slug]["session_id"] = fresh[0]["id"]
        rosterlib.save_roster(rost)
        return


def bind_by_recent_text(win: dict, text: str) -> None:
    slug = slug_for_window(win)
    if not slug:
        return
    rost = rosterlib.load_roster()
    if (rost.get("agents") or {}).get(slug, {}).get("session_id"):
        return
    needle = (text or "").strip()[:80]
    if len(needle) < 4:
        return
    global _session_cache
    _session_cache = (0.0, [])
    taken = taken_session_ids()
    hits = []
    for s in session_index():
        if s["id"] in taken:
            continue
        hist = s["path"] / "chat_history.jsonl"
        if not hist.is_file():
            continue
        try:
            blob = hist.read_text(encoding="utf-8", errors="replace")[-16000:]
        except Exception:
            continue
        if needle in blob:
            hits.append(s)
    if len(hits) != 1:
        return
    _persist_session(slug, hits[0]["id"])


FOLLOW_RE = re.compile(
    r"^(ja|nee|ok|oké|okay|prima|top|goed|klopt|stop|wacht|ga door|doorgaan|"
    r"thanks|dank|dankjewel|yes|no|go|wacht even|doe maar|graag)[\s!.?]*$",
    re.I,
)
CONT_RE = re.compile(r"^(en dan|en ook|daarna|ook nog|vervolgens|zelfde|nog even)\b", re.I)
_dispatch_lock = threading.Lock()


def unwrap_helper_user(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^\[Swarm extra\]\s*", "", t)
    if "\n---" in t:
        t = t.split("\n---", 1)[0].strip()
    return t


def helper_number(h: dict | None, idx: int = 0) -> int:
    slug = str((h or {}).get("slug") or "")
    m = re.search(r"-(\d+)$", slug)
    if m:
        try:
            n = int(m.group(1))
            if n > 0:
                return n
        except ValueError:
            pass
    return idx + 1


def fold_helper_thread(
    hmsgs: list[dict], busy: bool, task: str, thread: str
) -> list[dict]:
    """Keep the extra-agent question plus every answer. Hide only the swarm wrapper."""
    last_user = unwrap_helper_user(task or "")
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for hm in hmsgs or []:
        text = str(hm.get("text") or "").strip()
        role = hm.get("role")
        if not text or role not in {"user", "assistant"}:
            continue
        if role == "user":
            text = unwrap_helper_user(text)
            if not text or text.startswith("Je bent een extra agent"):
                continue
            last_user = text
        elif re.search(r"◆\s*Thinking|Waiting for response", text):
            continue
        sig = (role, re.sub(r"\s+", " ", text).strip().lower()[:280])
        if sig in seen:
            continue
        seen.add(sig)
        out.append(
            {
                "role": role,
                "text": text[:8000] if role == "assistant" else text,
                "helper": True,
                "thread": thread,
                "task": last_user,
            }
        )
    if not out and last_user:
        out.append(
            {
                "role": "user",
                "text": last_user,
                "helper": True,
                "thread": thread,
                "task": last_user,
            }
        )
    return out


def merge_helper_answers(msgs: list[dict], extras: list[dict]) -> list[dict]:
    """Put extra-agent questions and finished answers on the parent timeline."""
    out = list(msgs or [])
    have_user = {
        _norm_txt(m.get("text"))
        for m in out
        if m.get("role") == "user" and m.get("text")
    }
    have_asst = {
        (_norm_txt(m.get("text")), str(m.get("thread") or ""))
        for m in out
        if m.get("helper") and m.get("role") == "assistant" and m.get("text")
    }
    have_asst.update(
        (_norm_txt(m.get("text")), "")
        for m in out
        if m.get("role") == "assistant" and not m.get("helper") and m.get("text")
    )

    def _place(item: dict) -> None:
        task = _task_key(item.get("task") or "")
        if task:
            for i in range(len(out) - 1, -1, -1):
                if out[i].get("role") == "user" and _task_key(out[i].get("text") or "") == task:
                    j = i + 1
                    while j < len(out) and out[j].get("role") != "user":
                        j += 1
                    out.insert(j, item)
                    return
        out.append(item)

    for extra in extras or []:
        if not isinstance(extra, dict) or not extra.get("text"):
            continue
        role = extra.get("role")
        if role == "user":
            key = _norm_txt(extra.get("text"))
            if not key or key in have_user:
                continue
            have_user.add(key)
            _place(
                {
                    "role": "user",
                    "text": extra["text"],
                    "helper": True,
                    "thread": extra.get("thread") or "",
                    "task": extra.get("task") or extra["text"],
                    "n": extra.get("n"),
                    "at": extra.get("at"),
                }
            )
            continue
        if role != "assistant":
            continue
        sig = (_norm_txt(extra.get("text")), str(extra.get("thread") or ""))
        if sig in have_asst or (_norm_txt(extra.get("text")), "") in have_asst:
            continue
        have_asst.add(sig)
        _place(
            {
                "role": "assistant",
                "text": extra["text"],
                "helper": True,
                "thread": extra.get("thread") or "",
                "task": extra.get("task") or "",
                "n": extra.get("n"),
                "name": extra.get("name") or "",
                "at": extra.get("at"),
            }
        )
    return out


def helper_thread_messages(h: dict, idx: int, busy: bool) -> list[dict]:
    sid = str((h or {}).get("session_id") or "").strip()
    thread = str((h or {}).get("slug") or sid or "")
    n = helper_number(h, idx)
    hmsgs: list[dict] = []
    if sid:
        pth = session_dir_by_id(sid)
        if pth:
            try:
                hmsgs = parse_chat(pth)
            except Exception:
                hmsgs = []
    folded = fold_helper_thread(hmsgs, busy, str((h or {}).get("task") or ""), thread)
    out: list[dict] = []
    for m in folded:
        role = m.get("role")
        if role == "user" or (role == "assistant" and not busy):
            out.append({**m, "n": n, "name": f"Agent {n}"})
    return out


def attach_helper_messages(msgs: list[dict], slug: str, meta: dict, main_sid: str) -> list[dict]:
    extras: list[dict] = []
    try:
        items = extra_threads_for(slug, main_sid, meta)
    except Exception:
        items = [dict(h) for h in (meta.get("helpers") or []) if isinstance(h, dict)]
    live: dict = {}
    try:
        live = {s["tmux"]: s for s in agents_tmux.list_sessions(include_helpers=True)}
    except Exception:
        pass
    for i, h in enumerate(items):
        if not isinstance(h, dict):
            continue
        sess = live.get(str(h.get("tmux") or "")) or {}
        busy = bool(sess.get("busy") or agents_tmux.title_busy(str(sess.get("title") or "")))
        extras.extend(helper_thread_messages(h, i, busy))
    try:
        for sm in rosterlib.load_swarm_msgs(slug):
            if not sm.get("helper") or sm.get("role") != "assistant":
                continue
            extras.append(
                {
                    "role": "assistant",
                    "text": sm.get("text"),
                    "helper": True,
                    "thread": sm.get("thread") or "",
                    "task": sm.get("task") or "",
                    "n": sm.get("n") or helper_number({"slug": sm.get("thread")}, 0),
                    "at": sm.get("at"),
                }
            )
    except Exception:
        pass
    return merge_helper_answers(msgs, extras)


def archive_helper_answer(parent_slug: str, h: dict, idx: int = 0) -> None:
    """Keep a finished extra-agent answer in the parent chat after the helper leaves."""
    if not parent_slug or not isinstance(h, dict):
        return
    for m in helper_thread_messages(h, idx, busy=False):
        if m.get("role") != "assistant" or not m.get("text"):
            continue
        try:
            rosterlib.remember_swarm_msg(
                parent_slug,
                "assistant",
                str(m.get("text") or ""),
                helper=True,
                thread=str(m.get("thread") or h.get("slug") or ""),
                task=str(m.get("task") or h.get("task") or ""),
                n=m.get("n") or helper_number(h, idx),
            )
        except Exception:
            pass
        break


def live_busy(win: dict) -> bool:
    if not win:
        return False
    if win.get("tmux"):
        sess = next(
            (s for s in agents_tmux.list_sessions(include_helpers=True) if s.get("tmux") == win["tmux"]),
            None,
        )
        if sess is not None:
            return bool(sess.get("busy"))
    title = win.get("title") or ""
    return bool(win.get("busy")) or agents_tmux.title_busy(title)


# Elapsed "Bezig" clock. Lives on the server so closing a chat cannot reset it.
_busy_since: dict[str, float] = {}
_busy_idle_since: dict[str, float] = {}
_stopped_until: dict[str, float] = {}
_BUSY_IDLE_GRACE = 8.0
_STOP_HOLD = 2.8
_MAX_BUSY_HINT = 3 * 3600


def mark_stopped(slug: str, hold: float = _STOP_HOLD) -> None:
    """User hit Stop — report Ready immediately even if the pane is winding down."""
    key = str(slug or "").strip()
    if not key:
        return
    _stopped_until[key] = time.time() + max(0.4, float(hold))
    _busy_since.pop(key, None)
    _busy_idle_since.pop(key, None)


def is_just_stopped(slug: str) -> bool:
    key = str(slug or "").strip()
    if not key:
        return False
    return time.time() < float(_stopped_until.get(key) or 0)


def clear_last_submit(slug: str) -> None:
    key = str(slug or "").strip()
    if not key:
        return
    try:
        rost = rosterlib.load_roster()
        meta = (rost.get("agents") or {}).get(key)
        if meta and meta.get("last_submit_at") is not None:
            meta["last_submit_at"] = None
            rosterlib.save_roster(rost)
    except Exception:
        pass


def parse_ts(val) -> float | None:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        n = float(val)
        return n / 1000.0 if n > 1e12 else n
    s = str(val).strip()
    if not s:
        return None
    try:
        n = float(s)
        return n / 1000.0 if n > 1e12 else n
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


_LAST_SUBMIT_HOLD = 6.0


def last_submit_hold(meta: dict | None, now: float | None = None) -> bool:
    """Stay busy for a couple of seconds after Send so the title can catch up."""
    if not meta:
        return False
    ts = parse_ts(meta.get("last_submit_at"))
    if ts is None:
        return False
    age = (time.time() if now is None else now) - ts
    return 0 <= age < _LAST_SUBMIT_HOLD


def _hint_start(hint, now: float) -> float | None:
    h = parse_ts(hint)
    if h is None:
        return None
    age = now - h
    if 0 <= age < _MAX_BUSY_HINT:
        return h
    return None


def track_busy(key: str, busy: bool, hint=None) -> float | None:
    """Remember when this bot/helper became busy. Brief idle flicker keeps the clock."""
    if not key:
        return None
    now = time.time()
    if busy:
        _busy_idle_since.pop(key, None)
        if key not in _busy_since:
            _busy_since[key] = _hint_start(hint, now) or now
        return _busy_since[key]
    if key not in _busy_since:
        return None
    idle_from = _busy_idle_since.setdefault(key, now)
    if now - idle_from < _BUSY_IDLE_GRACE:
        return _busy_since[key]
    _busy_since.pop(key, None)
    _busy_idle_since.pop(key, None)
    return None


def busy_payload(key: str, busy: bool, hint=None) -> dict:
    started = track_busy(key, busy, hint)
    if not started:
        return {"busy_since": None, "busy_for": 0}
    return {"busy_since": started, "busy_for": max(0, int(time.time() - started))}


def attach_busy_times(wins: list[dict], rost: dict | None = None) -> list[dict]:
    rost = rost or rosterlib.load_roster()
    agents = rost.get("agents") or {}
    for w in wins:
        slug = w.get("slug") or ""
        if not slug:
            try:
                wid = int(w.get("id"))
            except (TypeError, ValueError):
                wid = None
            tmux = w.get("tmux") or ""
            for s, meta in agents.items():
                try:
                    mid = int(meta["window_id"]) if meta.get("window_id") is not None else None
                except (TypeError, ValueError):
                    mid = None
                if wid is not None and mid == wid:
                    slug = s
                    break
                if tmux and meta.get("tmux") == tmux:
                    slug = s
                    break
        meta = agents.get(slug) or {}
        key = slug or w.get("tmux") or str(w.get("id") or "")
        tmux = str(w.get("tmux") or meta.get("tmux") or "")
        live = None
        if tmux:
            live = next(
                (s for s in agents_tmux.list_sessions(include_helpers=True) if s.get("tmux") == tmux),
                None,
            )
        if live is not None:
            busy = bool(live.get("busy"))
            w["busy"] = busy
            if live.get("activity"):
                w["activity"] = live.get("activity")
        else:
            busy = bool(w.get("busy")) or agents_tmux.title_busy(w.get("title") or "")
        if is_just_stopped(slug):
            busy = False
            w["busy"] = False
            w["activity"] = "Ready"
        elif not busy and last_submit_hold(meta):
            busy = True
            w["busy"] = True
            act = (w.get("activity") or "").strip()
            if not act or act in {"Klaar", "Ready"}:
                w["activity"] = "Busy"
        if busy:
            act = (w.get("activity") or "").strip()
            if not act or act in {"Klaar", "Ready"}:
                w["activity"] = "Busy"
        clock = busy_payload(key, busy, meta.get("busy_since") or meta.get("last_submit_at"))
        w["busy_since"] = clock["busy_since"]
        w["busy_for"] = clock["busy_for"]
        if slug:
            w["slug"] = slug
        if meta.get("last_submit_at") is not None:
            w["last_submit_at"] = meta.get("last_submit_at")
    return wins


def last_user_text(win: dict, slug: str | None = None) -> str:
    slug = slug or slug_for_window(win) or ""
    meta = (rosterlib.load_roster().get("agents") or {}).get(slug) or {}
    msgs: list[dict] = []
    if meta.get("session_id"):
        pth = session_dir_by_id(str(meta["session_id"]))
        if pth:
            msgs = parse_chat(pth)
    if not msgs and (win.get("tmux") or meta.get("tmux")):
        msgs = messages_from_pane(win.get("tmux") or meta.get("tmux"))
    for m in reversed(msgs):
        if m.get("role") == "user" and m.get("text"):
            return str(m["text"])[:500]
    return ""


def is_file_notice(text: str) -> bool:
    t = (text or "").strip()
    return bool(re.match(r"^(?:Bestand(?: ontvangen)?:|📎\s)", t, re.I))


def file_basename(m) -> str:
    """Filename from a file card or 'Bestand ontvangen: /path/name' notice."""
    if isinstance(m, str):
        name, text, path = "", m, ""
    elif isinstance(m, dict):
        name = str(m.get("name") or "")
        text = str(m.get("text") or "")
        path = str(m.get("path") or "")
    else:
        return ""
    if name:
        return Path(name).name
    if path:
        return Path(path).name
    first = text.split("\n")[0].strip()
    hit = re.match(r"^(?:Bestand(?: ontvangen)?:|📎\s*)\s*(.+)$", first, re.I)
    if hit:
        return Path(hit.group(1).strip()).name
    if isinstance(m, dict) and m.get("meta") == "file" and first:
        return Path(first).name
    return ""


def is_new_question(win: dict, text: str, slug: str | None = None) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if FOLLOW_RE.match(t) or CONT_RE.match(t):
        return False
    if re.match(r"^(?:↩\s*)?Reply to this message\b", t, re.I):
        return False
    if is_file_notice(t):
        return False
    last = last_user_text(win, slug)
    if not last:
        return True
    stop = {
        "de", "het", "een", "van", "en", "in", "op", "te", "dat", "die", "is", "ik",
        "je", "niet", "voor", "met", "naar", "wat", "hoe", "the", "a", "to", "of",
    }
    a = {x for x in _tokens(t) if x not in stop}
    b = {x for x in _tokens(last) if x not in stop}
    if not a:
        return False
    return (len(a & b) / max(1, len(a))) < 0.35


def helper_slots_full(slug: str) -> bool:
    if not slug:
        return True
    live = {s["tmux"] for s in agents_tmux.list_sessions(include_helpers=True)}
    n = sum(1 for h in rosterlib.helpers_of(slug) if h.get("tmux") in live)
    return n >= 5


def classify_second(win: dict, text: str, slug: str | None = None) -> str:
    """While this chat is working: steer, queue, or start a 2nd agent."""
    t = (text or "").strip()
    if not t or is_file_notice(t):
        return "steer"
    if re.match(r"^(?:↩\s*)?Reply to this message\b", t, re.I):
        return "steer"
    if FOLLOW_RE.match(t) or CONT_RE.match(t):
        return "steer"
    if re.match(
        r"^(ja|nee|ok|oké|okay|yes|no|sure|go|stop|wait|and then|also|same|"
        r"prima|top|goed|klopt|wacht|en dan|en ook|daarna|ook nog|zelfde|nog even)\b",
        t,
        re.I,
    ):
        return "steer"
    if not is_new_question(win, t, slug):
        return "steer"
    if helper_slots_full(slug or ""):
        return "queue"
    return "helper"


def apply_second_choice(
    win: dict,
    slug: str,
    body: str,
    choice: str,
    chosen_by: str = "swarm",
) -> dict:
    """Run Swarm's pick (or the user's override) and remember it."""
    kind = (choice or "steer").strip().lower()
    if kind == "agent":
        kind = "helper"
    if kind not in {"steer", "helper", "queue"}:
        kind = "steer"
    who = (chosen_by or "swarm").strip().lower() or "swarm"
    route = {
        "choice": kind,
        "text": (body or "")[:200],
        "at": time.time(),
        "chosen_by": who,
        "qid": "",
        "hid": "",
    }
    if kind == "steer":
        extra = steer_into_chat(win, slug, body, interrupt_first=False)
        rosterlib.set_last_route(slug, route)
        return {
            "ok": True,
            "via": "steer",
            "choice": "steer",
            "chosen_by": who,
            "helper": False,
            "queued": False,
            "inbox": False,
            "text": extra.get("text") or body,
            "route": route,
            "routed": [],
        }
    if kind == "helper":
        extra = start_helper(win, slug, body)
        hid = str(extra.get("slug") or extra.get("id") or extra.get("tmux") or "")
        route["hid"] = hid
        rosterlib.set_last_route(slug, route)
        return {
            "ok": True,
            "via": "helper",
            "choice": "helper",
            "chosen_by": who,
            "helper": True,
            "queued": False,
            "inbox": False,
            "item": extra,
            "crew": crew_for_slug(slug),
            "queue": rosterlib.public_queue(slug),
            "queued_n": len(rosterlib.load_queue(slug)),
            "route": route,
            "routed": [],
            **{k: extra.get(k) for k in ("tmux", "slug") if extra.get(k)},
        }
    item = rosterlib.enqueue(
        slug, body, source="user", hold=False, choice="queue", chosen_by=who
    )
    route["qid"] = str(item.get("id") or "")
    rosterlib.set_last_route(slug, route)
    return {
        "ok": True,
        "via": "inbox",
        "choice": "queue",
        "chosen_by": who,
        "helper": False,
        "queued": True,
        "inbox": True,
        "item": item,
        "queue": rosterlib.public_queue(slug),
        "queued_n": len(rosterlib.load_queue(slug)),
        "route": route,
        "routed": [],
    }


def decorate_roster(pub: dict) -> dict:
    raw = rosterlib.load_roster()
    live = {s["tmux"]: s for s in agents_tmux.list_sessions(include_helpers=True)}
    by = {a["slug"]: a for a in pub.get("agents") or []}
    for slug, meta in (raw.get("agents") or {}).items():
        a = by.get(slug)
        if not a:
            continue
        hs = meta.get("helpers") or []
        a["helpers"] = len(hs)
        a["helpers_busy"] = sum(1 for h in hs if (live.get(h.get("tmux")) or {}).get("busy"))
        try:
            a["queued"] = len(rosterlib.load_queue(slug))
        except Exception:
            a["queued"] = 0
        a["crew"] = crew_for_slug(slug, meta)
        main = next((c for c in a["crew"] if not c.get("helper")), None)
        if main and main.get("busy_since"):
            a["busy_since"] = main["busy_since"]
            a["busy_for"] = main.get("busy_for") or 0
        else:
            a["busy_since"] = None
            a["busy_for"] = 0
    return pub


def spoken_brief() -> str:
    """Short spoken status of the whole crew — for Auto / drive voice."""
    wins = attach_busy_times(list_windows_cached())
    rost = decorate_roster(rosterlib.public_roster(wins, window_label))
    busy: list[str] = []
    ready: list[str] = []
    by_slug = {w.get("slug"): w for w in wins if w.get("slug")}
    for a in rost.get("agents") or []:
        name = str(a.get("label") or a.get("slug") or "bot")
        w = by_slug.get(a.get("slug"))
        main = next((c for c in (a.get("crew") or []) if not c.get("helper")), None)
        if (w and w.get("busy")) or (main and main.get("busy")):
            busy.append(name)
        else:
            ready.append(name)
    if not busy and not ready:
        return "Ik zie nog geen bots."
    if not busy:
        if len(ready) == 1:
            return f"{ready[0]} is klaar."
        return f"Alle {len(ready)} bots zijn klaar."
    if len(busy) == 1:
        rest = " De rest is klaar." if ready else ""
        return f"{busy[0]} is nog bezig.{rest}"
    head = ", ".join(busy[:-1]) + " en " + busy[-1]
    return f"{head} zijn nog bezig."


def ensure_shared_memory() -> None:
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not MEMORY_FILE.exists():
        MEMORY_FILE.write_text(
            "# Shared memory\n\nEvery bot reads this file (`SHARED.md` in its folder).\n"
            "Short facts. No passwords.\n\n## Recent\n",
            encoding="utf-8",
        )


def remember_user(label: str, text: str) -> None:
    text = (text or "").strip()
    if not text or text.startswith("[Swarm extra]") or text.startswith("[Overleg"):
        return
    ensure_shared_memory()
    stamp = time.strftime("%Y-%m-%d %H:%M")
    line = f"- {stamp} · {label}: {text.replace(chr(10), ' ')[:180]}"
    try:
        raw = MEMORY_FILE.read_text(encoding="utf-8")
    except Exception:
        raw = "# Shared memory\n\n## Recent\n"
    if "## Recent" not in raw:
        raw += "\n## Recent\n"
    head, rest = raw.split("## Recent", 1)
    old = [ln for ln in rest.splitlines() if ln.strip() and ln.strip() != "## Recent"]
    body = "\n".join([line] + old[:79]) + "\n"
    MEMORY_FILE.write_text(head + "## Recent\n" + body, encoding="utf-8")


def short_job(text: str, fallback: str = "Agent") -> str:
    t = re.sub(r"^\[(?:Swarm extra|Loop[^\]]*)\]:?\s*", "", (text or "").strip(), flags=re.I)
    t = re.sub(r"\s+", " ", t)
    words = [w for w in t.split() if w]
    if not words:
        return fallback
    out: list[str] = []
    for w in words:
        trial = " ".join(out + [w])
        if out and len(trial) > 22:
            break
        out.append(w)
        if len(out) >= 3:
            break
    return " ".join(out) or fallback


def crew_for_slug(slug: str, meta: dict | None = None) -> list[dict]:
    rost = rosterlib.load_roster()
    meta = meta or (rost.get("agents") or {}).get(slug) or {}
    live = {s["tmux"]: s for s in agents_tmux.list_sessions(include_helpers=True)}
    items = []
    parent = live.get(meta.get("tmux") or "")
    title = meta.get("title") or (parent or {}).get("title") or ""
    if parent is not None:
        busy = bool(parent.get("busy"))
    else:
        busy = agents_tmux.title_busy(title)
    if not busy and last_submit_hold(meta):
        busy = True
    act = (parent or {}).get("activity") or agents_tmux.activity_from_text(title, busy)
    act = agents_tmux.clean_activity(act, busy)
    main_clock = busy_payload(
        slug or "main",
        busy,
        meta.get("busy_since") or meta.get("last_submit_at"),
    )
    items.append(
        {
            "id": slug or "main",
            "name": meta.get("label") or slug,
            "busy": busy,
            "activity": act,
            "helper": False,
            **main_clock,
        }
    )
    for i, h in enumerate(rosterlib.helpers_of(slug)):
        tmux = str(h.get("tmux") or "")
        sess = live.get(tmux) or {}
        hb = bool(sess.get("busy")) or agents_tmux.title_busy(str(h.get("title") or sess.get("title") or ""))
        recent = False
        try:
            started = float(h.get("busy_since") or h.get("last_submit_at") or 0)
            recent = started > 0 and (time.time() - started) < 90
        except (TypeError, ValueError):
            recent = False
        if not hb and not recent:
            continue
        hid = str(h.get("slug") or tmux or "")
        n = helper_number(h, i)
        task = str(h.get("task") or "")[:160]
        act = sess.get("activity") or "Busy"
        try:
            desc = describe_live(act, "", "", "")
            line = desc.get("headline") or desc.get("line") or act
        except Exception:
            line = act
        items.append(
            {
                "id": hid,
                "name": f"Agent {n}",
                "n": n,
                "task": task,
                "summary": task,
                "line": line,
                "busy": True,
                "activity": act,
                "helper": True,
                **busy_payload(hid or "helper", True, h.get("busy_since") or h.get("last_submit_at")),
            }
        )
    return items


_last_keep = 0.0
_last_typed: dict[str, tuple[str, float]] = {}
_last_loops = 0.0
_wiped_extras = False


def _owned_helper_tmux() -> set[str]:
    owned: set[str] = set()
    try:
        rost = rosterlib.load_roster()
    except Exception:
        return owned
    for meta in (rost.get("agents") or {}).values():
        for h in meta.get("helpers") or []:
            if not isinstance(h, dict):
                continue
            name = str(h.get("tmux") or "").strip()
            slug = str(h.get("slug") or "").strip()
            if name:
                owned.add(name)
            if slug:
                owned.add(agents_tmux.PREFIX + slug)
    return owned


def _roster_keep_ids() -> tuple[set[str], set[str], set[str]]:
    """tmux names, session ids, slugs that must stay alive."""
    tmux_ok: set[str] = set()
    sid_ok: set[str] = set()
    slug_ok: set[str] = set()
    try:
        rost = rosterlib.load_roster()
    except Exception:
        return tmux_ok, sid_ok, slug_ok
    for slug, meta in (rost.get("agents") or {}).items():
        if not isinstance(meta, dict):
            continue
        slug_ok.add(str(slug))
        name = str(meta.get("tmux") or "").strip()
        if name:
            tmux_ok.add(name)
        sid = str(meta.get("session_id") or "").strip()
        if sid:
            sid_ok.add(sid)
        for h in meta.get("helpers") or []:
            if not isinstance(h, dict):
                continue
            hs = str(h.get("slug") or "").strip()
            ht = str(h.get("tmux") or "").strip()
            hid = str(h.get("session_id") or "").strip()
            if hs:
                slug_ok.add(hs)
            if ht:
                tmux_ok.add(ht)
            if hid:
                sid_ok.add(hid)
    return tmux_ok, sid_ok, slug_ok


def prune_orphan_sessions() -> None:
    """Drop forgotten heavy-* tmux and extra grok processes not on the roster."""
    tmux_ok, sid_ok, slug_ok = _roster_keep_ids()
    if not slug_ok:
        return
    try:
        listed = subprocess.check_output(
            [agents_tmux.TMUX, "ls", "-F", "#{session_name}"],
            text=True,
            timeout=4,
        )
    except Exception:
        listed = ""
    for name in listed.splitlines():
        name = name.strip()
        if not name.startswith(agents_tmux.PREFIX):
            continue
        slug = agents_tmux.slug_of_session(name)
        if name in tmux_ok or slug in slug_ok:
            continue
        if name.startswith("heavy-h--") or slug.startswith("h--"):
            continue
        try:
            agents_tmux.kill(name)
            print("prune tmux", name, flush=True)
        except Exception:
            pass
    try:
        ps = subprocess.check_output(["ps", "ax", "-o", "pid=,command="], text=True, timeout=4)
    except Exception:
        return
    work = str(agents_tmux.WORK_ROOT)
    for line in ps.splitlines():
        line = line.strip()
        if "grok --session-id" not in line or work not in line:
            continue
        try:
            pid = int(line.split(None, 1)[0])
        except (TypeError, ValueError):
            continue
        sid = ""
        if "--session-id" in line:
            sid = line.split("--session-id", 1)[1].split()[0].strip()
        if sid and sid in sid_ok:
            continue
        cwd = ""
        if "--cwd" in line:
            cwd = line.split("--cwd", 1)[1].strip()
        slug = Path(cwd).name if cwd else ""
        try:
            os.kill(pid, signal.SIGTERM)
            print("prune grok", pid, (sid or "")[:13], slug, flush=True)
        except Exception:
            pass


def prune_orphan_helpers() -> None:
    """Kill helper tmux sessions that no roster bot owns. Keep assigned agents."""
    owned = _owned_helper_tmux()
    try:
        rows = agents_tmux.list_sessions(include_helpers=True)
    except Exception:
        return
    for s in rows:
        name = str(s.get("tmux") or "")
        slug = str(s.get("slug") or "")
        if not (name.startswith("heavy-h--") or slug.startswith("h--")):
            continue
        if name in owned or (agents_tmux.PREFIX + slug) in owned:
            continue
        try:
            agents_tmux.kill(name)
        except Exception:
            pass


def wipe_extra_agents() -> None:
    """Back-compat name: only prune orphans, never wipe assigned helpers."""
    global _wiped_extras
    prune_orphan_helpers()
    _wiped_extras = True


def helper_prompt(label: str, text: str, last: str = "") -> str:
    body = (text or "").strip()
    who = (label or "deze bot").strip()
    tail = f" met: {last[:240]}" if last else ""
    return (
        f"[Swarm extra] {body}\n"
        f"---\n"
        f"You are an extra agent in the same chat as '{who}'. "
        f"That bot is busy{tail}. Do not interrupt that bot. "
        "Handle this request yourself in this folder. Reply in this chat."
    )


def helper_already_has_task(session_id: str, task: str) -> bool:
    sid = str(session_id or "").strip()
    needle = _norm_txt(task)[:40]
    if not sid or not needle:
        return False
    try:
        pth = session_dir_by_id(sid)
        if not pth:
            return False
        for m in parse_chat(pth):
            if m.get("role") == "user" and needle in _norm_txt(m.get("text")):
                return True
    except Exception:
        return False
    return False


def revive_helper(parent_slug: str, parent_meta: dict, h: dict) -> dict:
    """Bring a roster helper's tmux back. Resume its Grok session when possible."""
    sid = str(h.get("session_id") or "") or None
    cwd = parent_meta.get("cwd") or str(agents_tmux.WORK_ROOT / parent_slug)
    slug = str(h.get("slug") or "") or f"h--{parent_slug}-1"
    name = str(h.get("tmux") or (agents_tmux.PREFIX + slug))
    live = {s["tmux"] for s in agents_tmux.list_sessions(include_helpers=True)}
    if name in live:
        return {**h, "tmux": name, "slug": slug, "session_id": sid or h.get("session_id")}
    if sid:
        try:
            release_session_locks(sid)
        except Exception:
            pass
    info = agents_tmux.spawn(
        label=slug,
        cwd=cwd,
        ai=parent_meta.get("ai") or "grok",
        sid=sid,
        model=parent_meta.get("model") or "",
        resume=bool(sid),
    )
    agents_tmux._list_cache = (0.0, [])
    rosterlib.update_helper(
        parent_slug,
        slug,
        tmux=info.get("tmux") or name,
        session_id=info.get("session_id") or sid,
    )
    return {**h, **info, "slug": slug}


def drop_idle_helpers() -> None:
    """Finished extra agents leave. No leftover 'Agent 1 klaar' in the chat."""
    try:
        rost = rosterlib.load_roster()
        live = {s["tmux"]: s for s in agents_tmux.list_sessions(include_helpers=True)}
    except Exception:
        return
    now = time.time()
    for slug, meta in (rost.get("agents") or {}).items():
        if slug.startswith("h--") or (meta or {}).get("helper"):
            continue
        hs = list((meta or {}).get("helpers") or [])
        if not hs:
            continue
        keep = []
        for h in hs:
            if not isinstance(h, dict):
                continue
            name = str(h.get("tmux") or "")
            sess = live.get(name) if name else None
            busy = bool(
                sess
                and (sess.get("busy") or agents_tmux.title_busy(str(sess.get("title") or "")))
            )
            created = parse_ts(h.get("created"))
            young = created is not None and 0 <= now - created < 20
            if busy or (young and sess):
                keep.append(h)
                continue
            if name and sess:
                try:
                    agents_tmux.kill(name)
                except Exception:
                    pass
        if len(keep) != len(hs):
            try:
                rosterlib.replace_helpers(slug, keep)
            except Exception:
                pass


def revive_helpers() -> None:
    """Respawn assigned extra agents that lost their tmux. Send the task if never received."""
    try:
        rost = rosterlib.load_roster()
        live = {s["tmux"] for s in agents_tmux.list_sessions(include_helpers=True)}
    except Exception:
        return
    for slug, meta in (rost.get("agents") or {}).items():
        if slug.startswith("h--") or (meta or {}).get("helper"):
            continue
        for h in list((meta or {}).get("helpers") or []):
            if not isinstance(h, dict):
                continue
            task = str(h.get("task") or "").strip()
            if not task:
                continue
            name = str(h.get("tmux") or "")
            if helper_already_has_task(str(h.get("session_id") or ""), task):
                continue
            if name and name in live:
                continue
            try:
                info = revive_helper(slug, meta or {}, h)
            except Exception as exc:
                print("revive helper", h.get("slug"), exc, flush=True)
                continue
            tmux = str(info.get("tmux") or "")
            if not tmux:
                continue
            if helper_already_has_task(str(info.get("session_id") or h.get("session_id") or ""), task):
                continue
            label = str((meta or {}).get("label") or slug)
            threading.Thread(
                target=_send_when_ready,
                args=(tmux, helper_prompt(label, task)),
                daemon=True,
            ).start()


def tick_loops() -> None:
    global _last_loops
    now = time.time()
    if now - _last_loops < 20:
        return
    _last_loops = now
    rost = rosterlib.load_roster()
    for slug, meta in (rost.get("agents") or {}).items():
        if slug.startswith("h--") or meta.get("helper"):
            continue
        try:
            packed = {**meta, "slug": slug}
            msgs = messages_for_meta(packed)
            rosterlib.ingest_loop_texts(
                slug, [m.get("text") for m in msgs if m.get("role") == "user"]
            )
        except Exception:
            pass
    for slug, loop in rosterlib.due_swarm_loops():
        try:
            win = resolve_delivery({"slug": slug})
            if not win:
                continue
            # Still working: leave due, try again next tick. Do not mark fired.
            if live_busy(win):
                continue
            name = loop.get("name") or "Loop"
            prompt = loop.get("prompt") or ""
            dispatch_text(win, f"[Loop · {name}]: {prompt}", True)
            rosterlib.mark_loop_fired(slug, loop.get("id") or "")
        except Exception:
            pass


def keep_bots_alive() -> None:
    global _last_keep
    try:
        unstick_stalled()
    except Exception as exc:
        print("unstick", exc, flush=True)
    prune_orphan_helpers()
    try:
        prune_orphan_sessions()
    except Exception as exc:
        print("prune sessions", exc, flush=True)
    now = time.time()
    if now - _last_keep < 30:
        return
    _last_keep = now
    try:
        drop_idle_helpers()
    except Exception as exc:
        print("drop idle helpers", exc, flush=True)
    try:
        revive_helpers()
    except Exception as exc:
        print("revive helpers", exc, flush=True)
    rost = rosterlib.load_roster()
    live = {s["tmux"] for s in agents_tmux.list_sessions(include_helpers=True)}
    for slug, meta in (rost.get("agents") or {}).items():
        if slug.startswith("h--") or meta.get("helper"):
            continue
        if rosterlib.is_forgotten(slug=slug, tmux=str(meta.get("tmux") or ""), session_id=str(meta.get("session_id") or "")):
            continue
        name = str(meta.get("tmux") or "")
        if name and name in live:
            if not agents_tmux.running_ai(name):
                try:
                    agents_tmux.respawn(
                        name,
                        ai=meta.get("ai") or "grok",
                        cwd=meta.get("cwd") or "",
                        sid=meta.get("session_id") or None,
                        model=meta.get("model") or "",
                    )
                    print("respawn dead", name, flush=True)
                except Exception as exc:
                    print("respawn", name, exc, flush=True)
            continue
        try:
            ensure_hidden_tmux(slug, meta)
        except Exception:
            pass


def session_id_from_nicks(meta: dict) -> str:
    """Restore session_id from this window only. Never match by label (that steals chats)."""
    if not meta:
        return ""
    have = str(meta.get("session_id") or "").strip()
    slug = str(meta.get("slug") or "").strip()
    if have:
        owner = session_owner(have)
        if owner and slug and owner != slug:
            return ""
        return have
    try:
        nicks = rosterlib.load_nicks()
    except Exception:
        return ""
    try:
        wid = str(int(meta["window_id"])) if meta.get("window_id") is not None else ""
    except (TypeError, ValueError):
        wid = str(meta.get("window_id") or "")
    hit = (nicks.get("by_window") or {}).get(wid) or {}
    sid = str(hit.get("session_id") or "").strip()
    if not sid:
        return ""
    owner = session_owner(sid)
    if owner and slug and owner != slug:
        return ""
    return sid


def _persist_session(slug: str | None, sid: str) -> bool:
    """Bind session to this bot only if it is free or already ours."""
    if not slug or not sid:
        return False
    owner = session_owner(sid)
    if owner and owner != slug:
        return False
    rost = rosterlib.load_roster()
    if slug not in rost.get("agents", {}):
        return False
    if rost["agents"][slug].get("session_id") == sid:
        return True
    rost["agents"][slug]["session_id"] = sid
    rosterlib.save_roster(rost)
    try:
        rosterlib.remember_nick(rost["agents"][slug], rost["agents"][slug].get("label") or slug, {"session_id": sid})
    except Exception:
        pass
    return True


def messages_for_meta(meta: dict, allow_cwd_fallback: bool = True) -> list[dict]:
    if not meta:
        return []
    ai = rosterlib.normalize_ai(meta.get("ai"))
    slug = meta.get("slug") or ""
    cwd = meta.get("cwd") or ""
    if not cwd and slug:
        guess = agents_tmux.WORK_ROOT / str(slug)
        if guess.is_dir():
            cwd = str(guess)
    sid = str(meta.get("session_id") or "").strip() or session_id_from_nicks(meta)
    if sid:
        owner = session_owner(sid)
        if owner and slug and owner != slug:
            sid = ""
        else:
            meta["session_id"] = sid
            _persist_session(slug, sid)
    if ai == "claude" and sid:
        p = claude_history_path(cwd, sid)
        if p:
            msgs = parse_claude_chat(p)
            if msgs:
                return msgs
    if ai == "codex" and cwd_is_private(slug, cwd):
        msgs = parse_codex_chat(cwd)
        if msgs:
            return msgs
    bound = bool(sid)
    if sid:
        pth = session_dir_by_id(sid)
        if pth:
            msgs = parse_chat(pth)
            if msgs:
                return msgs
    # A bound session that parsed empty is a rewrite/miss — never steal a
    # helper session from the shared workspace (that blanks the chat for ~20s).
    if (not bound) and allow_cwd_fallback and cwd_is_private(slug, cwd):
        pth = latest_grok_session_for_cwd(cwd)
        owner = session_owner(pth.name) if pth else None
        if pth and (not owner or owner == slug):
            msgs = parse_chat(pth)
            if msgs:
                _persist_session(slug, pth.name)
                return msgs
    if meta.get("tmux"):
        return messages_from_pane(str(meta["tmux"]))
    return []


def _helper_parent_key(slug: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in (slug or "bot")).strip("-")[:24] or "bot"


def _task_key(text: str) -> str:
    t = unwrap_helper_user(text or "")
    return re.sub(r"\s+", " ", t).strip().lower()[:80]


def bind_helper_sessions(slug: str, main_sid: str, items: list[dict], cwd: str) -> list[dict]:
    """Fill empty helper session_ids from this bot's workspace. Do not add new cards."""
    if not items or not slug or not cwd_is_private(slug, cwd):
        return items
    unused: list[tuple[str, str, str]] = []
    have = {str(h.get("session_id") or "") for h in items if h.get("session_id")}
    for d in session_dirs_for_cwd(cwd):
        if not d.name or d.name == main_sid or d.name in have:
            continue
        owner = session_owner(d.name)
        if owner and owner != slug and not str(owner).startswith(("h--", "sess-")):
            continue
        msgs = parse_chat(d)
        task = ""
        for m in msgs:
            if m.get("role") == "user" and m.get("text"):
                task = unwrap_helper_user(m["text"])
                if task:
                    break
        unused.append((d.name, _task_key(task), task))
    for h in items:
        if h.get("session_id"):
            continue
        key = _task_key(h.get("task") or "")
        if not key:
            continue
        hit = None
        for i, (sid, tkey, task) in enumerate(unused):
            if tkey and (key in tkey or tkey in key):
                hit = i
                break
        if hit is None:
            continue
        sid, _, task = unused.pop(hit)
        h["session_id"] = sid
        if task and not h.get("task"):
            h["task"] = task[:400]
        have.add(sid)
    for sid, _, task in unused:
        items.append(
            {
                "slug": f"sess-{sid[:8]}",
                "tmux": "",
                "session_id": sid,
                "task": (task or "")[:400],
            }
        )
    return items


def dedupe_helper_items(items: list[dict]) -> list[dict]:
    """One card per extra job. Merge sess-* leftovers onto the live helper."""
    out: list[dict] = []

    def same(a: dict, b: dict) -> bool:
        for field in ("session_id", "slug", "tmux"):
            va, vb = str(a.get(field) or ""), str(b.get(field) or "")
            if va and vb and va == vb:
                return True
        ka, kb = _task_key(a.get("task") or ""), _task_key(b.get("task") or "")
        return bool(ka and kb and (ka == kb or ka in kb or kb in ka))

    def merge(a: dict, b: dict) -> dict:
        m = dict(a)
        for k, v in b.items():
            if not v:
                continue
            if k == "slug" and str(m.get("slug") or "").startswith("h--") and str(v).startswith("sess-"):
                continue
            m[k] = v
        if str(m.get("slug") or "").startswith("sess-") and str(b.get("slug") or "").startswith("h--"):
            m["slug"] = b["slug"]
        return m

    for src in items:
        if not isinstance(src, dict):
            continue
        if not (src.get("session_id") or src.get("slug") or src.get("tmux")):
            continue
        found = False
        for i, ex in enumerate(out):
            if same(ex, src):
                out[i] = merge(ex, src)
                found = True
                break
        if not found:
            out.append(dict(src))
    return out


def extra_threads_for(slug: str, main_sid: str, meta: dict, live_rows: list[dict] | None = None) -> list[dict]:
    """Roster helpers + live tmux extras. Bind file sessions; never invent extra cards."""
    items: list[dict] = [dict(h) for h in (meta.get("helpers") or []) if isinstance(h, dict)]
    key = _helper_parent_key(slug)
    prefix = f"{agents_tmux.HELPER_PREFIX}{key}-"
    if live_rows is None:
        try:
            live_rows = agents_tmux.list_sessions(include_helpers=True)
        except Exception:
            live_rows = []
    for s in live_rows or []:
        sl = str(s.get("slug") or "")
        if sl.startswith(prefix):
            items.append(
                {
                    "slug": sl,
                    "tmux": s.get("tmux") or "",
                    "session_id": s.get("session_id") or "",
                    "task": "",
                }
            )
    cwd = str(meta.get("cwd") or "")
    if not cwd and slug:
        guess = agents_tmux.WORK_ROOT / str(slug)
        if guess.is_dir():
            cwd = str(guess)
    items = rosterlib._union_helpers(items, [], keep=24)
    items = bind_helper_sessions(slug, main_sid, items, cwd)
    return dedupe_helper_items(items)


_collect_cache: dict[str, tuple[float, dict]] = {}

TOOL_NL = {
    "read_file": "Reading",
    "search_replace": "Writing",
    "write": "Writing",
    "run_terminal_command": "Terminal",
    "web_search": "Searching web",
    "web_fetch": "Reading web",
    "grep": "Searching",
    "list_dir": "Reading",
    "todo_write": "Planning",
    "image_gen": "Image",
    "image_edit": "Image",
    "get_command_or_subagent_output": "Waiting",
    "spawn_subagent": "Agent",
    "use_tool": "Tool",
}


_LIVE_HEAD = {
    "Reading": "Reading the code",
    "Writing": "Editing a file",
    "Terminal": "Running a command",
    "Searching": "Searching the code",
    "Searching web": "Searching the web",
    "Reading web": "Reading a page",
    "Browser": "Looking in the browser",
    "Planning": "Planning next steps",
    "Image": "Working on an image",
    "Waiting": "Waiting on a task",
    "Thinking": "Thinking through the approach",
    "Busy": "Working on your request",
    "Agent": "Starting an extra agent",
    "Tool": "Using a tool",
    "Leest": "Reading the code",
    "Schrijft": "Editing a file",
    "Zoekt": "Searching the code",
    "Zoekt web": "Searching the web",
    "Leest web": "Reading a page",
    "Plant": "Planning next steps",
    "Beeld": "Working on an image",
    "Wacht": "Waiting on a task",
    "Denkt": "Thinking through the approach",
    "Bezig": "Working on your request",
}


def _clean_live_target(raw: str) -> str:
    s = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not s:
        return ""
    s = re.sub(r"TOKEN=\$\([^)]*\)\s*", "", s)
    s = re.sub(r"(?i)\b[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API[_-]?KEY)=\S+\s*", "", s)
    s = re.sub(r"^cd\s+\S+\s+&&\s+", "", s)
    s = re.sub(r"^/Users/timgrootes/Projects/desktop-harness/\.venv/bin/python3(?:\.\d+)?\s+", "python ", s)
    s = re.sub(r"^python3(?:\.\d+)?\s+", "python ", s)

    def _host(m: re.Match) -> str:
        raw = m.group(0).rstrip(".,);]>\"'\\")
        try:
            u = urlparse(raw)
        except ValueError:
            return "web"
        return u.netloc or "web"

    s = re.sub(r"https?://\S+", _host, s)
    m = re.search(r"(/?(?:Users)/[^\s:]+|/[^\s:]+\.[A-Za-z0-9]{1,12})", s)
    if not m:
        m = re.search(r"(/[^\s:]+)", s)
    if m and "/" in m.group(1):
        name = Path(m.group(1).rstrip("/")).name
        if name and name not in {".", ".."} and not name.startswith("$"):
            if s.strip() == m.group(1).strip() or " " not in s.strip():
                return name
            return name
    return s.strip(" ·-—")


def _human_thought(text: str) -> str:
    t = strip_code_blocks(text or "")
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"\[Baas[^\]]*\]:\s*", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    if re.search(r"TOKEN=|[{}=<>]{2}|^\s*(def |import |from |curl |python|launchctl)", t):
        return ""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", t) if p.strip()]
    if not parts:
        return t
    if len(parts[0]) < 12:
        return " ".join(parts[:2]) if len(parts) > 1 else t
    return " ".join(parts[:2])


def _human_command(raw: str) -> str:
    sl = (raw or "").lower()
    if any(x in sl for x in ("pytest", "unittest", "test_")):
        return "Checking that everything still works"
    if "launchctl" in sl:
        return "Restarting the background app"
    if "curl" in sl or ":8790" in sl:
        return "Talking to Swarm"
    if any(x in sl for x in ("iconutil", "sips", ".icns")):
        return "Making the app icon"
    if sl.startswith("git ") or " git " in sl:
        return "Looking at git"
    if "grep" in sl or "rg " in sl:
        return "Searching the files"
    return "Running a terminal command"


def _grok_title_step(title: str) -> str:
    t = re.sub(r"^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏●○]\s*", "", title or "")
    t = re.sub(r"\s*[—–-]\s*grok.*$", "", t, flags=re.I)
    parts = [p.strip() for p in re.split(r"\s*[—–-]\s*", t) if p.strip()]
    keep = []
    for p in parts:
        low = p.lower().rstrip(".…")
        if low in {"thinking", "waiting", "preparing", "responding", "busy", "denkt", "bezig"}:
            continue
        if low.startswith("waiting for response"):
            continue
        if re.fullmatch(r"bot-\d+", low):
            continue
        if len(low) < 8:
            continue
        keep.append(p)
    step = keep[-1] if keep else ""
    return re.sub(r"[.…]+$", "", step).strip()


def _task_from_pane(pane: str) -> str:
    m = re.search(
        r"Task\s+(.+?)(?:\s+\(\d+\)|\s+\d+[smh]\b|\s*\[|$)",
        pane or "",
        re.I,
    )
    if not m:
        return ""
    t = re.sub(r"\s+", " ", m.group(1)).strip(" .")
    return t[:90] if len(t) >= 8 else ""


def _thought_from_pane(pane: str) -> str:
    bits = []
    for ln in (pane or "").splitlines():
        m = re.match(r"^\s*[┃│]\s+(.+?)\s*$", ln)
        if not m:
            continue
        bit = m.group(1).strip()
        if not bit or bit.startswith(("◆", "◈", "●", "▾")):
            continue
        bits.append(bit)
    if not bits:
        return ""
    return _human_thought(" ".join(bits))


def _grok_pane_tally(pane: str) -> str:
    bits = re.findall(
        r"((?:Searched|Read|Listed|Wrote|Edited|Ran)\s+\d+(?:\s+[A-Za-z]+){0,3})",
        pane or "",
        re.I,
    )
    out, seen = [], set()
    for bit in bits:
        key = bit.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(re.sub(r"\s+", " ", bit).strip())
    return ", ".join(out)


def _clean_live_question(raw: str) -> str:
    q = re.sub(r"\s+", " ", str(raw or "")).strip()
    q = re.sub(r"^\[(?:Baas|Boss|Overleg van)[^\]]*\]:\s*", "", q, flags=re.I)
    if re.search(r"<system-reminder>|Background task \"call-", q, re.I):
        return ""
    return q.strip()


def describe_live(activity: str, extra: str = "", last_user: str = "", last_asst: str = "") -> dict:
    act = agents_tmux.clean_activity(activity or "Busy", True)
    if act == "Ready":
        act = "Busy"
    head = _LIVE_HEAD.get(act, act)
    target = _clean_live_target(extra)
    thought = _human_thought(last_asst)
    if act == "Reading" and target:
        line = f"Reading {target}"
    elif act == "Writing" and target:
        line = f"Editing {target}"
    elif act == "Terminal":
        cmd = _human_command(extra or target)
        line = thought if thought and cmd == "Running a terminal command" else cmd
    elif act in {"Searching", "Searching web"} and target:
        line = f"Searching for {target}" if act == "Searching" else f"Searching the web for {target}"
    elif act == "Reading web" and target:
        line = f"Reading {target}"
    elif act == "Browser" and target:
        line = f"Opening {target}"
    elif act == "Planning" and target:
        line = f"Planning: {target}"
    elif act == "Image" and target:
        line = f"Image: {target}"
    elif thought:
        line = thought
    elif act == "Thinking":
        line = "Deciding the next step"
    elif act == "Waiting":
        line = "Waiting for a running task"
    elif act == "Busy":
        line = "Working in the terminal"
    elif target:
        line = target
    else:
        line = head
    q = _clean_live_question(last_user)
    return {"headline": head, "line": line, "on": q}


def _norm_txt(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()[:220]


def _swarm_ts(sm: dict) -> float | None:
    raw = sm.get("at")
    if not raw:
        return None
    try:
        if isinstance(raw, (int, float)):
            ts = float(raw)
            if ts > 1e12:
                ts /= 1000.0
            return ts
        text = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _swarm_age_s(sm: dict) -> float | None:
    ts = _swarm_ts(sm)
    if ts is None:
        return None
    return max(0.0, time.time() - ts)


def is_tool_msg(m: dict | None) -> bool:
    if not m or m.get("role") != "assistant":
        return False
    if m.get("meta") == "tool":
        return True
    text = str(m.get("text") or "").strip()
    return bool(
        re.match(
            r"^(Leest|Schrijft|Terminal|Zoekt|Zoekt web|Browser|Plant|Beeld|Leest web|Wacht|"
            r"Reading|Writing|Searching|Searching web|Reading web|Planning|Image|Waiting)\s*(·|$)",
            text,
            re.I,
        )
    )


def _is_file_msg(m: dict | None) -> bool:
    if not m:
        return False
    if m.get("meta") == "file":
        return True
    return is_file_notice(str(m.get("text") or ""))


def turn_open(msgs: list[dict] | None, live_waiting: bool = True) -> bool:
    """True while the last user question has no finished answer yet.

    Trailing tool rows after a prose answer only keep the turn open while
    Grok is actually working. Otherwise a finished reply plus leftover
    tools (SEO loop, file edits) leaves the chat on Bezig forever.
    """
    last_user = -1
    for i, m in enumerate(msgs or []):
        if m.get("helper") or _is_file_msg(m):
            continue
        if m.get("role") == "user":
            last_user = i
    if last_user < 0:
        return False
    last_prose = -1
    last_tool = -1
    for i, m in enumerate((msgs or [])[last_user + 1 :], last_user + 1):
        if m.get("helper") or m.get("role") != "assistant":
            continue
        if is_tool_msg(m):
            last_tool = i
        else:
            last_prose = i
    if last_prose < 0:
        return True
    if last_tool > last_prose:
        return bool(live_waiting)
    return False


def load_turn_msgs(win: dict | None, slug: str | None = None) -> list[dict]:
    slug = slug or slug_for_window(win or {}) or str((win or {}).get("slug") or "")
    meta = (rosterlib.load_roster().get("agents") or {}).get(slug) or {}
    msgs: list[dict] = []
    if meta.get("session_id"):
        pth = session_dir_by_id(str(meta["session_id"]))
        if pth:
            msgs = parse_chat(pth)
    if not msgs and ((win or {}).get("tmux") or meta.get("tmux")):
        msgs = messages_from_pane((win or {}).get("tmux") or meta.get("tmux"))
    if slug:
        have = {_norm_txt(m.get("text")) for m in msgs if m.get("role") == "user"}
        try:
            msgs = merge_swarm_users(msgs, rosterlib.load_swarm_msgs(slug), have)
        except Exception:
            pass
    return msgs


def turn_answered(win: dict | None, slug: str | None = None, msgs: list[dict] | None = None) -> bool:
    """True when the last real user question already has a prose reply."""
    rows = msgs if msgs is not None else load_turn_msgs(win, slug)
    if not rows:
        return False
    return not turn_open(rows, live_waiting=False)


def merge_swarm_users(msgs: list[dict], swarm: list[dict], have: set[str] | None = None) -> list[dict]:
    """Keep this bot's Swarm history. Dedup against the session; do not drop older turns."""
    have = set(have or [])
    out = list(msgs or [])
    for m in out:
        if m.get("role") != "user":
            continue
        key = _norm_txt(m.get("text"))
        if key:
            have.add(key)
    extra: list[dict] = []
    seen = set(have)
    for sm in swarm or []:
        if sm.get("role") != "user":
            continue
        if sm.get("meta") == "file" or is_file_notice(sm.get("text") or ""):
            continue
        key = _norm_txt(sm.get("text"))
        if not key:
            continue
        ts = _swarm_ts(sm) or 0.0
        if key in seen:
            if ts:
                for m in out:
                    if (
                        m.get("role") == "user"
                        and _norm_txt(m.get("text")) == key
                        and not m.get("at")
                    ):
                        m["at"] = sm.get("at")
            continue
        seen.add(key)
        extra.append(sm)
    if not extra:
        return out
    extra.sort(key=lambda m: _swarm_ts(m) or 0.0)
    session_ts = [t for t in (_swarm_ts(m) for m in out) if t]
    if not session_ts:
        now = time.time()
        old, fresh = [], []
        for sm in extra:
            ts = _swarm_ts(sm) or 0.0
            if ts and now - ts < 120:
                fresh.append(sm)
            else:
                old.append(sm)
        return old + out + fresh
    merged: list[dict] = []
    ei = 0
    for m in out:
        mts = _swarm_ts(m)
        while ei < len(extra):
            ets = _swarm_ts(extra[ei]) or 0.0
            if mts is None or ets > mts:
                break
            merged.append(extra[ei])
            ei += 1
        merged.append(m)
    merged.extend(extra[ei:])
    return merged


def last_jsonl_tail(path: Path, nbytes: int = 24000) -> list[dict]:
    if not path.is_file():
        return []
    try:
        data = path.read_bytes()[-nbytes:]
    except OSError:
        return []
    parts = data.split(b"\n")
    if data and not data.startswith(b"{") and parts:
        parts = parts[1:]
    out = []
    for line in parts:
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


_TUI_CHROME = re.compile(
    r"(?i)always[- ]?approve|enter:(queue|send)|shift\+tab|esc:cancel|"
    r"ctrl\+[a-z0-9]|grok\s+\d+(?:\.\d+)?|auto-approve|"
    r"type a message|queued\s*$|\[↓\]|\[↑\]|\[\+\]|"
    r"mode\s*\||\|\s*esc:|\|\s*ctrl\+|"
    r"waiting for response|esc to interrupt|"
    r"\b\d+[kKmM]\s*/\s*\d+[kKmM]\b"
)
_BOX_RE = re.compile(r"[╭╰│─╮╯┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬▀▄■□▪▬━┃┏┓┗┛╴╵╶╷]+")
_PROMPT_RE = re.compile(r"^\s*[>|❯]\s")
_STATUS_RE = re.compile(r"^:?\d+[kKmM]?\s*\[")
_WORK_LINE = re.compile(
    r"(?i)(read_file|search_replace|run_terminal|web_search|web_fetch|"
    r"list_dir|todo_write|[●◆◈✔✗] |error:|traceback|running |"
    r"calling |tool |wrote |reading |grep |searched |inspect )"
)


def _strip_ansi(s: str) -> str:
    s = (s or "").replace("\x1b", "")
    s = re.sub(r"\[[0-9;?]*[A-Za-z]", "", s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return s.replace("\r", "").rstrip()


def _is_tui_chrome(s: str) -> bool:
    t = (s or "").strip()
    if not t:
        return True
    core = _BOX_RE.sub("", t).strip(" |")
    if not core:
        return True
    if _TUI_CHROME.search(t) or _TUI_CHROME.search(core):
        return True
    if _STATUS_RE.match(t):
        return True
    if re.fullmatch(r"[:\d\s\[\]↓↑topkKmM·.\-]+", t):
        return True
    return False


def clip_pane(raw: str, keep: int = 8) -> str:
    """Last few useful work lines — never Grok chrome, prompt, or typed input."""
    useful: list[str] = []
    skipping_prompt = False
    for ln in (raw or "").splitlines():
        s = _strip_ansi(ln)
        if _is_tui_chrome(s):
            skipping_prompt = False
            continue
        core = _BOX_RE.sub("", s).strip(" |")
        if _PROMPT_RE.match(s) or _PROMPT_RE.match(core) or core in {">", "❯", "|"} or s.strip() in {">", "❯", "|"}:
            skipping_prompt = True
            continue
        if skipping_prompt:
            continue
        useful.append(s[-160:])
    kept = useful[-keep:]
    if not kept:
        return ""
    return "\n".join(kept)


def capture_terminal_tab(tty: str, title: str = "") -> str:
    want = (tty or "").strip()
    if want and not want.startswith("/dev/"):
        want = "/dev/" + want.lstrip("/")
    hint = unique_title_hint(title) if title else ""
    if not hint:
        hint = ""
    try:
        r = subprocess.run(
            [
                "osascript",
                "-e",
                "on run argv",
                "-e",
                "set want to item 1 of argv",
                "-e",
                "set hint to item 2 of argv",
                "-e",
                'tell application "Terminal"',
                "-e",
                "repeat with w in windows",
                "-e",
                "try",
                "-e",
                "if want is not \"\" and (tty of selected tab of w) is want then",
                "-e",
                "return history of selected tab of w",
                "-e",
                "end if",
                "-e",
                "if hint is not \"\" and (name of w) contains hint then",
                "-e",
                "return history of selected tab of w",
                "-e",
                "end if",
                "-e",
                "end try",
                "-e",
                "end repeat",
                "-e",
                "end tell",
                "-e",
                'return ""',
                "-e",
                "end run",
                want or "",
                hint or "",
            ],
            capture_output=True,
            text=True,
            timeout=1.2,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def _iso_age(ts) -> float | None:
    if not ts:
        return None
    try:
        text = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, time.time() - dt.timestamp())
    except Exception:
        return None


def _hist_text(obj: dict) -> str:
    raw = obj.get("content")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return "\n".join(
            str(b.get("text") or "") if isinstance(b, dict) else str(b) for b in raw
        )
    return ""


def _reasoning_text(summary) -> str:
    if isinstance(summary, str):
        t = summary.strip()
    elif isinstance(summary, list):
        bits = []
        for item in summary:
            if isinstance(item, dict):
                bits.append(str(item.get("text") or ""))
            elif item:
                bits.append(str(item))
        t = " ".join(bits).strip()
    else:
        return ""
    return re.sub(r"\s+", " ", t).strip()


def _hist_live_bits(pth: Path) -> tuple[str, str, str, str]:
    turn = _turn_live(pth)
    return turn["last_user"], turn["last_asst"], turn["tool"], turn["extra"]


def _turn_live(pth: Path) -> dict:
    """Current-turn tools, thought and last prose — not limited to 45 seconds."""
    last_user = ""
    last_asst = ""
    thought = ""
    tool = ""
    extra = ""
    steps: list[dict] = []
    for obj in last_jsonl_tail(pth / "chat_history.jsonl", 220000):
        kind = obj.get("type")
        if kind == "user":
            text = _hist_text(obj)
            found = USER_Q.findall(str(text or ""))
            if found:
                last_user = found[-1].strip()
            elif text and "user_query" not in str(text):
                last_user = str(text).strip()
            last_asst = ""
            thought = ""
            tool, extra = "", ""
            steps = []
        elif kind == "reasoning":
            bit = _reasoning_text(obj.get("summary"))
            if bit:
                thought = bit
        elif kind == "assistant":
            tcs = obj.get("tool_calls") or []
            if isinstance(tcs, list):
                for tc in tcs:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    name = str(tc.get("name") or fn.get("name") or "")
                    ex = _tool_extra(
                        tc.get("input") or tc.get("arguments") or fn.get("arguments")
                    )
                    if name:
                        tool, extra = name, ex
                        steps.append(
                            {
                                "tool": name,
                                "target": _clean_live_target(ex),
                            }
                        )
            text = strip_code_blocks(_hist_text(obj)).strip()
            if text and not text.startswith("{"):
                last_asst = text
    return {
        "last_user": last_user,
        "last_asst": last_asst,
        "thought": thought,
        "tool": tool,
        "extra": extra,
        "steps": steps,
        "step_n": len(steps),
    }


def _idle_progress() -> dict:
    return {
        "activity": "Ready",
        "tool": "",
        "detail": "",
        "thought": "",
        "pane": "",
        "waiting": False,
        "headline": "",
        "line": "",
        "on": "",
        "step_n": 0,
        "done": "",
    }


def live_progress(sid: str, tmux: str, title: str, tty: str = "") -> dict:
    """What the bot is doing right now — same idea as the Terminal status line."""
    activity = agents_tmux.activity_from_text(title or "", True) or "Busy"
    tool = ""
    extra = ""
    waiting = False
    last_user = ""
    last_asst = ""
    thought = ""
    step_n = 0
    steps: list[dict] = []
    pth = session_dir_by_id(sid) if sid else None
    if pth:
        turn = _turn_live(pth)
        last_user = turn["last_user"]
        last_asst = turn["last_asst"]
        thought = turn["thought"]
        hist_tool = turn["tool"]
        hist_extra = turn["extra"]
        step_n = int(turn["step_n"] or 0)
        steps = list(turn.get("steps") or [])
        ev_tool = ""
        ev_phase = ""
        for ev in reversed(last_jsonl_tail(pth / "events.jsonl", 80000)):
            t = ev.get("type")
            age = _iso_age(ev.get("ts"))
            if t == "turn_ended":
                break
            if t == "tool_started" and ev.get("tool_name"):
                ev_tool = str(ev["tool_name"])
                waiting = True
                break
            if t == "phase_changed" and not ev_phase:
                ph = str(ev.get("phase") or "")
                if ph in {
                    "reasoning",
                    "streaming_reasoning",
                    "responding",
                    "streaming_text",
                    "tool_execution",
                }:
                    ev_phase = ph
                    if age is not None and age > 180:
                        continue
                    waiting = True
                    break
        if last_user and not last_asst:
            waiting = True
        tool = ev_tool or hist_tool
        extra = hist_extra
        if ev_phase in {"reasoning", "streaming_reasoning"} and not ev_tool:
            activity = "Thinking"
        elif ev_phase in {"responding", "streaming_text"} and not ev_tool:
            activity = "Writing"
        elif ev_phase == "tool_execution" and not tool:
            activity = "Busy"
    live_title = title or ""
    if tmux:
        try:
            live_title = agents_tmux.pane_title(tmux) or live_title
        except Exception:
            pass
    if agents_tmux.title_busy(live_title or title or ""):
        waiting = True
    title_bit = re.search(
        r"(?:Preparing|Running|Dump|Open|Read|Write|Searching|Thinking)\s+[^\-—]+",
        live_title or title or "",
        re.I,
    )
    if title_bit:
        extra = extra or re.sub(r"\s+", " ", title_bit.group(0)).strip()
    if tool:
        activity = TOOL_NL.get(tool, tool.replace("_", " "))
    pane = ""
    if tmux:
        try:
            pane = agents_tmux.capture_pane(tmux, 28)
        except Exception:
            pane = ""
        act2 = agents_tmux.activity_from_text(pane or "", True)
        if act2 and act2 not in {"Klaar", "Bezig", "Ready", "Busy"}:
            activity = act2
        if agents_tmux.pane_busy(pane or ""):
            waiting = True
        elif not agents_tmux.pane_overlay(pane or ""):
            # Live pane is source of truth. A stale title or interrupted
            # turn (user still last in history) must not keep the pill on.
            waiting = False
    raw_pane = pane
    pane = clip_pane(pane, keep=8) if pane else ""
    if not waiting:
        return _idle_progress()
    step = _grok_title_step(live_title or title)
    task = _task_from_pane(raw_pane)
    if not thought:
        thought = _thought_from_pane(raw_pane)
    tally = _grok_pane_tally(raw_pane) or _step_tally(steps)
    generic = {
        _LIVE_HEAD.get("Thinking"),
        _LIVE_HEAD.get("Busy"),
        _LIVE_HEAD.get("Denkt"),
        _LIVE_HEAD.get("Bezig"),
        "Thinking through the approach",
        "Working on your request",
        "Deciding the next step",
        "Working in the terminal",
    }
    desc = describe_live(
        activity,
        extra or (step if step and len(step) < 80 else ""),
        last_user,
        last_asst or thought,
    )
    if tally and not _is_tally_pill(tally):
        desc["line"] = tally
    elif step and len(step) > 80:
        desc["line"] = step
    elif thought:
        nice = _human_thought(thought)
        if nice and desc["line"] in generic:
            desc["line"] = nice
    if task and desc["line"] in generic:
        desc["line"] = task
    if step and len(step) <= 80:
        desc["headline"] = step
    elif task:
        desc["headline"] = task
    if step_n and desc["line"] in generic:
        desc["line"] = f"{step_n} steps so far"
    if not pane:
        pane = desc["line"]
    return {
        "activity": activity or "Busy",
        "tool": tool,
        "detail": desc["line"],
        "thought": _human_thought(thought) if thought else "",
        "pane": pane,
        "waiting": True,
        "headline": desc["headline"],
        "line": desc["line"],
        "on": "",
        "step_n": step_n,
        "done": tally,
    }


_PROG_NOTE = {
    "Reading": "Reading",
    "Writing": "Editing",
    "Terminal": "Running command",
    "Searching": "Searching",
    "Searching web": "Searching the web",
    "Reading web": "Reading page",
    "Browser": "Browsing",
    "Planning": "Planning",
    "Image": "Working on image",
    "Waiting": "Waiting",
    "Thinking": "Thinking",
    "Busy": "Working",
    "Leest": "Reading",
    "Schrijft": "Editing",
    "Zoekt": "Searching",
    "Zoekt web": "Searching the web",
    "Denkt": "Thinking",
    "Bezig": "Working",
    "Plant": "Planning",
    "Beeld": "Working on image",
    "Wacht": "Waiting",
}

_PILL_ALIAS = {
    "leest in de code": "Reading",
    "past een bestand aan": "Editing",
    "voert een commando uit": "Running command",
    "zoekt in de code": "Searching",
    "zoekt op het web": "Searching the web",
    "leest een webpagina": "Reading page",
    "kijkt in de browser": "Browsing",
    "plant de volgende stappen": "Planning",
    "werkt aan beeld": "Working on image",
    "wacht op een taak": "Waiting",
    "denkt na": "Thinking",
    "aan het werk": "Working",
    "planning next steps": "Planning",
    "deciding the next step": "Thinking",
    "thinking through the approach": "Thinking",
    "working on your request": "Working",
    "reading the code": "Reading",
    "editing a file": "Editing",
    "running a command": "Running command",
    "searching the code": "Searching",
}

_TALLY_RX = re.compile(
    r"\b((?:Searched|Read|Listed|Wrote|Edited|Ran)\s+\d+\s+(?:files?|patterns?|dirs?|directories|commands?|times?))\b",
    re.I,
)
_PILL_CLOCK_RX = re.compile(r"^\d+\s*[smh]$|^\d+u\s+\d+m$", re.I)
_PILL_FILE_RX = re.compile(
    r"^(?:Leest|Read|Reading)\s+(\S+)$|^(?:Past|Edited|Editing)\s+(\S+)(?:\s+aan)?$|"
    r"^(?:Zoekt in|Searched|Searching)\s+(\S+)$|^(?:Ran|Running)\s+(\S+)$",
    re.I,
)


def _step_tally(steps: list[dict] | None) -> str:
    rows = [s for s in (steps or []) if isinstance(s, dict)]
    if not rows:
        return ""
    writes = [s for s in rows if s.get("tool") in {"search_replace", "write"}]
    reads = [s for s in rows if s.get("tool") in {"read_file", "list_dir"}]
    greps = [s for s in rows if s.get("tool") == "grep"]
    if len(writes) == 1 and writes[0].get("target"):
        return f"Editing {writes[0]['target']}"
    if len(writes) > 1:
        return f"Edited {len(writes)} files"
    if len(reads) >= 2:
        return f"Read {len(reads)} files"
    if len(reads) == 1 and reads[0].get("target"):
        return f"Reading {reads[0]['target']}"
    if greps:
        return f"Searched {len(greps)} time" + ("s" if len(greps) != 1 else "")
    n = len(rows)
    return f"{n} step" + ("s" if n != 1 else "")


def _progress_clock(seconds: float) -> str:
    s = max(0, int(seconds or 0))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    return f"{m // 60}u {m % 60}m"


def _progress_extra(prog: dict | None) -> str:
    p = prog or {}
    head = _PROG_NOTE.get(str(p.get("activity") or "Busy")) or "Working"
    for cand in (p.get("done"), p.get("line"), p.get("detail")):
        c = re.sub(r"\s+", " ", str(cand or "")).strip()
        if not c or c.lower() == head.lower():
            continue
        if c.endswith("?") or re.match(
            r"^(ik |voor |maak |zorg |check |lukt |doe |the |i |please )", c, re.I
        ):
            continue
        if len(c) > 72:
            continue
        if looks_like_user_question(c):
            continue
        return c
    return ""


def looks_like_user_question(text: str) -> bool:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if not t or len(t) < 12:
        return False
    if t.endswith("?") and len(t) > 24:
        return True
    return bool(re.match(r"^(maak |zorg |check |fix |verwijder |ik wil )", t, re.I))


def _progress_filename(prog: dict | None) -> str:
    extra = _progress_extra(prog)
    m = re.search(r"([A-Za-z0-9._-]+\.[A-Za-z0-9]{1,8})\b", extra)
    return m.group(1) if m else ""


def _title_tally(text: str) -> str:
    bit = re.sub(r"\s+", " ", str(text or "")).strip()
    if not bit:
        return ""
    return bit[0].upper() + bit[1:] if bit[0].islower() else bit


def _is_tally_pill(text: str) -> bool:
    return bool(_TALLY_RX.fullmatch(str(text or "").strip()))


def _normalize_pill(text: str) -> str:
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw or _PILL_CLOCK_RX.fullmatch(raw):
        return ""
    if re.fullmatch(r"(nog\s+)?bezig", raw, re.I):
        return ""
    alias = _PILL_ALIAS.get(raw.lower())
    if alias:
        return alias
    if _PROG_NOTE.get(raw):
        return _PROG_NOTE[raw]
    if looks_like_user_question(raw) or "?" in raw or len(raw) > 48:
        return ""
    m = _PILL_FILE_RX.match(raw)
    if m:
        name = next((g for g in m.groups() if g), "")
        name = name.rstrip(".,;:") if name else ""
        low = raw.lower()
        if name:
            if low.startswith(("leest", "read")):
                return f"Read {name}"
            if low.startswith(("past", "edit")):
                return f"Edited {name}"
            if low.startswith(("zoekt", "search")):
                return f"Searched {name}"
            if low.startswith("ran") or low.startswith("running"):
                return f"Ran {name}"
    if _TALLY_RX.fullmatch(raw):
        return _title_tally(raw)
    return raw


def pills_from_progress_text(text: str) -> list[str]:
    """Turn stored/live status into Grok-Bot pills: 'Read 11 files', 'Thinking'."""
    raw = str(text or "").strip()
    if not raw:
        return []
    raw = re.sub(r"^(Nog\s+)?Bezig\s*·\s*", "", raw, flags=re.I)
    found: list[str] = []
    seen: set[str] = set()
    for bit in _TALLY_RX.findall(raw):
        pill = _title_tally(bit)
        key = pill.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(pill)
    if found:
        return found
    out: list[str] = []
    for part in re.split(r"\s*·\s*", raw):
        for bit in part.split(","):
            pill = _normalize_pill(bit)
            if not pill:
                continue
            key = pill.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(pill)
    return out


def _activity_pill(prog: dict | None) -> str:
    act = str((prog or {}).get("activity") or "Busy")
    return _PROG_NOTE.get(act) or "Working"


def progress_pills(prog: dict | None, elapsed_s: float = 0) -> list[str]:
    p = prog or {}
    blob = " · ".join(
        str(p.get(k) or "").strip() for k in ("done", "line", "detail") if p.get(k)
    )
    pills = [x for x in pills_from_progress_text(blob) if not _is_tally_pill(x)]
    name = _progress_filename(p)
    act = str(p.get("activity") or "Busy")
    if name and act in {"Writing", "Schrijft"}:
        return [f"Edited {name}"]
    if name and act in {"Reading", "Leest"}:
        return [f"Read {name}"]
    if name and act in {"Searching", "Zoekt"}:
        return [f"Searched {name}"]
    if name and act == "Terminal":
        return [f"Ran {name}"]
    generic = {
        "thinking",
        "working",
        "busy",
        "waiting",
        "deciding the next step",
        "thinking through the approach",
        "working on your request",
        "working in the terminal",
    }
    for key in ("headline", "line", "thought", "detail"):
        val = re.sub(r"\s+", " ", str(p.get(key) or "")).strip()
        if not val or val.lower() in generic:
            continue
        if looks_like_user_question(val) or "?" in val:
            continue
        if 8 <= len(val) <= 90:
            return [val]
    if pills:
        return pills[-1:]
    return [_activity_pill(p)]


def progress_note_text(prog: dict | None, elapsed_s: float = 0) -> str:
    pills = progress_pills(prog, elapsed_s)
    return pills[-1] if pills else ""


def _progress_fp(prog: dict | None) -> str:
    return "|".join(progress_pills(prog)).lower()


def merge_progress_notes(msgs: list[dict], swarm: list[dict]) -> list[dict]:
    notes: list[dict] = []
    last_user = -1
    last_user_ts = None
    for i, m in enumerate(msgs or []):
        if m.get("role") == "user" and not m.get("helper") and m.get("meta") != "file":
            last_user = i
            last_user_ts = _swarm_ts(m)
    have = {
        _norm_txt(m.get("text"))
        for i, m in enumerate(msgs or [])
        if m.get("meta") == "progress" and i > last_user
    }
    for sm in swarm or []:
        if sm.get("meta") != "progress":
            continue
        sts = _swarm_ts(sm)
        if last_user_ts and sts and sts < last_user_ts - 0.2:
            continue
        for pill in pills_from_progress_text(sm.get("text") or ""):
            if _is_tally_pill(pill):
                continue
            key = _norm_txt(pill)
            if not key or key in have:
                continue
            have.add(key)
            notes.append(
                {
                    "role": "assistant",
                    "text": pill,
                    "at": sm.get("at"),
                    "meta": "progress",
                }
            )
    if not notes:
        return list(msgs or [])
    notes.sort(key=lambda m: (_swarm_ts(m) or 0, str(m.get("text") or "")))
    out = list(msgs or [])
    for note in notes:
        nts = _swarm_ts(note)
        if nts is None:
            out.append(note)
            continue
        idx = len(out)
        for i, m in enumerate(out):
            mts = _swarm_ts(m)
            if mts is not None and mts > nts:
                idx = i
                break
        out.insert(idx, note)
    return out


def merge_file_cards(msgs: list[dict], swarm: list[dict]) -> list[dict]:
    """One file card per name. Turn Grok 'Bestand ontvangen' lines into that card."""
    out: list[dict] = []
    have: set[str] = set()

    def as_card(src: dict, name: str) -> dict:
        path = str(src.get("path") or "")
        if not path:
            first = str(src.get("text") or "").split("\n")[0]
            hit = re.match(r"^(?:Bestand(?: ontvangen)?:|📎\s*)\s*(.+)$", first, re.I)
            if hit:
                path = hit.group(1).strip()
        return {
            "role": "user",
            "text": name,
            "at": src.get("at"),
            "meta": "file",
            "name": name,
            "path": path,
        }

    for m in msgs or []:
        name = file_basename(m)
        notice = bool(m.get("meta") == "file" or is_file_notice(m.get("text") or ""))
        if notice and name:
            key = _norm_txt(name)
            if key in have:
                continue
            have.add(key)
            out.append(as_card(m, name))
            continue
        out.append(m)
    extras: list[dict] = []
    for sm in swarm or []:
        if sm.get("meta") != "file" and not is_file_notice(sm.get("text") or ""):
            continue
        name = file_basename(sm)
        key = _norm_txt(name)
        if not key or key in have:
            continue
        have.add(key)
        extras.append(as_card(sm, name))
    if not extras:
        return out
    for card in extras:
        nts = _swarm_ts(card)
        if nts is None:
            out.append(card)
            continue
        idx = len(out)
        for i, m in enumerate(out):
            mts = _swarm_ts(m)
            if mts is not None and mts > nts:
                idx = i
                break
        out.insert(idx, card)
    return out


def maybe_note_progress(
    slug: str, prog: dict | None, elapsed_s: float = 0, last_user_at: float | None = None
) -> None:
    if not slug or not (prog or {}).get("waiting"):
        return
    pills = progress_pills(prog, elapsed_s)
    if not pills:
        return
    now = time.time()
    path = rosterlib.agent_dir(slug) / "live_note.json"
    prev: dict = {}
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        prev = {}
    seen = [str(x) for x in (prev.get("pills") or []) if str(x).strip()]
    if not seen and prev.get("last"):
        seen = pills_from_progress_text(str(prev.get("last") or ""))
    seen_l = {s.lower() for s in seen}
    age = now - float(prev.get("at") or 0)
    turn_at = float(prev.get("turn") or 0)
    new_turn = bool(last_user_at and last_user_at > turn_at + 0.4)
    if new_turn:
        seen, seen_l = [], set()
    fresh = [p for p in pills if p.lower() not in seen_l]
    if not fresh:
        act = _activity_pill(prog)
        if act and act.lower() not in seen_l and age >= 0.4:
            fresh = [act]
        else:
            return
    try:
        for text in fresh:
            rosterlib.remember_swarm_msg(slug, "assistant", text, meta="progress")
            seen.append(text)
            seen_l.add(text.lower())
        path.write_text(
            json.dumps(
                {
                    "at": now,
                    "last": fresh[-1],
                    "pills": seen,
                    "fp": "|".join(seen).lower(),
                    "turn": last_user_at or turn_at or now,
                }
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def collect_chat(win: dict) -> dict:
    """This bot's session plus every extra-agent thread that belongs to it."""
    slug = slug_for_window(win)
    cache_key = str(slug or win.get("tmux") or win.get("id") or "")
    now = time.time()
    hit = _collect_cache.get(cache_key)
    if cache_key and hit and now - hit[0] < 0.08:
        return hit[1]
    rost = rosterlib.load_roster()
    meta = (rost.get("agents") or {}).get(slug or "") or {}
    packed = {
        **meta,
        "slug": slug or meta.get("slug") or "",
        "tmux": win.get("tmux") or meta.get("tmux"),
        "ai": meta.get("ai") or "grok",
        "window_id": win.get("id") or meta.get("window_id"),
        "label": meta.get("label") or "",
    }
    sid = session_id_from_nicks(packed)
    title = str(win.get("title") or meta.get("title") or "")
    tty = str(win.get("tty") or meta.get("tty") or "")
    if not tty and not packed.get("tmux"):
        try:
            tty = match_tty({**win, "slug": slug, "title": title})
        except Exception:
            tty = ""
    live_sid = ""
    tmux_name = str(packed.get("tmux") or "")
    if tmux_name:
        try:
            live_sid = agents_tmux.live_session_id(tmux_name)
        except Exception:
            live_sid = ""
    if not live_sid and tty:
        try:
            live_sid = live_session_id_for_tty(tty)
        except Exception:
            live_sid = ""
    have = str(meta.get("session_id") or sid or "")
    if live_sid:
        packed["session_id"] = live_sid
        if live_sid != have:
            _persist_session(slug, live_sid)
        sid = live_sid
    elif sid:
        packed["session_id"] = sid
    main_sid = str(packed.get("session_id") or meta.get("session_id") or "")
    msgs = messages_for_meta(packed)
    if slug:
        try:
            rosterlib.ingest_loop_texts(
                slug, [m.get("text") for m in msgs if m.get("role") == "user"]
            )
        except Exception:
            pass
        have = {_norm_txt(m.get("text")) for m in msgs if m.get("role") == "user"}
        swarm_all = rosterlib.load_swarm_msgs(slug)
        msgs = merge_swarm_users(msgs, swarm_all, have)
    else:
        swarm_all = []
    prog = live_progress(main_sid, packed.get("tmux") or "", title, tty)
    stopped = is_just_stopped(slug or "")
    pane_live = False
    if not stopped:
        try:
            pane_live = live_busy(win)
        except Exception:
            pane_live = bool((prog or {}).get("waiting"))
    live_wait = (not stopped) and (
        pane_live or bool((prog or {}).get("waiting")) or last_submit_hold(meta)
    )
    answered = turn_answered(win, slug, msgs)
    open_turn = turn_open(msgs, live_waiting=live_wait)
    # A first prose reply must not hide a still-running turn (tools/compact).
    if stopped:
        prog = _idle_progress()
        live_wait = False
        open_turn = False
        pane_live = False
    elif pane_live:
        prog = dict(prog or {})
        prog["waiting"] = True
        if prog.get("activity") in (None, "", "Klaar", "Ready"):
            prog["activity"] = "Busy"
        live_wait = True
        open_turn = True
    elif answered and not last_submit_hold(meta):
        prog = _idle_progress()
        live_wait = False
        open_turn = False
    elif open_turn and live_wait:
        prog = dict(prog or {})
        prog["waiting"] = True
        if prog.get("activity") in (None, "", "Klaar", "Ready"):
            prog["activity"] = "Busy"
        if not (prog.get("line") or prog.get("headline")):
            desc = describe_live(prog.get("activity") or "Busy", prog.get("detail") or "", "")
            prog["headline"] = desc["headline"]
            prog["line"] = desc["line"]
    prog = dict(prog or {})
    prog["on"] = ""
    last_user_at = None
    for m in reversed(msgs or []):
        if m.get("role") == "user" and not m.get("helper"):
            last_user_at = _swarm_ts(m)
            break
    if slug and prog.get("waiting"):
        since = meta.get("busy_since") or win.get("busy_since")
        try:
            elapsed = max(0.0, time.time() - float(since)) if since else float(win.get("busy_for") or 0)
        except (TypeError, ValueError):
            elapsed = 0.0
        maybe_note_progress(slug, prog, elapsed, last_user_at)
    if slug:
        more = rosterlib.load_swarm_msgs(slug) or swarm_all
        msgs = merge_progress_notes(msgs, more)
        msgs = merge_file_cards(msgs, more)
    out = {
        "messages": msgs,
        "session": main_sid,
        "helpers": 0,
        "helpers_busy": 0,
        "queued": len(rosterlib.load_queue(slug) if slug else []),
        "queue": rosterlib.public_queue(slug) if slug else [],
        "crew": crew_for_slug(slug or "", meta),
        "loops": rosterlib.public_loops(slug) if slug else [],
        "progress": prog,
    }
    crew = out["crew"] or []
    if stopped or (answered and not pane_live and not last_submit_hold(meta)):
        for c in crew:
            if c and not c.get("helper"):
                c["busy"] = False
                if (c.get("activity") or "") in {"", "Busy", "Bezig", "Waiting"}:
                    c["activity"] = "Ready"
    out["helpers"] = sum(1 for c in crew if c.get("helper"))
    out["helpers_busy"] = sum(1 for c in crew if c.get("helper") and c.get("busy"))
    if cache_key:
        _collect_cache[cache_key] = (now, out)
    return out


def _clear_tmux_overlay(tmux: str) -> None:
    if not tmux:
        return
    try:
        pane = agents_tmux.capture_pane(tmux, 20)
        if agents_tmux.pane_overlay(pane) == "rewind":
            agents_tmux.send_keys(tmux, "Escape")
            time.sleep(0.18)
    except Exception:
        pass


def _send_when_ready(tmux: str, text: str, timeout: float = 80.0) -> None:
    """Wait until Grok can take a message, then paste+Enter. Do not fire into a live turn."""
    if not tmux or not (text or "").strip():
        return
    _clear_tmux_overlay(tmux)
    ready = agents_tmux.wait_ready(tmux, timeout=timeout)
    if not ready:
        time.sleep(0.5)
    _clear_tmux_overlay(tmux)
    last_err = None
    for _ in range(3):
        try:
            agents_tmux.send_text(tmux, text, enter=True)
            return
        except Exception as exc:
            last_err = exc
            time.sleep(0.9)
            _clear_tmux_overlay(tmux)
    if last_err:
        print("send when ready", tmux, last_err, flush=True)


def pick_idle_helper(parent_slug: str) -> dict | None:
    live = {s["tmux"]: s for s in agents_tmux.list_sessions(include_helpers=True)}
    for h in reversed(rosterlib.helpers_of(parent_slug)):
        sess = live.get(h.get("tmux") or "")
        if sess and not sess.get("busy"):
            return h
    return None


def start_helper(win: dict, slug: str, text: str) -> dict:
    """Always start (or reuse) an extra agent. Explicit assign must not queue."""
    last = last_user_text(win, slug)
    rost = rosterlib.load_roster()
    meta = (rost.get("agents") or {}).get(slug) or {}
    label = meta.get("label") or slug
    cwd = meta.get("cwd") or str(agents_tmux.WORK_ROOT / slug)
    live = {s["tmux"]: s for s in agents_tmux.list_sessions(include_helpers=True)}
    active = [h for h in (meta.get("helpers") or []) if h.get("tmux") in live]
    reuse = pick_idle_helper(slug)
    prompt = helper_prompt(str(label), text, last)
    task = (text or "").strip()[:400]
    now = time.time()
    if reuse and reuse.get("tmux"):
        rosterlib.update_helper(
            slug,
            reuse.get("slug") or "",
            task=task,
            last_submit_at=now,
            busy_since=now,
        )
        threading.Thread(
            target=_send_when_ready, args=(reuse["tmux"], prompt), daemon=True
        ).start()
        return {**reuse, "reused": True, "id": reuse.get("slug") or reuse.get("tmux")}
    if len(active) >= 5:
        target = active[0]
        rosterlib.update_helper(
            slug,
            target.get("slug") or "",
            task=task,
            last_submit_at=now,
            busy_since=now,
        )
        threading.Thread(
            target=_send_when_ready, args=(target["tmux"], prompt), daemon=True
        ).start()
        return {**target, "reused": True, "id": target.get("slug") or target.get("tmux")}
    dead = None
    for h in meta.get("helpers") or []:
        name = str(h.get("tmux") or "")
        if name and name not in live and str(h.get("task") or "").strip() == task:
            dead = h
            break
    if not dead:
        for h in meta.get("helpers") or []:
            name = str(h.get("tmux") or "")
            if name and name not in live:
                dead = h
                break
    if dead:
        try:
            info = revive_helper(slug, meta, {**dead, "task": task})
            rosterlib.update_helper(
                slug,
                dead.get("slug") or "",
                task=task,
                last_submit_at=now,
                busy_since=now,
            )
            threading.Thread(
                target=_send_when_ready, args=(info["tmux"], prompt), daemon=True
            ).start()
            return {**info, "revived": True}
        except Exception as exc:
            print("start_helper revive", exc, flush=True)
    info = agents_tmux.spawn_helper(
        slug,
        cwd=cwd,
        ai=rosterlib.normalize_ai(meta.get("ai")),
        model=meta.get("model") or "",
    )
    agents_tmux._list_cache = (0.0, [])
    dropped = []
    with _dispatch_lock:
        out = rosterlib.add_helper(
            slug, {**info, "task": task, "last_submit_at": now, "busy_since": now}
        )
        dropped = out.get("dropped") or []
    for old in dropped:
        if old.get("tmux"):
            try:
                agents_tmux.kill(old["tmux"])
            except Exception:
                pass
    threading.Thread(target=_send_when_ready, args=(info["tmux"], prompt), daemon=True).start()
    return {**info, "id": info.get("slug") or info.get("tmux")}


def reassign_helper(win: dict, slug: str, hid: str, action: str) -> dict:
    """Move an extra agent to this chat or the queue after Swarm already picked agent."""
    action = (action or "").strip().lower()
    gone = rosterlib.remove_helper(slug, hid)
    if not gone:
        raise RuntimeError("no extra agent")
    if gone.get("tmux"):
        try:
            agents_tmux.kill(str(gone["tmux"]))
        except Exception:
            pass
    task = unwrap_helper_user(str(gone.get("task") or "")).strip()
    queued = False
    steered = False
    if action == "queue":
        if not task:
            raise RuntimeError("nothing to queue")
        rosterlib.enqueue(slug, task, source="user", hold=False)
        queued = True
    elif action == "steer":
        if not task:
            raise RuntimeError("nothing to send")
        steer_into_chat(win, slug, task)
        steered = True
    elif action not in {"helper", "agent", ""}:
        raise RuntimeError("unknown action")
    return {
        "ok": True,
        "action": action or "drop",
        "dropped": True,
        "helper": False,
        "queued": queued,
        "via": "queue" if queued else ("steer" if steered else "drop"),
    }


def drain_queues() -> None:
    rost = rosterlib.load_roster()
    live = {s["slug"]: s for s in agents_tmux.list_sessions()}
    for slug, meta in (rost.get("agents") or {}).items():
        q = rosterlib.mature_queue(slug)
        ready = [x for x in q if isinstance(x, dict) and str(x.get("status") or "queued") == "queued"]
        if not ready:
            continue
        win = None
        if meta.get("tmux"):
            win = live.get(slug) or {"tmux": meta["tmux"], "slug": slug, "id": meta.get("window_id")}
        elif meta.get("window_id"):
            win = find_window(int(meta["window_id"]))
        tmux = str((win or {}).get("tmux") or meta.get("tmux") or "")
        if tmux:
            try:
                if agents_tmux.pane_busy(agents_tmux.capture_pane(tmux, 20)):
                    continue
            except Exception:
                if not win or live_busy(win):
                    continue
        elif not win or live_busy(win):
            continue
        item = ready[0]
        q = [x for x in q if x is not item]
        rosterlib.save_queue(slug, q)
        try:
            if tmux:
                threading.Thread(
                    target=_send_when_ready,
                    args=(tmux, item.get("text") or "", 40.0),
                    daemon=True,
                ).start()
            else:
                deliver_text(win, item.get("text") or "", True)
            _mark_submit(slug)
            try:
                rosterlib.remember_swarm_msg(slug, "user", item.get("text") or "")
            except Exception:
                pass
        except Exception:
            rosterlib.save_queue(slug, [item] + q)
            return


def _mark_submit(slug: str) -> None:
    if not slug:
        return
    now = time.time()
    rost = rosterlib.load_roster()
    if slug in (rost.get("agents") or {}):
        rost["agents"][slug]["last_submit_at"] = now
        if slug not in _busy_since:
            rost["agents"][slug]["busy_since"] = now
            _busy_since[slug] = now
            _busy_idle_since.pop(slug, None)
        rosterlib.save_roster(rost)


def recently_submitted(slug: str) -> bool:
    if not slug:
        return False
    meta = (rosterlib.load_roster().get("agents") or {}).get(slug) or {}
    try:
        return (time.time() - float(meta.get("last_submit_at") or 0)) < 25
    except (TypeError, ValueError):
        return False


def _dispatch_leak(win: dict, slug: str, body: str, name: str) -> dict:
    """'lek naar ads' jumps to that bot and optionally forwards the last real question."""
    found = rosterlib.find_agent_by_name(name)
    if not found:
        raise RuntimeError(f"no bot named {name}")
    tslug, tmeta = found
    label = str(tmeta.get("label") or tslug)
    switch = {"id": tmeta.get("window_id"), "slug": tslug, "label": label}
    if tslug == slug:
        return {"ok": True, "silent": True, "via": "here", "routed": [], "switch": switch}
    payload = rosterlib.last_user_question(slug, skip=body) if slug else ""
    via = "switch"
    if payload:
        twin = {
            "slug": tslug,
            "id": tmeta.get("window_id"),
            "tmux": tmeta.get("tmux") or "",
            "session_id": tmeta.get("session_id") or "",
            "title": tmeta.get("title") or label,
        }
        try:
            busy = live_busy(twin) or recently_submitted(tslug)
        except Exception:
            busy = False
        if busy:
            rosterlib.enqueue(tslug, payload, source="user", hold=True)
            via = "pill"
        else:
            deliver_text(twin, payload, True)
            _mark_submit(tslug)
            try:
                rosterlib.remember_swarm_msg(tslug, "user", payload)
            except Exception:
                pass
            via = "tmux"
    return {
        "ok": True,
        "silent": True,
        "via": via,
        "helper": False,
        "queued": via == "pill",
        "routed": [label],
        "switch": switch,
    }


_STOP_CMD_RE = re.compile(
    r"^(?:stop+|stop!+|stop nu|stop dit|hou op|halt|cancel|abort)\s*[!.]*$",
    re.I,
)


def is_stop_command(text: str) -> bool:
    return bool(_STOP_CMD_RE.match((text or "").strip()))


def dispatch_text(win: dict, text: str, submit: bool, hinted_busy: bool = False) -> dict:
    slug = slug_for_window(win) or win.get("slug") or ""
    body = (text or "").strip()
    if submit and is_stop_command(body):
        out = interrupt_chat(win)
        out["via"] = "stop-cmd"
        out["silent"] = True
        return out
    leak_name = rosterlib.parse_leak_command(body) if submit else None
    if leak_name:
        return _dispatch_leak(win, slug, body, leak_name)
    if submit and (text or "").strip():
        rost = rosterlib.load_roster()
        label = ((rost.get("agents") or {}).get(slug) or {}).get("label") or slug or "bot"
        try:
            remember_user(label, text)
        except Exception:
            pass
        try:
            parsed = rosterlib.parse_loop_command(text)
            if parsed and slug:
                rosterlib.upsert_loop(
                    slug,
                    parsed["name"],
                    parsed["interval"],
                    parsed["prompt"],
                    source="grok",
                )
        except Exception:
            pass
    body = (text or "").strip()
    if submit and slug and body:
        last = _last_typed.get(slug)
        if last and last[0] == body and time.time() - last[1] < 8:
            if slug:
                try:
                    rosterlib.remember_swarm_msg(slug, "user", body)
                except Exception:
                    pass
            return {"ok": True, "via": "dup", "helper": False, "queued": False}
        _last_typed[slug] = (body, time.time())
    rost_now = rosterlib.load_roster()
    ceo = rost_now.get("ceo") or ""
    if (
        submit
        and body
        and slug
        and slug != ceo
        and not body.startswith("[Swarm extra]")
        and live_busy(win)
        and not turn_answered(win, slug)
    ):
        choice = classify_second(win, body, slug)
        return apply_second_choice(win, slug, body, choice, chosen_by="swarm")
    deliver_text(win, text, submit)
    if submit and body:
        _mark_submit(slug)
        if slug:
            try:
                rosterlib.remember_swarm_msg(slug, "user", body)
            except Exception:
                pass
    if win.get("tmux") and submit and (text or "").strip():
        threading.Thread(
            target=lambda: (time.sleep(1.2), bind_by_recent_text(win, text)),
            daemon=True,
        ).start()
    if slug:
        try:
            drain_queues()
        except Exception:
            pass
    routed: list[str] = []
    if (
        submit
        and body
        and not body.startswith("[")
        and slug
        and slug == (rosterlib.load_roster().get("ceo") or "")
    ):
        try:
            routed = rosterlib.auto_delegate(
                slug,
                body,
                dispatch_text,
                is_busy=lambda win: live_busy(win) or recently_submitted(str(win.get("slug") or "")),
                inbox=lambda s, t: rosterlib.enqueue(s, t, source="CEO", hold=True),
            )
        except Exception as exc:
            print("auto_delegate", exc, flush=True)
    return {
        "ok": True,
        "via": "tmux" if win.get("tmux") else "tty",
        "helper": False,
        "queued": False,
        "routed": routed,
    }


def kill_helpers(slug: str) -> None:
    if not slug:
        return
    for h in rosterlib.helpers_of(slug):
        if h.get("tmux"):
            try:
                agents_tmux.kill(h["tmux"])
            except Exception:
                pass


def messages_from_pane(tmux_name: str) -> list[dict]:
    raw = agents_tmux.capture_pane(tmux_name)
    if not raw:
        return []
    skip = re.compile(
        r"^(Shift\+Tab|Enter:send|Ctrl\+x|Esc:cancel|Worked for|Thought for|Grok \d|~/|/[Uu]sers)",
        re.I,
    )
    msgs: list[dict] = []
    pending_user: str | None = None
    bits: list[str] = []

    def flush():
        nonlocal pending_user, bits
        if not pending_user:
            bits = []
            return
        msgs.append({"role": "user", "text": pending_user[:4000]})
        body = strip_code_blocks("\n".join(bits).strip())
        if body:
            msgs.append({"role": "assistant", "text": body[:8000]})
        pending_user = None
        bits = []

    for line in raw.splitlines():
        s = line.strip().strip("│").strip()
        if not s or s in {"█"} or skip.search(s):
            continue
        if "Waiting for response" in s:
            continue
        m = re.search(r"❯\s+(.+)", s)
        if m:
            flush()
            pending_user = re.sub(r"\s+\d{1,2}:\d{2}\s*[AP]M\s*$", "", m.group(1)).strip()
            continue
        if pending_user:
            if s.startswith("◆"):
                continue
            bits.append(s)
    flush()
    return msgs[-CHAT_LIMIT:]


_chat_cache: dict[str, dict] = {}


_CODE_FENCE = re.compile(r"```[\s\S]*?```")


def strip_code_blocks(text: str) -> str:
    """Keep prose; drop fenced snippets and leftover fences."""
    text = _CODE_FENCE.sub("", text or "")
    text = re.sub(r"```+\w*\n?", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _tool_status(name: str, extra: str = "") -> str:
    name = (name or "").strip()
    verb = TOOL_NL.get(name, name.replace("_", " ") if name else "")
    extra = re.sub(r"\s+", " ", extra or "").strip()
    if extra:
        extra = extra[:220]
        return f"{verb} · {extra}" if verb else extra
    return verb


def _tool_extra(obj) -> str:
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                return _tool_extra(json.loads(s))
            except Exception:
                pass
        return s
    if not isinstance(obj, dict):
        return ""
    for key in ("path", "file_path", "target_file", "query", "command", "url", "pattern", "target"):
        val = obj.get(key)
        if val:
            return str(val).replace("\n", " ").strip()
    return ""


def _attach_at(msg: dict | None, obj: dict) -> dict | None:
    if not msg:
        return None
    at = obj.get("timestamp") or obj.get("created_at") or obj.get("at")
    if at is not None and at != "":
        msg["at"] = at
    return msg


def _line_to_msg(line: bytes) -> dict | None:
    try:
        obj = json.loads(line.decode("utf-8", "replace"))
    except Exception:
        return None
    kind = obj.get("type")
    if kind in {"tool_result", "reasoning", "system"}:
        return None
    if kind == "backend_tool_call":
        kind_obj = obj.get("kind") or {}
        name = str(kind_obj.get("tool_type") or obj.get("name") or "")
        action = kind_obj.get("action") if isinstance(kind_obj, dict) else {}
        text = _tool_status(name, _tool_extra(action))
        return _attach_at(
            {"role": "assistant", "text": text[:200], "meta": "tool"} if text else None,
            obj,
        )
    content = obj.get("content")
    tool_bits: list[str] = []
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                btype = str(block.get("type") or "")
                if btype in {"tool_use", "tool_call"}:
                    label = _tool_status(str(block.get("name") or ""), _tool_extra(block.get("input")))
                    if label:
                        tool_bits.append(label)
                else:
                    parts.append(str(block.get("text") or ""))
            else:
                parts.append(str(block))
        content = "\n".join(p for p in parts if p)
    tcs = obj.get("tool_calls") or []
    if isinstance(tcs, list):
        for tc in tcs:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = str(tc.get("name") or fn.get("name") or "")
            extra = _tool_extra(tc.get("input") or tc.get("arguments") or fn.get("arguments"))
            label = _tool_status(name, extra)
            if label:
                tool_bits.append(label)
    text = str(content or "").strip()
    if kind == "user":
        found = USER_Q.findall(text)
        if found:
            text = found[-1].strip()
        elif "user_info" in text or "system-reminder" in text or "user_query" in text:
            return None
        elif re.search(
            r"this session is being continued|summary below covers the earlier portion|"
            r"Primary Request and Intent:",
            text,
            re.I,
        ):
            return None
        if not text or text.startswith("<"):
            return None
        return _attach_at({"role": "user", "text": text[:12000]}, obj)
    if kind == "assistant":
        text = strip_code_blocks(text)
        if text.startswith("{"):
            return None
        if not text and tool_bits:
            text = tool_bits[-1]
            return _attach_at(
                {"role": "assistant", "text": text[:200], "meta": "tool"}, obj
            )
        if text and not text.startswith("{"):
            return _attach_at({"role": "assistant", "text": text[:24000]}, obj)
    return None


CHAT_LIMIT = 200
_CHAT_CACHE_VER = 10


def updates_turns(session_dir: Path) -> list[dict]:
    """First timestamp + assembled text of each user/assistant turn in updates.jsonl."""
    rows = last_jsonl_tail(session_dir / "updates.jsonl", 600_000)
    turns: list[dict] = []
    cur: dict | None = None
    for obj in rows:
        upd = (obj.get("params") or {}).get("update") or {}
        kind = str(upd.get("sessionUpdate") or "")
        if kind not in {"user_message_chunk", "agent_message_chunk"}:
            continue
        role = "user" if kind.startswith("user") else "assistant"
        content = upd.get("content")
        if isinstance(content, dict):
            chunk = str(content.get("text") or "")
        else:
            chunk = str(content or "")
        if role == "user" and (
            chunk.lstrip().startswith("<system-reminder>")
            or "this session is being continued" in chunk.lower()
            or "Primary Request and Intent:" in chunk
        ):
            if cur:
                turns.append(cur)
                cur = None
            continue
        if cur and cur.get("role") == role:
            cur["text"] += chunk
            continue
        if cur:
            turns.append(cur)
        cur = {"role": role, "at": obj.get("timestamp"), "text": chunk}
    if cur:
        turns.append(cur)
    return turns


def stamp_chat_times(session_dir: Path, msgs: list[dict]) -> list[dict]:
    """Copy Grok update timestamps onto parsed chat rows that lack `at`."""
    if not msgs:
        return msgs
    if all(m.get("at") for m in msgs):
        return msgs
    turns = updates_turns(session_dir)
    if not turns:
        return msgs
    i = 0
    n = len(turns)
    for m in msgs:
        role = m.get("role")
        if role not in {"user", "assistant"}:
            continue
        while i < n and turns[i].get("role") != role:
            i += 1
        if i >= n:
            break
        t = turns[i]
        if role == "user":
            needle = _norm_txt(m.get("text"))[:48]
            blob = _norm_txt(t.get("text"))
            if needle and not (needle in blob or blob[:48] in needle):
                continue
        if m.get("at") is None and t.get("at") is not None:
            m["at"] = t["at"]
        if role == "user":
            i += 1
    return msgs


def parse_chat(session_dir: Path, limit: int = CHAT_LIMIT) -> list[dict]:
    hist = session_dir / "chat_history.jsonl"
    key = str(hist)
    prev = _chat_cache.get(key)
    if not hist.is_file():
        return (prev.get("msgs") or [])[-limit:] if prev else []
    try:
        st = hist.stat()
    except OSError:
        return (prev.get("msgs") or [])[-limit:] if prev else []
    fresh = bool(prev and prev.get("ver") == _CHAT_CACHE_VER)
    if fresh and prev["mtime"] == st.st_mtime and prev["size"] == st.st_size:
        return prev["msgs"][-limit:]
    acc: list[dict] = []
    start = 0
    leftover = b""
    if fresh and 0 < prev["size"] <= st.st_size:
        start = int(prev["size"])
        acc = list(prev["msgs"])
        leftover = prev.get("tail") or b""
    try:
        with hist.open("rb") as f:
            if start:
                f.seek(start)
                chunk = leftover + f.read()
            else:
                # Full file when small enough: user lines sit at the start,
                # fat tool dumps at the end would hide the conversation.
                if st.st_size <= 8_000_000:
                    chunk = f.read()
                else:
                    f.seek(st.st_size - 2_000_000)
                    chunk = f.read().split(b"\n", 1)[-1]
    except OSError:
        return prev["msgs"][-limit:] if prev else []
    parts = chunk.split(b"\n")
    incomplete = b""
    if chunk and not chunk.endswith(b"\n"):
        incomplete = parts[-1]
        parts = parts[:-1]
    for line in parts:
        if not line.strip():
            continue
        msg = _line_to_msg(line)
        if msg:
            acc.append(msg)
    if not acc and prev and prev.get("msgs"):
        return prev["msgs"][-limit:]
    if len(acc) > 400:
        acc = acc[-400:]
    acc = stamp_chat_times(session_dir, acc)
    _chat_cache[key] = {
        "ver": _CHAT_CACHE_VER,
        "mtime": st.st_mtime,
        "size": st.st_size,
        "msgs": acc,
        "tail": incomplete,
    }
    return acc[-limit:]


def find_window(wid: int) -> dict | None:
    try:
        wid = int(wid)
    except (TypeError, ValueError):
        return None
    for win in list_windows_cached():
        if win["id"] == wid:
            return win
    rost = rosterlib.load_roster()
    for slug, meta in (rost.get("agents") or {}).items():
        try:
            mid = int(meta.get("window_id")) if meta.get("window_id") is not None else None
        except (TypeError, ValueError):
            mid = None
        if mid == wid:
            return {
                "id": wid,
                "app": "Terminal",
                "title": meta.get("title") or meta.get("label") or slug,
                "tmux": meta.get("tmux"),
                "slug": slug,
                "x": 0,
                "y": 0,
                "w": 800,
                "h": 600,
                "pid": 0,
                "minimized": True,
            }
    return None


def screen_window() -> dict:
    bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    return {
        "id": 0,
        "app": "iMac",
        "title": "Heel scherm",
        "x": float(bounds.origin.x),
        "y": float(bounds.origin.y),
        "w": float(bounds.size.width),
        "h": float(bounds.size.height),
        "pid": 0,
    }


def resolve_target(body: dict) -> dict:
    raw = body.get("id", "screen")
    if raw in (None, "", "screen", "full", 0, "0"):
        return screen_window()
    win = find_window(int(raw))
    if not win:
        raise RuntimeError("window is gone")
    return enrich_window(win)


_tty_cache: tuple[float, list[dict]] = (0.0, [])


def _norm_tty(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    if not s.startswith("/dev/"):
        s = "/dev/" + s.lstrip("/")
    return s


def terminal_tabs() -> list[dict]:
    """Stable Terminal.app identity: window id + tty, never the changing Grok title."""
    global _tty_cache
    now = time.time()
    if now - _tty_cache[0] < 1.5:
        return _tty_cache[1]
    try:
        r = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "Terminal"',
                "-e",
                "set out to \"\"",
                "-e",
                "repeat with w in windows",
                "-e",
                'set out to out & (id of w as text) & (character id 9) & (tty of selected tab of w) & (character id 9) & (name of w) & linefeed',
                "-e",
                "end repeat",
                "-e",
                "return out",
                "-e",
                "end tell",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (subprocess.TimeoutExpired, OSError):
        return _tty_cache[1]
    tabs: list[dict] = []
    if r.returncode == 0:
        for line in (r.stdout or "").splitlines():
            if "\t" not in line:
                continue
            parts = line.split("\t")
            wid = None
            if len(parts) >= 3:
                try:
                    wid = int(parts[0].strip())
                except ValueError:
                    wid = None
                tty, name = parts[1].strip(), parts[2].strip()
            else:
                tty, name = parts[0].strip(), parts[1].strip()
            tty = _norm_tty(tty)
            if tty:
                tabs.append({"id": wid, "tty": tty, "title": name})
    _tty_cache = (now, tabs)
    return tabs


def enrich_window(win: dict) -> dict:
    if not win:
        return win
    rost = rosterlib.load_roster()
    for slug, meta in (rost.get("agents") or {}).items():
        same = meta.get("window_id") == win.get("id") or (
            win.get("tmux") and meta.get("tmux") == win.get("tmux")
        )
        if not same:
            continue
        win["slug"] = slug
        if meta.get("tmux"):
            win["tmux"] = meta["tmux"]
        if meta.get("tty"):
            win["tty"] = meta["tty"]
        break
    if not win.get("tmux") and not win.get("tty"):
        tty = match_tty(win)
        if tty:
            win["tty"] = tty
    return win


def focus_window_id(wid: int) -> bool:
    """Raise a CG window and click it. Avoids Terminal's hanging AppleScript."""
    try:
        wid = int(wid)
    except (TypeError, ValueError):
        return False
    opts = Quartz.kCGWindowListExcludeDesktopElements
    raw = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
    hit = None
    for item in raw:
        try:
            if int(item.get("kCGWindowNumber") or 0) != wid:
                continue
        except Exception:
            continue
        hit = item
        break
    if not hit:
        return False
    try:
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication
        pid = int(hit.get("kCGWindowOwnerPID") or 0)
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if pid else None
        if app:
            app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    except Exception:
        pass
    bounds = hit.get("kCGWindowBounds") or {}
    x = float(bounds.get("X") or 0) + max(40.0, float(bounds.get("Width") or 80) * 0.5)
    h = float(bounds.get("Height") or 200)
    y = float(bounds.get("Y") or 0) + max(80.0, h - 36.0)
    try:
        down = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventLeftMouseDown, (x, y), Quartz.kCGMouseButtonLeft
        )
        up = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventLeftMouseUp, (x, y), Quartz.kCGMouseButtonLeft
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
    except Exception:
        return False
    return True


def focus_terminal_title(hint: str) -> bool:
    """Raise a Terminal window by title via System Events — Terminal's own dictionary hangs."""
    hint = (hint or "").strip()
    if len(hint) < 4:
        return False
    try:
        r = subprocess.run(
            [
                "osascript",
                "-e",
                "on run argv",
                "-e",
                "set hint to item 1 of argv",
                "-e",
                'tell application "System Events"',
                "-e",
                'if not (exists process "Terminal") then return "miss"',
                "-e",
                'tell process "Terminal"',
                "-e",
                "set frontmost to true",
                "-e",
                "repeat with w in windows",
                "-e",
                "try",
                "-e",
                "set n to name of w as text",
                "-e",
                "if n contains hint then",
                "-e",
                'perform action "AXRaise" of w',
                "-e",
                'return "ok"',
                "-e",
                "end if",
                "-e",
                "end try",
                "-e",
                "end repeat",
                "-e",
                "end tell",
                "-e",
                "end tell",
                "-e",
                'return "miss"',
                "-e",
                "end run",
                hint,
            ],
            capture_output=True,
            text=True,
            timeout=2.5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0 and "ok" in (r.stdout or "")


def focus_tty(tty: str) -> bool:
    if not tty:
        return False
    try:
        r = subprocess.run(
            [
                "osascript",
                "-e",
                "on run argv",
                "-e",
                "set want to item 1 of argv",
                "-e",
                "set short to item 2 of argv",
                "-e",
                'tell application "Terminal"',
                "-e",
                "activate",
                "-e",
                "repeat with w in windows",
                "-e",
                "repeat with t in tabs of w",
                "-e",
                "try",
                "-e",
                "set have to (tty of t) as text",
                "-e",
                'if have is want or have is short or ("/dev/" & have) is want then',
                "-e",
                "set selected of t to true",
                "-e",
                "set frontmost of w to true",
                "-e",
                'return "ok"',
                "-e",
                "end if",
                "-e",
                "end try",
                "-e",
                "end repeat",
                "-e",
                "end repeat",
                "-e",
                "end tell",
                "-e",
                'return "miss"',
                "-e",
                "end run",
                tty,
                tty.replace("/dev/", ""),
            ],
            capture_output=True,
            text=True,
            timeout=2.5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0 and "ok" in (r.stdout or "")


def front_tty() -> str:
    r = subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "Terminal" to get tty of selected tab of front window',
        ],
        capture_output=True,
        text=True,
        timeout=4,
    )
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def resolve_delivery(body: dict) -> dict:
    """Pick exactly one bot. Prefer tmux name / slug so a stale window id cannot leak."""
    rost = rosterlib.load_roster()
    tmux = str(body.get("tmux") or "").strip()
    if tmux.startswith("heavy-"):
        return {"tmux": tmux, "id": body.get("id"), "slug": body.get("slug")}
    slug = str(body.get("slug") or "").strip()
    if slug and slug in (rost.get("agents") or {}):
        meta = rost["agents"][slug]
        if meta.get("tmux"):
            return {
                "tmux": meta["tmux"],
                "id": meta.get("window_id"),
                "slug": slug,
                "tty": meta.get("tty"),
            }
        if meta.get("window_id"):
            win = find_window(int(meta["window_id"]))
            if win:
                win["slug"] = slug
                if meta.get("tty"):
                    win["tty"] = meta["tty"]
                return enrich_window(win)
    win = resolve_target(body)
    return enrich_window(win)


_TITLE_STOP = {
    "timgrootes",
    "grok",
    "terminal",
    "waiting",
    "response",
    "thinking",
    "dump",
    "open",
    "preparing",
    "always",
    "approve",
}


def _title_tokens(s: str) -> set[str]:
    words = re.findall(r"[a-z0-9]{4,}", (s or "").lower())
    return {w for w in words if w not in _TITLE_STOP}


def unique_title_hint(title: str) -> str:
    """Fragment that identifies one Terminal, never the shared username."""
    raw = re.sub(r"^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏●○]\s*", "", title or "")
    bits = re.split(r"\s*[—–-]\s*", raw)
    for bit in bits:
        bit = bit.strip()
        if len(bit) < 8:
            continue
        low = bit.lower()
        if low.startswith("timgrootes") or "grok" in low:
            continue
        if _title_tokens(bit):
            return bit[:42]
    return ""


def persist_tty(win: dict) -> None:
    slug = win.get("slug")
    tty = _norm_tty(win.get("tty") or "")
    if not slug or not tty:
        return
    rost = rosterlib.load_roster()
    agents = rost.get("agents") or {}
    if slug not in agents:
        return
    try:
        wid = int(win["id"]) if win.get("id") not in (None, "") else None
    except (TypeError, ValueError):
        wid = None
    for other, meta in agents.items():
        if other == slug:
            continue
        if _norm_tty(str(meta.get("tty") or "")) != tty:
            continue
        try:
            oid = int(meta["window_id"]) if meta.get("window_id") is not None else None
        except (TypeError, ValueError):
            oid = None
        if oid is not None and wid is not None and oid != wid:
            return
    agents[slug]["tty"] = tty
    rosterlib.save_roster(rost)


def tty_of_pid(pid: int) -> str:
    try:
        r = subprocess.run(
            ["ps", "-o", "tty=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return ""
    t = (r.stdout or "").strip()
    if not t or t == "??":
        return ""
    return t if t.startswith("/dev/") else "/dev/" + t


def tty_for_session(sid: str) -> str:
    """TTY of the grok process that already owns this session id."""
    sid = str(sid or "").strip()
    if not sid:
        return ""
    try:
        rows = json.loads((Path.home() / ".grok" / "active_sessions.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("session_id") or "") != sid:
            continue
        try:
            pid = int(row.get("pid"))
        except (TypeError, ValueError):
            continue
        tty = tty_of_pid(pid)
        if tty:
            return tty
    return ""


_tty_sid_cache: dict[str, tuple[float, str]] = {}
_SID_CMD_RE = re.compile(r"(?:--session-id|--resume)[=\s]+(\S+)")


def live_session_id_for_tty(tty: str) -> str:
    """Grok --session-id of the process actually sitting on this Terminal tab."""
    tty = _norm_tty(tty)
    if not tty:
        return ""
    now = time.time()
    hit = _tty_sid_cache.get(tty)
    if hit and now - hit[0] < 8:
        return hit[1]
    sid = ""
    try:
        rows = json.loads((Path.home() / ".grok" / "active_sessions.json").read_text(encoding="utf-8"))
    except Exception:
        rows = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get("pid"))
        except (TypeError, ValueError):
            continue
        if _norm_tty(tty_of_pid(pid)) == tty:
            sid = str(row.get("session_id") or "").strip()
            if sid:
                break
    if not sid:
        short = tty.replace("/dev/", "")
        try:
            r = subprocess.run(
                ["ps", "-ax", "-o", "tty=,command="],
                capture_output=True,
                text=True,
                timeout=1.2,
            )
        except Exception:
            r = None
        if r and r.returncode == 0:
            for line in (r.stdout or "").splitlines():
                if short not in line or "--session-id" not in line:
                    continue
                parts = line.split(None, 1)
                if not parts:
                    continue
                if _norm_tty(parts[0]) != tty:
                    continue
                m = _SID_CMD_RE.search(line)
                if m:
                    sid = m.group(1).strip().strip("\"'")
                    break
    if sid:
        _tty_sid_cache[tty] = (now, sid)
    return sid


def match_tty(win: dict) -> str:
    """Map this bot to its Terminal tab by live session, window id, then unique title."""
    slug = ""
    try:
        slug = slug_for_window(win) or str(win.get("slug") or "")
    except Exception:
        slug = str(win.get("slug") or "")
    meta = {}
    try:
        meta = (rosterlib.load_roster().get("agents") or {}).get(slug) or {}
    except Exception:
        meta = {}
    sid = str(win.get("session_id") or meta.get("session_id") or "")
    bound = _norm_tty(tty_for_session(sid))
    if bound:
        return bound
    tabs = terminal_tabs()
    try:
        wid = int(win["id"]) if win.get("id") not in (None, "") else None
    except (TypeError, ValueError):
        wid = None
    by_id = {t["id"]: t for t in tabs if t.get("id") is not None}
    if wid is not None and wid in by_id:
        return by_id[wid]["tty"]
    want = _title_tokens(
        " ".join(
            str(x or "")
            for x in (win.get("title"), win.get("label"), win.get("slug"))
        )
    )
    if want:
        scored: list[tuple[int, dict]] = []
        for tab in tabs:
            score = len(want & _title_tokens(tab.get("title") or ""))
            if score:
                scored.append((score, tab))
        scored.sort(key=lambda x: -x[0])
        if scored and scored[0][0] >= 2 and (
            len(scored) == 1 or scored[0][0] > scored[1][0]
        ):
            return scored[0][1]["tty"]
    have = _norm_tty(str(win.get("tty") or ""))
    if have:
        owner = next((t for t in tabs if _norm_tty(t.get("tty")) == have), None)
        if owner and (wid is None or owner.get("id") == wid):
            return have
    title = (win.get("title") or "").strip()
    if title:
        exact = [t for t in tabs if t.get("title") == title]
        if len(exact) == 1:
            return exact[0]["tty"]
    return ""


def keystroke_return() -> None:
    subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to key code 36',
        ],
        capture_output=True,
        timeout=3,
    )


def _bind_tmux(slug: str, name: str) -> None:
    if not slug or not name:
        return
    rost = rosterlib.load_roster()
    if slug in (rost.get("agents") or {}):
        rost["agents"][slug]["tmux"] = name
        rosterlib.save_roster(rost)


def ensure_tmux_for_agent(slug: str, meta: dict | None = None) -> str:
    """Live tmux session for this bot, or start one."""
    meta = meta or {}
    live = {s["tmux"] for s in agents_tmux.list_sessions(include_helpers=True)}
    name = str(meta.get("tmux") or "")
    if name in live:
        return name
    guess = agents_tmux.PREFIX + slug
    if guess in live:
        _bind_tmux(slug, guess)
        _bind_tmux_window(slug, {"tmux": guess, "id": agents_tmux.session_id(guess)})
        return guess
    if not slug:
        raise RuntimeError("no bot to start")
    sid = str(meta.get("session_id") or "") or None
    resume = bool(sid and session_dir_by_id(sid))
    info = agents_tmux.spawn(
        label=slug,
        cwd=meta.get("cwd") or "",
        ai=meta.get("ai") or "grok",
        sid=sid,
        model=meta.get("model") or "",
        resume=resume,
    )
    name = info.get("tmux") or guess
    agents_tmux._list_cache = (0.0, [])
    _bind_tmux(slug, name)
    _bind_tmux_window(slug, info)
    return name


_adopt_lock = threading.Lock()


def _pid_cmd(pid: int) -> str:
    try:
        r = subprocess.run(
            ["ps", "-o", "command=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return ""
    return r.stdout or ""


def grok_pid_for_session(sid: str) -> int:
    sid = str(sid or "").strip()
    if not sid:
        return 0
    try:
        rows = json.loads((Path.home() / ".grok" / "active_sessions.json").read_text(encoding="utf-8"))
    except Exception:
        rows = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("session_id") or "") != sid:
            continue
        try:
            return int(row.get("pid") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def release_session_locks(sid: str) -> None:
    """Drop stale grok locks so the same session can be reopened in tmux."""
    sid = str(sid or "").strip()
    if not sid or "/" in sid or ".." in sid:
        return
    paths = [Path.home() / ".grok" / "relocations" / f"{sid}.lock"]
    pth = session_dir_by_id(sid)
    if pth and pth.is_dir():
        paths.extend(pth.glob("*.lock"))
    for fp in paths:
        try:
            if fp.is_file():
                fp.unlink()
        except Exception:
            pass


def stop_native_grok(slug: str, meta: dict | None = None) -> None:
    """Stop the Terminal.app grok for this bot so tmux can take the same session."""
    meta = meta or {}
    sid = str(meta.get("session_id") or "")
    pid = grok_pid_for_session(sid)
    cmd = _pid_cmd(pid) if pid else ""
    if pid and re.search(r"\b(grok|claude|codex)\b", cmd):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pid = 0
        except Exception:
            pass
        if pid:
            for _ in range(8):
                time.sleep(0.08)
                if not _pid_cmd(pid).strip():
                    break
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass
    tty = _norm_tty(str(meta.get("tty") or ""))
    if not tty and sid:
        tty = _norm_tty(tty_for_session(sid))
    if tty:
        try:
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    "on run argv",
                    "-e",
                    "set want to item 1 of argv",
                    "-e",
                    'tell application "Terminal"',
                    "-e",
                    "repeat with w in windows",
                    "-e",
                    "try",
                    "-e",
                    "if (tty of selected tab of w) is want then",
                    "-e",
                    "close w",
                    "-e",
                    "return",
                    "-e",
                    "end if",
                    "-e",
                    "end try",
                    "-e",
                    "end repeat",
                    "-e",
                    "end tell",
                    "-e",
                    "end run",
                    tty,
                ],
                capture_output=True,
                timeout=1.6,
            )
        except Exception:
            pass
    if sid:
        release_session_locks(sid)


def _bind_tmux_window(slug: str, info: dict) -> None:
    if not slug:
        return
    rost = rosterlib.load_roster()
    if slug not in (rost.get("agents") or {}):
        return
    rec = rost["agents"][slug]
    if info.get("tmux"):
        rec["tmux"] = info["tmux"]
    if info.get("session_id"):
        rec["session_id"] = info["session_id"]
    if info.get("id"):
        rec["window_id"] = info["id"]
    if info.get("cwd"):
        rec["cwd"] = info["cwd"]
    rec.pop("tty", None)
    rosterlib.save_roster(rost)


def ensure_hidden_tmux(slug: str, meta: dict | None = None, win: dict | None = None) -> str:
    """Prefer a hidden tmux bot. Adopt native Terminal tabs (Ads/Degero/Video) once."""
    if not slug:
        raise RuntimeError("no bot")
    packed = {**(meta or {})}
    if win:
        for k in ("tmux", "session_id", "cwd", "ai", "model", "tty"):
            if win.get(k) and not packed.get(k):
                packed[k] = win[k]
    with _adopt_lock:
        live = {s["tmux"] for s in agents_tmux.list_sessions(include_helpers=True)}
        name = str(packed.get("tmux") or "")
        if name in live:
            return name
        guess = agents_tmux.PREFIX + slug
        if guess in live:
            _bind_tmux(slug, guess)
            _bind_tmux_window(slug, {"tmux": guess, "id": agents_tmux.session_id(guess)})
            return guess
        if packed.get("tty") or packed.get("session_id"):
            if not packed.get("cwd"):
                packed["cwd"] = str(Path.home())
            stop_native_grok(slug, packed)
            time.sleep(0.15)
        elif packed.get("session_id"):
            release_session_locks(str(packed.get("session_id") or ""))
        return ensure_tmux_for_agent(slug, packed)


def _focus_slug_window(slug: str) -> bool:
    """Quartz window id can go stale; find this bot again and raise it."""
    if not slug:
        return False
    for w in list_windows():
        if slug_for_window(w) != slug:
            continue
        try:
            wid = int(w.get("id"))
        except (TypeError, ValueError):
            continue
        if focus_window_id(wid):
            rost = rosterlib.load_roster()
            if slug in (rost.get("agents") or {}):
                rost["agents"][slug]["window_id"] = wid
                rosterlib.save_roster(rost)
            return True
    return False


def steer_into_chat(win: dict, slug: str, text: str, interrupt_first: bool = True) -> dict:
    """Put a 2nd message into this bot's Grok terminal and actually send it.

    Pasting during a live turn is dropped. Stop first when the user chose
    This chat; otherwise wait until the current turn finishes.
    """
    text = (text or "").strip()
    if not text:
        raise RuntimeError("nothing to send")
    if not slug:
        slug = str(win.get("slug") or slug_for_window(win) or "")
    try:
        rosterlib.remember_swarm_msg(slug, "user", text)
    except Exception:
        pass
    busy = False
    try:
        busy = live_busy(win) or recently_submitted(slug)
    except Exception:
        busy = False
    if interrupt_first and busy:
        try:
            interrupt_chat(win)
        except Exception as exc:
            print("steer interrupt", exc, flush=True)
    if not busy:
        deliver_text(win, text, True)
        _mark_submit(slug)
        return {"ok": True, "via": "steer", "text": text}
    tmux = str(win.get("tmux") or "")
    if not tmux:
        try:
            rost = rosterlib.load_roster()
            meta = (rost.get("agents") or {}).get(slug) or {}
            tmux = ensure_hidden_tmux(slug, meta, win)
            win["tmux"] = tmux
        except Exception as exc:
            print("steer tmux", exc, flush=True)
            deliver_text(win, text, True)
            _mark_submit(slug)
            return {"ok": True, "via": "steer", "text": text}
    threading.Thread(
        target=_send_when_ready, args=(tmux, text, 80.0), daemon=True
    ).start()
    _mark_submit(slug)
    return {"ok": True, "via": "steer", "text": text}


def deliver_text(win: dict, text: str, submit: bool) -> None:
    """Send only via hidden tmux. Never raise or type into a Terminal.app window."""
    slug = str(win.get("slug") or slug_for_window(win) or "")
    rost = rosterlib.load_roster()
    meta = (rost.get("agents") or {}).get(slug) or {}
    try:
        name = ensure_hidden_tmux(slug, meta, win)
    except Exception as e:
        raise RuntimeError("could not reach this bot") from e
    win["tmux"] = name
    agents_tmux.send_text(name, text, enter=submit)


_stall_fp: dict[str, tuple[str, float]] = {}
_busy_seen: dict[str, float] = {}
_last_unstick: dict[str, float] = {}
# Real work (App Store API, locale edits, thinking) often sits on one
# tool line for minutes. Only treat that as dead after a long freeze.
_STALL_FREEZE = {"default": 480.0, "command": 720.0, "media": 720.0, "gui": 300.0}
_STALL_GUI_MAX = 15 * 60
_STALL_HARD_MAX = 40 * 60
_UNSTICK_COOLDOWN = 90.0
_LONG_TOOL_RE = re.compile(
    r"(?i)image_gen|image_edit|image_to_video|reference_to_video|\bvideo\b"
)


def stall_kind(pane: str) -> str:
    t = pane or ""
    if re.search(r"command still running", t, re.I):
        return "command"
    if _LONG_TOOL_RE.search(t):
        return "media"
    if agents_tmux.pane_gui_loop(t):
        return "gui"
    return "default"


def stall_reason(pane: str, *, frozen_for: float, busy_for: float) -> str | None:
    """Why this turn should be cancelled, or None if it is still making progress."""
    if not agents_tmux.pane_busy(pane or "") or agents_tmux.pane_overlay(pane or ""):
        return None
    kind = stall_kind(pane)
    if frozen_for >= _STALL_FREEZE.get(kind, _STALL_FREEZE["default"]):
        return "geen voortgang"
    if kind == "gui" and busy_for >= _STALL_GUI_MAX:
        return "browser/klik-lus"
    if busy_for >= _STALL_HARD_MAX:
        return "te lang bezig"
    return None


def unstick_stalled() -> list[dict]:
    """Stop turns that are spinning without progress (frozen pane or GUI loop)."""
    now = time.time()
    stopped: list[dict] = []
    for sess in agents_tmux.list_sessions(include_helpers=True):
        tmux = str(sess.get("tmux") or "")
        slug = str(sess.get("slug") or "")
        if not tmux or not sess.get("busy"):
            _stall_fp.pop(tmux, None)
            _busy_seen.pop(tmux, None)
            continue
        try:
            pane = agents_tmux.capture_pane(tmux, 40)
        except Exception:
            continue
        if not agents_tmux.pane_busy(pane) or agents_tmux.pane_overlay(pane):
            _stall_fp.pop(tmux, None)
            _busy_seen.pop(tmux, None)
            continue
        fp = agents_tmux.pane_fingerprint(pane)
        prev = _stall_fp.get(tmux)
        if not prev or prev[0] != fp:
            _stall_fp[tmux] = (fp, now)
            frozen_for = 0.0
        else:
            frozen_for = now - prev[1]
        # How long this busy episode has lasted — not a stale roster clock
        # from an earlier turn this morning.
        seen = _busy_seen.setdefault(tmux, now)
        busy_for = now - seen
        reason = stall_reason(pane, frozen_for=frozen_for, busy_for=busy_for)
        if not reason:
            continue
        if now - _last_unstick.get(tmux, 0) < _UNSTICK_COOLDOWN:
            continue
        slug = str(sess.get("slug") or "")
        try:
            last_sub = float(
                (((rosterlib.load_roster().get("agents") or {}).get(slug) or {}).get("last_submit_at")) or 0
            )
        except Exception:
            last_sub = 0
        if last_sub and now - last_sub < 120:
            continue
        try:
            agents_tmux.interrupt(tmux)
        except Exception as exc:
            print("unstick interrupt", tmux, exc, flush=True)
            continue
        _last_unstick[tmux] = now
        _stall_fp.pop(tmux, None)
        _busy_since.pop(slug, None)
        _busy_idle_since.pop(slug, None)
        note = f"Gestopt: vastgelopen ({reason})."
        try:
            rosterlib.remember_swarm_msg(slug, "assistant", note, meta="progress")
        except Exception:
            pass
        stopped.append({"slug": slug, "tmux": tmux, "reason": reason})
        print("unstick", slug, reason, flush=True)
    return stopped


def interrupt_chat(win: dict) -> dict:
    """Cancel this chat now: running command, model turn, and extra agents."""
    slug = str(win.get("slug") or slug_for_window(win) or "")
    rost = rosterlib.load_roster()
    meta = (rost.get("agents") or {}).get(slug) or {}
    live_rows = agents_tmux.list_sessions(include_helpers=True)
    live = {s.get("tmux") for s in live_rows}
    name = str(win.get("tmux") or meta.get("tmux") or "")
    if name not in live:
        guess = agents_tmux.PREFIX + slug if slug else ""
        name = guess if guess in live else ""
    if not name:
        raise RuntimeError("no live chat")
    print("stop", slug, name, flush=True)
    agents_tmux.interrupt(name, force=True)
    try:
        time.sleep(0.18)
        if agents_tmux.pane_busy(agents_tmux.capture_pane(name, 24)):
            agents_tmux.interrupt(name, force=True)
    except Exception:
        pass
    for h in rosterlib.helpers_of(slug):
        tm = str((h or {}).get("tmux") or "")
        if tm and tm in live:
            try:
                agents_tmux.interrupt(tm, force=True)
            except Exception:
                pass
    mark_stopped(slug)
    clear_last_submit(slug)
    if slug:
        try:
            path = rosterlib.agent_dir(slug) / "live_note.json"
            if path.is_file():
                path.unlink()
        except Exception:
            pass
        try:
            rosterlib.remember_swarm_msg(slug, "assistant", "Stopped", meta="progress")
        except Exception:
            pass
    return {
        "ok": True,
        "stopped": True,
        "busy": False,
        "activity": "Ready",
        "progress": _idle_progress(),
        "tmux": name,
        "slug": slug,
        "last_submit_at": 0,
    }


def delete_bot(slug: str) -> None:
    slug = str(slug or "").strip()
    if not slug:
        raise RuntimeError("no bot")
    rost = rosterlib.load_roster()
    meta = dict((rost.get("agents") or {}).get(slug) or {})
    try:
        rosterlib.remember_forgotten(slug, meta)
    except Exception:
        pass
    try:
        rosterlib.forget_nicks(meta)
    except Exception:
        pass
    try:
        kill_helpers(slug)
    except Exception:
        pass
    tmux = str(meta.get("tmux") or "")
    if tmux:
        try:
            agents_tmux.kill(tmux)
        except Exception:
            pass
    try:
        stop_native_grok(slug, meta)
    except Exception:
        pass
    try:
        wid = int(meta["window_id"]) if meta.get("window_id") not in (None, "") else None
    except (TypeError, ValueError):
        wid = None
    if wid is not None:
        try:
            close_terminal_by_id(wid)
        except Exception:
            pass
    rosterlib.drop_agent(slug=slug)


def capture_jpeg(window_id: int | None, max_w: int, quality: float) -> bytes:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"cap-{os.getpid()}-{threading.get_ident()}.jpg"
    cmd = ["screencapture", "-x", "-t", "jpg"]
    if window_id:
        cmd.extend(["-l", str(window_id), "-o"])
    cmd.append(str(path))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        if r.returncode != 0 or not path.exists() or path.stat().st_size < 40:
            raise RuntimeError((r.stderr or r.stdout or "capture failed").strip())
        if max_w:
            subprocess.run(
                ["sips", "-Z", str(int(max_w)), str(path)],
                capture_output=True,
                timeout=8,
            )
        return path.read_bytes()
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


def cliclick(*parts: str) -> None:
    cmd = ["cliclick", *parts]
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=4)


def clipboard_get() -> bytes:
    return subprocess.check_output(["pbpaste"], timeout=3)


def clipboard_set(data: bytes) -> None:
    subprocess.run(["pbcopy"], input=data, check=True, timeout=3)


def paste_text(text: str) -> None:
    prev = b""
    try:
        prev = clipboard_get()
    except Exception:
        prev = b""
    try:
        clipboard_set(text.encode("utf-8"))
        time.sleep(0.06)
        subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to keystroke "v" using command down',
            ],
            check=True,
            capture_output=True,
            timeout=4,
        )
        time.sleep(0.1)
    finally:
        try:
            clipboard_set(prev)
        except Exception:
            pass


def type_text(text: str) -> None:
    if not text:
        return
    # cliclick t: is unreliable for spaces / unicode; paste into the focused window
    paste_text(text)


SPECIAL_KEYS = {
    "enter": "kp:return",
    "return": "kp:return",
    "esc": "kp:esc",
    "escape": "kp:esc",
    "tab": "kp:tab",
    "backspace": "kp:delete",
    "delete": "kp:fwd-delete",
    "space": "kp:space",
    "up": "kp:arrow-up",
    "down": "kp:arrow-down",
    "left": "kp:arrow-left",
    "right": "kp:arrow-right",
    "cmd-w": "kd:cmd t:w ku:cmd",
    "cmd-n": "kd:cmd t:n ku:cmd",
    "cmd-t": "kd:cmd t:t ku:cmd",
    "cmd-q": "kd:cmd t:q ku:cmd",
    "cmd-s": "kd:cmd t:s ku:cmd",
    "cmd-c": "kd:cmd t:c ku:cmd",
    "cmd-v": "kd:cmd t:v ku:cmd",
    "cmd-z": "kd:cmd t:z ku:cmd",
    "cmd-a": "kd:cmd t:a ku:cmd",
    "cmd-l": "kd:cmd t:l ku:cmd",
}


def send_key(name: str) -> None:
    spec = SPECIAL_KEYS.get(name.lower().strip())
    if not spec:
        raise ValueError(f"unknown key {name}")
    cliclick(*spec.split())


def focus_window(win: dict) -> None:
    cliclick(f"c:{int(win['x'] + min(80, win['w'] / 2))},{int(win['y'] + 14)}")
    time.sleep(0.08)


def focus_prompt(win: dict) -> None:
    """Bring the chosen Terminal window to front by title — do not click overlapping panes."""
    if win.get("minimized"):
        unminimize_terminals()
        time.sleep(0.2)
    app = win.get("app") or "Terminal"
    title = win.get("title") or ""
    if title:
        subprocess.run(
            [
                "osascript",
                "-e",
                "on run argv",
                "-e",
                "set appName to item 1 of argv",
                "-e",
                "set winName to item 2 of argv",
                "-e",
                "tell application appName to activate",
                "-e",
                'tell application "System Events" to tell process appName',
                "-e",
                "set frontmost to true",
                "-e",
                "try",
                "-e",
                "perform action \"AXRaise\" of (first window whose name is winName)",
                "-e",
                "end try",
                "-e",
                "end tell",
                "-e",
                "end run",
                app,
                title,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        time.sleep(0.12)
        return
    cliclick(f"c:{int(win['x'] + min(80, win['w'] / 2))},{int(win['y'] + 12)}")
    time.sleep(0.1)


def close_window(win: dict) -> None:
    slug = slug_for_window(win) or str(win.get("slug") or "")
    if slug:
        delete_bot(slug)
        return
    if win.get("tmux"):
        try:
            agents_tmux.kill(win["tmux"])
        except Exception:
            pass
    try:
        rosterlib.drop_agent(window_id=int(win["id"]))
    except Exception:
        pass


def new_window(win: dict) -> None:
    focus_window(win)
    app = win["app"].lower()
    if app in {"safari", "google chrome", "chrome"}:
        cliclick("kd:cmd", "t:t", "ku:cmd")
    else:
        cliclick("kd:cmd", "t:n", "ku:cmd")


def open_app(name: str, ai: str = "grok", model: str = "") -> dict | None:
    name = name.strip()
    if not name or "/" in name or ".." in name:
        raise ValueError("bad app name")
    if name.lower() == "terminal":
        return open_new_terminal(ai=ai, model=model)
    subprocess.run(["open", "-a", name], check=True, capture_output=True, timeout=8)
    return None


_last_ai_update: dict[str, float] = {}


def maybe_update_ai(ai: str) -> None:
    ai = rosterlib.normalize_ai(ai)
    now = time.time()
    if now - _last_ai_update.get(ai, 0) < 3600:
        return
    _last_ai_update[ai] = now
    try:
        exe = agents_tmux.bin_for(ai)
    except Exception:
        return
    subprocess.Popen(
        [exe, "update"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def maybe_update_grok() -> None:
    maybe_update_ai("grok")


def switch_bot_ai(slug: str, ai: str, model: str | None = None, force: bool = False) -> dict:
    ai = rosterlib.normalize_ai(ai)
    model = rosterlib.normalize_model(ai, model)
    agents_tmux.bin_for(ai)
    rost = rosterlib.load_roster()
    if slug not in rost.get("agents", {}):
        raise RuntimeError("unknown bot")
    meta = rost["agents"][slug]
    prev_ai = rosterlib.normalize_ai(meta.get("ai"))
    prev_model = rosterlib.normalize_model(prev_ai, meta.get("model"))
    tmux = str(meta.get("tmux") or "")
    running = agents_tmux.running_ai(tmux) if tmux else ""
    rost["agents"][slug]["ai"] = ai
    rost["agents"][slug]["model"] = model
    rosterlib.save_roster(rost)
    same_cfg = prev_ai == ai and prev_model == model
    same_proc = (not running) or running == ai
    if same_cfg and same_proc and not force:
        return {"changed": False, "ai": ai, "model": model, "running": running}
    maybe_update_ai(ai)
    if not tmux:
        return {"changed": True, "ai": ai, "model": model, "respawned": False, "running": running}
    sid = str(uuid.uuid4())
    info = agents_tmux.respawn(tmux, ai=ai, cwd=meta.get("cwd"), sid=sid, model=model)
    rost = rosterlib.load_roster()
    rost["agents"][slug]["session_id"] = info.get("session_id")
    rost["agents"][slug]["ai"] = ai
    rost["agents"][slug]["model"] = model
    if info.get("cwd"):
        rost["agents"][slug]["cwd"] = info["cwd"]
    rosterlib.save_roster(rost)
    return {
        "changed": True,
        "ai": ai,
        "model": model,
        "respawned": True,
        "session_id": info.get("session_id"),
        "running": ai,
    }


def open_new_terminal(ai: str = "grok", model: str = "") -> dict:
    ai = rosterlib.normalize_ai(ai)
    model = rosterlib.normalize_model(ai, model)
    maybe_update_ai(ai)
    started = time.time()
    info = agents_tmux.spawn(ai=ai, model=model)
    rost = rosterlib.load_roster()
    slug = info["slug"]
    rost["agents"][slug] = {
        "role": "worker",
        "auto": False,
        "label": "New bot",
        "window_id": info["id"],
        "tmux": info["tmux"],
        "title": info["title"],
        "session_id": info.get("session_id"),
        "cwd": info.get("cwd"),
        "ai": ai,
        "model": model,
    }
    order = rost.setdefault("order", [])
    if slug in order:
        order.remove(slug)
    order.insert(0, slug)
    rosterlib.agent_dir(slug)
    rosterlib.save_roster(rost)
    try:
        rosterlib.ensure_team_roles(rost)
    except Exception:
        pass
    if not info.get("session_id"):
        threading.Thread(target=bind_new_session, args=(slug, started), daemon=True).start()
    return info


def installed_apps() -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    finder = Path("/System/Library/CoreServices/Finder.app")
    if finder.is_dir():
        seen.add("Finder")
        names.append("Finder")
    for folder in (
        Path("/Applications"),
        Path("/System/Applications"),
        Path.home() / "Applications",
    ):
        if not folder.is_dir():
            continue
        for app in sorted(folder.glob("*.app")):
            n = app.stem
            if n in seen:
                continue
            seen.add(n)
            names.append(n)
    preferred = [n for n in PREFERRED_APPS if n in seen]
    rest = [n for n in names if n not in set(PREFERRED_APPS)]
    return preferred + rest


class App:
    def __init__(self, port: int, token: str, public: bool):
        self.port = port
        self.token = token
        self.public = public
        self.tunnel_proc: subprocess.Popen | None = None
        self.public_url: str | None = None
        self._stop = threading.Event()

    def authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        parsed = urlparse(handler.path)
        qs = parse_qs(parsed.query)
        k = (qs.get("k") or [None])[0]
        if not k:
            k = handler.headers.get("X-Remote-Key")
        if not k:
            auth = handler.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                k = auth[7:].strip()
        if not k:
            cookie = SimpleCookie()
            raw = handler.headers.get("Cookie") or ""
            cookie.load(raw)
            if "k" in cookie:
                k = cookie["k"].value
        if not k:
            return False
        try:
            return secrets.compare_digest(k, self.token)
        except Exception:
            return False

    def start_tunnel(self) -> str | None:
        cloudflared = shutil.which("cloudflared")
        if not cloudflared:
            print("! cloudflared ontbreekt — alleen Wi‑Fi")
            return None
        log_path = STATE_DIR / "tunnel.log"
        log_f = open(log_path, "w")
        proc = subprocess.Popen(
            [
                cloudflared,
                "tunnel",
                "--url",
                f"http://127.0.0.1:{self.port}",
                "--no-autoupdate",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.tunnel_proc = proc
        lines: list[str] = []
        lock = threading.Lock()

        def reader():
            assert proc.stdout is not None
            for line in proc.stdout:
                log_f.write(line)
                log_f.flush()
                with lock:
                    lines.append(line)

        threading.Thread(target=reader, daemon=True).start()
        url_re = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
        found = None
        registered = False
        deadline = time.time() + 50
        while time.time() < deadline:
            with lock:
                chunk = lines[:]
                lines.clear()
            for line in chunk:
                m = url_re.search(line)
                if m and not found:
                    found = m.group(0)
                    print(f"  tunnel: {found}")
                if "Registered tunnel connection" in line:
                    registered = True
            if found and registered:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        if not found:
            print(f"! tunnel startte niet — zie {log_path}")
            return None
        self.public_url = found
        return found

    def cleanup(self) -> None:
        self._stop.set()
        if self.tunnel_proc and self.tunnel_proc.poll() is None:
            self.tunnel_proc.terminate()
            try:
                self.tunnel_proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.tunnel_proc.kill()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"imac-phone/{VERSION}"

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _set_cookie(self) -> None:
            self.send_header(
                "Set-Cookie",
                f"k={app.token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800",
            )

        def _reject(self, code=HTTPStatus.UNAUTHORIZED) -> None:
            body = b'{"error":"unauthorized"}'
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status=200) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self._set_cookie()
            self.end_headers()
            self.wfile.write(raw)

        def _bytes(self, data: bytes, content_type: str, cache="no-store") -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", str(len(data)))
            self._set_cookie()
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self, max_n: int | None = None) -> dict:
            limit = JSON_BODY_MAX if max_n is None else max_n
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            if n > limit:
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(left, 65536))
                    if not chunk:
                        break
                    left -= len(chunk)
                return {"__error": "too_large", "__size": n}
            raw = self.rfile.read(n)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                return {}
            return data if isinstance(data, dict) else {}

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Remote-Key")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            if path in {"/", "/index.html"}:
                if not app.authorized(self):
                    self._reject()
                    return
                html = (STATIC / "index.html").read_text(encoding="utf-8")
                theme = "dark" if str(load_settings().get("theme") or "").lower() == "dark" else "light"
                html = html.replace('data-theme="light"', f'data-theme="{theme}"', 1)
                html = html.replace('data-theme="dark"', f'data-theme="{theme}"', 1)
                if theme == "dark":
                    for old in ('content="#e6e7ed"', 'content="#f4f5f8"', 'content="#14161c"'):
                        html = html.replace(old, 'content="#14161c"', 1)
                else:
                    for old in ('content="#14161c"', 'content="#0b0d12"', 'content="#e6e7ed"'):
                        html = html.replace(old, 'content="#e6e7ed"', 1)
                self._bytes(html.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path in {"/go", "/go.html"}:
                self._bytes((STATIC / "go.html").read_bytes(), "text/html; charset=utf-8")
                return
            if path in {"/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"}:
                fp = STATIC / "icons" / "apple-touch-icon.png"
                if fp.is_file():
                    self._bytes(fp.read_bytes(), "image/png", cache="public, max-age=86400")
                    return
            if path == "/connect.json":
                pub = ""
                try:
                    pub = URL_FILE.read_text(encoding="utf-8").splitlines()[0].strip()
                except Exception:
                    pass
                self._json(
                    {
                        "ok": True,
                        "lan": f"http://{lan_ip()}:{app.port}/?k={app.token}",
                        "pub": (f"{public_base()}/?k={app.token}" if public_base() else ""),
                        "cf": pub,
                    }
                )
                return
            if path == "/health":
                self._json({"ok": True, "version": VERSION})
                return
            if path.startswith("/icons/") or path.startswith("/static/"):
                rel = path.split("/", 2)[-1]
                fp = (STATIC / "icons" / Path(rel).name).resolve()
                if not str(fp).startswith(str((STATIC / "icons").resolve())) or not fp.is_file():
                    self._json({"error": "not found"}, 404)
                    return
                ext = fp.suffix.lower()
                ctype = "image/jpeg" if ext in {".jpg", ".jpeg"} else "image/svg+xml" if ext == ".svg" else "image/png"
                self._bytes(fp.read_bytes(), ctype, cache="public, max-age=86400")
                return
            if not app.authorized(self):
                self._reject()
                return
            if path == "/api/settings":
                self._json({"ok": True, "settings": load_settings()})
                return
            if path == "/api/vault":
                if not load_settings().get("secrets", True):
                    self._json({"ok": False, "error": "vault off", "names": []}, 403)
                    return
                self._json({"ok": True, "names": list_secret_names()})
                return
            if path == "/api/memory":
                MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
                if not MEMORY_FILE.exists():
                    MEMORY_FILE.write_text("# Shared memory\n", encoding="utf-8")
                self._json({"ok": True, "text": MEMORY_FILE.read_text(encoding="utf-8")})
                return
            if path == "/api/chat":
                raw_id = (qs.get("id") or [""])[0]
                raw_slug = str((qs.get("slug") or [""])[0] or "").strip()
                win = window_for_slug(raw_slug) if raw_slug else None
                if not win:
                    try:
                        win = find_window(int(raw_id))
                    except Exception:
                        win = None
                if not win:
                    self._json({"ok": False, "error": "window gone", "messages": []}, 404)
                    return
                try:
                    chat = collect_chat(win)
                except Exception as exc:
                    print("collect_chat", raw_id, exc, flush=True)
                    chat = {
                        "messages": [],
                        "session": "",
                        "helpers": 0,
                        "helpers_busy": 0,
                        "queued": 0,
                        "queue": [],
                        "crew": [],
                        "loops": [],
                        "progress": {},
                    }
                slug = slug_for_window(win) or win.get("slug") or ""
                meta = (rosterlib.load_roster().get("agents") or {}).get(slug) or {}
                main = next((c for c in (chat.get("crew") or []) if not c.get("helper")), None)
                busy = live_busy(win) or last_submit_hold(meta)
                if main and main.get("busy"):
                    busy = True
                prog = chat.get("progress") or {}
                if prog.get("waiting"):
                    busy = True
                if (
                    turn_answered(win, slug, chat.get("messages") or [])
                    and not last_submit_hold(meta)
                    and not live_busy(win)
                    and not is_just_stopped(slug)
                ):
                    busy = False
                    prog = {**(prog or {}), "waiting": False, "activity": "Ready"}
                route = meta.get("last_route") if isinstance(meta.get("last_route"), dict) else None
                if route:
                    try:
                        if time.time() - float(route.get("at") or 0) > 1800:
                            route = None
                    except (TypeError, ValueError):
                        route = None
                self._json(
                    {
                        "ok": True,
                        "for_id": win.get("id"),
                        "slug": slug,
                        "label": win.get("title") or "Agent",
                        "session": chat.get("session") or "",
                        "summary": "",
                        "messages": chat.get("messages") or [],
                        "helpers": chat.get("helpers") or 0,
                        "helpers_busy": chat.get("helpers_busy") or 0,
                        "queued": chat.get("queued") or 0,
                        "queue": chat.get("queue") or [],
                        "crew": chat.get("crew") or [],
                        "loops": chat.get("loops") or [],
                        "progress": prog,
                        "busy": busy,
                        "activity": (prog.get("activity") if busy else None) or (main or {}).get("activity") or win.get("activity") or ("Busy" if busy else "Ready"),
                        "busy_since": (main or {}).get("busy_since"),
                        "busy_for": (main or {}).get("busy_for") or 0,
                        "last_submit_at": meta.get("last_submit_at"),
                        "route": route,
                    }
                )
                return
            if path == "/api/agent-memory":
                slug = (qs.get("slug") or [""])[0]
                if not slug:
                    self._json({"ok": False, "error": "no bot"}, 400)
                    return
                self._json({"ok": True, "slug": slug, "text": rosterlib.read_memory(slug)})
                return
            if path == "/api/loop":
                slug = (qs.get("slug") or [""])[0]
                if not slug:
                    self._json({"ok": False, "error": "no bot"}, 400)
                    return
                self._json({"ok": True, "slug": slug, "loops": rosterlib.public_loops(slug)})
                return
            if path in {"/api/browse/health", "/api/browse/shot", "/api/browse/read"}:
                sub = "/" + path.rsplit("/", 1)[-1]
                timeout = 3.0 if sub in {"/health", "/shot"} else 25.0
                bot = (qs.get("bot") or [""])[0]
                q = urlencode({"bot": bot}) if bot else ""
                status, ctype, data = browse_proxy("GET", sub, timeout=timeout, query=q)
                if "json" in ctype:
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self._set_cookie()
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self._bytes(data, ctype)
                return
            if path == "/api/brief":
                self._json({"ok": True, "text": spoken_brief()})
                return
            if path == "/api/tts":
                q = (qs.get("q") or qs.get("text") or [""])[0]
                voice = (qs.get("voice") or [TTS_VOICE])[0]
                try:
                    audio, ctype = tts_mp3(q, voice)
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, 400)
                    return
                self._bytes(audio, ctype, cache="public, max-age=86400")
                return
            if path == "/api/state":
                threading.Thread(
                    target=lambda: (drain_queues(), keep_bots_alive(), ensure_shared_memory(), tick_loops()),
                    daemon=True,
                ).start()
                wins = attach_busy_times(list_windows_cached())
                rost = decorate_roster(rosterlib.public_roster(wins, window_label))
                self._json(
                    {
                        "ok": True,
                        "host": socket.gethostname(),
                        "ip": lan_ip(),
                        "windows": wins,
                        "roster": rost,
                        "apps": installed_apps()[:80],
                        "providers": agents_tmux.providers_ok(),
                        "models": rosterlib.MODELS,
                        "usage": weekly_usage(),
                    }
                )
                return
            if path == "/api/roster":
                wins = list_windows()
                self._json({"ok": True, **decorate_roster(rosterlib.public_roster(wins, window_label))})
                return
            if path in {"/api/frame", "/api/thumb"}:
                raw_id = (qs.get("id") or ["screen"])[0]
                is_thumb = path.endswith("thumb")
                try:
                    wid = None if raw_id in {"screen", "0", "full"} else int(raw_id)
                    jpeg = capture_jpeg(
                        wid,
                        max_w=520 if is_thumb else 1280,
                        quality=0.45 if is_thumb else 0.58,
                    )
                    self._bytes(jpeg, "image/jpeg")
                except Exception as e:
                    self._json({"ok": False, "error": str(e)}, 404)
                return
            self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            if not app.authorized(self):
                self._reject()
                return
            parsed = urlparse(self.path)
            path = parsed.path
            body = self._read_json(json_body_limit(path))
            try:
                if path == "/api/click":
                    win = resolve_target(body)
                    nx = float(body.get("nx", 0.5))
                    ny = float(body.get("ny", 0.5))
                    kind = str(body.get("kind") or "click")
                    x = int(win["x"] + max(0.0, min(1.0, nx)) * win["w"])
                    y = int(win["y"] + max(0.0, min(1.0, ny)) * win["h"])
                    if kind == "right":
                        cliclick(f"rc:{x},{y}")
                    elif kind == "dbl":
                        cliclick(f"dc:{x},{y}")
                    else:
                        cliclick(f"c:{x},{y}")
                    self._json({"ok": True, "x": x, "y": y})
                    return
                if path == "/api/scroll":
                    win = resolve_target(body)
                    nx = float(body.get("nx", 0.5))
                    ny = float(body.get("ny", 0.5))
                    dy = int(body.get("dy") or 0)
                    x = int(win["x"] + max(0.0, min(1.0, nx)) * win["w"])
                    y = int(win["y"] + max(0.0, min(1.0, ny)) * win["h"])
                    ticks = max(-20, min(20, dy))
                    if ticks:
                        cliclick(f"m:{x},{y}", f"w:{ticks}")
                    self._json({"ok": True})
                    return
                if path == "/api/focus":
                    win = find_window(int(body["id"]))
                    if not win:
                        raise RuntimeError("window is gone")
                    focus_window(win)
                    self._json({"ok": True})
                    return
                if path == "/api/close":
                    win = find_window(int(body["id"]))
                    if not win:
                        raise RuntimeError("window is gone")
                    close_window(win)
                    self._json({"ok": True})
                    return
                if path == "/api/new":
                    win = find_window(int(body["id"]))
                    if not win:
                        raise RuntimeError("window is gone")
                    new_window(win)
                    time.sleep(0.15)
                    self._json({"ok": True})
                    return
                if path == "/api/open":
                    if not load_settings().get("new_terminal", True) and str(body.get("app") or "").lower() == "terminal":
                        raise RuntimeError("new terminal is off in settings")
                    name = str(body.get("app") or "").strip()
                    info = open_app(name, ai=str(body.get("ai") or "grok"), model=str(body.get("model") or "")) or {}
                    time.sleep(0.2)
                    self._json(
                        {
                            "ok": True,
                            "app": name,
                            "id": info.get("id"),
                            "slug": info.get("slug"),
                            "tmux": info.get("tmux"),
                        }
                    )
                    return
                if path == "/api/hide-terminals":
                    hide_terminal_windows()
                    self._json({"ok": True})
                    return
                if path == "/api/upload":
                    if body.get("__error") == "too_large":
                        raise RuntimeError("file too large (max 12 MB)")
                    if not body:
                        raise RuntimeError("empty upload")
                    if body.get("id") in (None, "", "screen", "full") and not str(
                        body.get("slug") or ""
                    ).strip():
                        raise RuntimeError("pick a bot first")
                    slug = str(body.get("slug") or slug_for_window(resolve_delivery(body)) or "inbox")
                    name = Path(str(body.get("name") or "bestand")).name
                    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80] or "bestand"
                    raw = base64.b64decode(str(body.get("data") or ""), validate=False)
                    if not raw:
                        raise RuntimeError("empty file")
                    if len(raw) > UPLOAD_FILE_MAX:
                        raise RuntimeError("file too large (max 12 MB)")
                    dest = rosterlib.agent_dir(slug) / "inbox"
                    dest.mkdir(parents=True, exist_ok=True)
                    fp = dest / name
                    fp.write_bytes(raw)
                    win = resolve_delivery(body)
                    try:
                        rosterlib.remember_swarm_msg(
                            slug, "user", name, meta="file", name=name, path=str(fp)
                        )
                    except Exception:
                        pass
                    notice = f"Bestand ontvangen: {fp}\nGebruik dit bestand in je werk."
                    extra = {"via": "file", "queued": False, "helper": False}
                    try:
                        busy = live_busy(win)
                    except Exception:
                        busy = False
                    tmux = str((win or {}).get("tmux") or "")
                    if busy and tmux:
                        threading.Thread(
                            target=_send_when_ready, args=(tmux, notice, 80.0), daemon=True
                        ).start()
                    else:
                        try:
                            deliver_text(win, notice, True)
                            _mark_submit(slug)
                        except Exception as exc:
                            extra["error"] = str(exc)
                    self._json({"ok": True, "path": str(fp), "name": name, **extra})
                    return
                if path == "/api/icon":
                    rosterlib.set_icon(
                        str(body.get("slug") or ""),
                        str(body.get("icon") or ""),
                        str(body.get("color") or ""),
                    )
                    self._json({"ok": True, **decorate_roster(rosterlib.public_roster(list_windows(), window_label))})
                    return
                if path == "/api/reorder":
                    slugs = body.get("slugs") or []
                    if not isinstance(slugs, list):
                        raise RuntimeError("invalid order")
                    rosterlib.set_order([str(s) for s in slugs])
                    self._json({"ok": True, **decorate_roster(rosterlib.public_roster(list_windows(), window_label))})
                    return
                if path == "/api/settings":
                    self._json({"ok": True, "settings": save_settings(body)})
                    return
                if path == "/api/rename":
                    rosterlib.rename_agent(str(body.get("slug") or ""), str(body.get("name") or ""))
                    self._json({"ok": True, **decorate_roster(rosterlib.public_roster(list_windows(), window_label))})
                    return
                if path == "/api/ceo":
                    rosterlib.set_ceo(str(body.get("slug") or ""))
                    self._json({"ok": True, **decorate_roster(rosterlib.public_roster(list_windows(), window_label))})
                    return
                if path == "/api/bot":
                    slug = str(body.get("slug") or "").strip()
                    if not slug:
                        raise RuntimeError("no bot")
                    if str(body.get("op") or "") == "delete" or body.get("delete"):
                        delete_bot(slug)
                        self._json(
                            {
                                "ok": True,
                                "deleted": slug,
                                **decorate_roster(rosterlib.public_roster(list_windows(), window_label)),
                            }
                        )
                        return
                    want_ai = body.get("ai")
                    prev_meta = (rosterlib.load_roster().get("agents") or {}).get(slug) or {}
                    prev_ai = rosterlib.normalize_ai(prev_meta.get("ai"))
                    prev_model = rosterlib.normalize_model(prev_ai, prev_meta.get("model"))
                    want_model = body.get("model")
                    if want_ai is not None:
                        agents_tmux.bin_for(rosterlib.normalize_ai(str(want_ai)))
                    rosterlib.update_bot(
                        slug,
                        name=body.get("name"),
                        icon=body.get("icon"),
                        color=body.get("color"),
                        ceo=bool(body.get("ceo")),
                        home=bool(body.get("home")) if "home" in body else None,
                        ai=want_ai,
                        model=want_model,
                    )
                    if body.get("home"):
                        try:
                            kill_helpers(slug)
                            rosterlib.clear_helpers(slug)
                        except Exception:
                            pass
                    extra = {}
                    next_ai = rosterlib.normalize_ai(str(want_ai) if want_ai is not None else prev_ai)
                    next_model = rosterlib.normalize_model(next_ai, want_model if want_model is not None else prev_model)
                    if next_ai != prev_ai or next_model != prev_model:
                        extra = switch_bot_ai(slug, next_ai, next_model, force=True)
                    elif want_ai is not None:
                        extra = switch_bot_ai(slug, next_ai, next_model)
                    if "text" in body:
                        text = str(body.get("text") or "")
                        if len(text) > 200_000:
                            raise RuntimeError("memory too large")
                        rosterlib.write_memory(slug, text)
                    loop_prompt = str(body.get("loop_prompt") or "").strip()
                    if loop_prompt:
                        raw_iv = str(body.get("interval") or "").strip()
                        if not raw_iv:
                            raw_iv = rosterlib.interval_from_fields(
                                body.get("n") or body.get("loop_n"),
                                body.get("unit") or body.get("loop_unit"),
                            )
                        rosterlib.upsert_loop(
                            slug,
                            str(body.get("loop_name") or ""),
                            raw_iv,
                            loop_prompt,
                            source="swarm",
                        )
                    self._json(
                        {
                            "ok": True,
                            "loops": rosterlib.public_loops(slug),
                            **decorate_roster(rosterlib.public_roster(list_windows(), window_label)),
                            **extra,
                        }
                    )
                    return
                if path == "/api/ai":
                    slug = str(body.get("slug") or "").strip()
                    extra = switch_bot_ai(slug, str(body.get("ai") or "grok"))
                    self._json(
                        {
                            "ok": True,
                            **decorate_roster(rosterlib.public_roster(list_windows(), window_label)),
                            **extra,
                        }
                    )
                    return
                if path == "/api/loop":
                    slug = str(body.get("slug") or "").strip()
                    if not slug:
                        raise RuntimeError("no bot")
                    op = str(body.get("op") or "add")
                    if op == "del":
                        rosterlib.remove_loop(slug, str(body.get("id") or ""))
                    else:
                        raw_iv = str(body.get("interval") or "").strip()
                        if not raw_iv:
                            raw_iv = rosterlib.interval_from_fields(
                                body.get("n") or body.get("loop_n") or 30,
                                body.get("unit") or body.get("loop_unit") or "m",
                            )
                        rosterlib.upsert_loop(
                            slug,
                            str(body.get("name") or ""),
                            raw_iv,
                            str(body.get("prompt") or ""),
                            source="swarm",
                        )
                    self._json(
                        {
                            "ok": True,
                            "loops": rosterlib.public_loops(slug),
                            **decorate_roster(rosterlib.public_roster(list_windows(), window_label)),
                        }
                    )
                    return
                if path == "/api/assign":
                    slug = str(body.get("slug") or "")
                    title = str(body.get("title") or "").strip()
                    if not title:
                        raise RuntimeError("no task")
                    item = rosterlib.assign_task(slug, title, str(body.get("from") or "CEO"))
                    try:
                        win = resolve_delivery({"slug": slug})
                        deliver_text(
                            win,
                            f"[Task from CEO]: {title}\n\nHandle this autonomously. "
                            f"Update ~/.grok/imac-phone/agents/{slug}/memory.md. "
                            f"Mark the task done in tasks.json when you finish.",
                            True,
                        )
                    except Exception:
                        pass
                    self._json({"ok": True, "task": item})
                    return
                if path == "/api/vault":
                    if not load_settings().get("secrets", True):
                        raise RuntimeError("vault is off in settings")
                    add_secret(str(body.get("name") or ""), str(body.get("value") or ""))
                    self._json({"ok": True, "names": list_secret_names()})
                    return
                if path == "/api/memory":
                    text = str(body.get("text") or "")
                    if len(text) > 200_000:
                        raise RuntimeError("memory too large")
                    MEMORY_FILE.write_text(text, encoding="utf-8")
                    self._json({"ok": True})
                    return
                if path == "/api/agent-memory":
                    slug = str(body.get("slug") or "").strip()
                    if not slug:
                        raise RuntimeError("no bot")
                    text = str(body.get("text") or "")
                    if len(text) > 200_000:
                        raise RuntimeError("memory too large")
                    rosterlib.write_memory(slug, text)
                    self._json({"ok": True})
                    return
                if path.startswith("/api/browse/"):
                    sub = path[len("/api/browse") :]
                    qs = parse_qs(parsed.query)
                    bot = str(body.get("bot") or (qs.get("bot") or [""])[0] or "")
                    if bot and "bot" not in body:
                        body = dict(body or {})
                        body["bot"] = bot
                    q = urlencode({"bot": bot}) if bot else ""
                    status, ctype, data = browse_proxy("POST", sub, body, query=q)
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self._set_cookie()
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if path == "/api/tts":
                    text = str(body.get("text") or body.get("q") or "")
                    voice = str(body.get("voice") or TTS_VOICE)
                    audio, ctype = tts_mp3(text, voice)
                    self._bytes(audio, ctype, cache="public, max-age=86400")
                    return
                if path == "/api/stt":
                    raw = base64.b64decode(str(body.get("audio_b64") or ""), validate=False)
                    name = str(body.get("name") or "talk.m4a")
                    text = stt_bytes(raw, name)
                    self._json({"ok": True, "text": text})
                    return
                if path == "/api/stop":
                    if body.get("id") in (None, "", "screen", "full") and not body.get("slug") and not body.get("tmux"):
                        raise RuntimeError("pick a bot first")
                    win = resolve_delivery(body)
                    self._json(interrupt_chat(win))
                    return
                if path == "/api/type":
                    text = str(body.get("text") or "")
                    if len(text) > 4000:
                        raise RuntimeError("text too long")
                    if body.get("id") in (None, "", "screen", "full"):
                        raise RuntimeError("pick a bot first")
                    win = resolve_delivery(body)
                    slug = slug_for_window(win) or str(win.get("slug") or body.get("slug") or "")
                    if str(body.get("op") or "") in {"starthelper", "spawnhelper"}:
                        if not slug:
                            raise RuntimeError("no bot")
                        extra = start_helper(win, slug, text)
                        hid = str(extra.get("slug") or extra.get("id") or extra.get("tmux") or "")
                        route = rosterlib.set_last_route(
                            slug,
                            {
                                "choice": "helper",
                                "text": text,
                                "at": time.time(),
                                "chosen_by": "user",
                                "hid": hid,
                            },
                        )
                        self._json(
                            {
                                "ok": True,
                                "action": "helper",
                                "choice": "helper",
                                "chosen_by": "user",
                                "helper": True,
                                "item": extra,
                                "queue": rosterlib.public_queue(slug),
                                "queued_n": len(rosterlib.load_queue(slug)),
                                "crew": crew_for_slug(slug),
                                "route": route,
                                **{k: extra.get(k) for k in ("tmux", "slug") if extra.get(k)},
                            }
                        )
                        return
                    if str(body.get("op") or "") == "reassign":
                        if not slug:
                            raise RuntimeError("no bot")
                        extra = reassign_helper(
                            win,
                            slug,
                            str(body.get("hid") or body.get("qid") or ""),
                            str(body.get("action") or ""),
                        )
                        extra["queue"] = rosterlib.public_queue(slug)
                        extra["queued_n"] = len(rosterlib.load_queue(slug))
                        extra["crew"] = crew_for_slug(slug)
                        act = str(body.get("action") or extra.get("action") or "")
                        extra["choice"] = "queue" if act == "queue" else ("steer" if act == "steer" else act)
                        extra["chosen_by"] = "user"
                        extra["route"] = rosterlib.set_last_route(
                            slug,
                            {
                                "choice": extra["choice"] or "steer",
                                "text": extra.get("text") or text,
                                "at": time.time(),
                                "chosen_by": "user",
                            },
                        )
                        self._json(extra)
                        return
                    if body.get("queue") or str(body.get("op") or "") == "queue":
                        if not slug:
                            raise RuntimeError("no bot")
                        item = rosterlib.enqueue(
                            slug,
                            text,
                            hold=bool(body.get("hold")),
                            choice="queue",
                            chosen_by="user",
                        )
                        route = rosterlib.set_last_route(
                            slug,
                            {
                                "choice": "queue",
                                "text": text,
                                "at": time.time(),
                                "chosen_by": "user",
                                "qid": item.get("id") or "",
                            },
                        )
                        self._json(
                            {
                                "ok": True,
                                "queued": True,
                                "choice": "queue",
                                "chosen_by": "user",
                                "item": item,
                                "queue": rosterlib.public_queue(slug),
                                "queued_n": len(rosterlib.load_queue(slug)),
                                "route": route,
                            }
                        )
                        return
                    if str(body.get("op") or "") == "drophelper":
                        if not slug:
                            raise RuntimeError("no bot")
                        hid = str(body.get("hid") or body.get("qid") or "")
                        gone = rosterlib.remove_helper(slug, hid)
                        if gone and gone.get("tmux"):
                            try:
                                agents_tmux.kill(str(gone["tmux"]))
                            except Exception:
                                pass
                        self._json(
                            {
                                "ok": True,
                                "dropped": bool(gone),
                                "crew": crew_for_slug(slug),
                                "queue": rosterlib.public_queue(slug),
                            }
                        )
                        return
                    if str(body.get("op") or "") == "dequeue":
                        if not slug:
                            raise RuntimeError("no bot")
                        rosterlib.remove_queue(slug, str(body.get("qid") or ""))
                        self._json(
                            {
                                "ok": True,
                                "queue": rosterlib.public_queue(slug),
                                "queued_n": len(rosterlib.load_queue(slug)),
                            }
                        )
                        return
                    if str(body.get("op") or "") == "assign":
                        if not slug:
                            raise RuntimeError("no bot")
                        action = str(body.get("action") or "queue")
                        qid = str(body.get("qid") or "")
                        item = rosterlib.assign_queue(slug, qid, action)
                        extra = {}
                        if item and action in {"helper", "agent"}:
                            extra = start_helper(win, slug, str(item.get("text") or ""))
                        elif item and action == "steer":
                            extra = steer_into_chat(win, slug, str(item.get("text") or ""))
                        kind = "helper" if action in {"helper", "agent"} else action
                        route = rosterlib.set_last_route(
                            slug,
                            {
                                "choice": kind,
                                "text": (item or {}).get("text") or extra.get("text") or "",
                                "at": time.time(),
                                "chosen_by": "user",
                                "qid": qid if kind == "queue" else "",
                                "hid": extra.get("slug") or extra.get("id") or "",
                            },
                        )
                        self._json(
                            {
                                "ok": True,
                                "action": action,
                                "choice": kind,
                                "chosen_by": "user",
                                "item": item,
                                "text": extra.get("text") or ((item or {}).get("text") or ""),
                                "helper": bool(extra.get("tmux") or extra.get("slug")),
                                "queue": rosterlib.public_queue(slug),
                                "queued_n": len(rosterlib.load_queue(slug)),
                                "crew": crew_for_slug(slug),
                                "route": route,
                            }
                        )
                        return
                    submit = bool(body.get("submit"))
                    extra = dispatch_text(win, text, submit, hinted_busy=bool(body.get("busy")))
                    extra["queue"] = rosterlib.public_queue(slug) if slug else []
                    extra["queued_n"] = len(rosterlib.load_queue(slug)) if slug else 0
                    self._json(extra)
                    return
                if path == "/api/key":
                    if body.get("id") in (None, "", "screen", "full"):
                        raise RuntimeError("pick a bot first")
                    win = resolve_delivery(body)
                    if win.get("tmux"):
                        agents_tmux.send_text(win["tmux"], "", enter=True)
                    else:
                        deliver_text(win, "", True)
                    self._json({"ok": True})
                    return
                self._json({"error": "not found"}, 404)
            except Exception as e:
                print(f"! {path} {e}", flush=True)
                self._json({"ok": False, "error": str(e)}, 400)

    return Handler


def publish_phone_page(primary: str, lan: str) -> None:
    """Keep a stable iPhone entry: two buttons, current 5G + wifi links."""
    try:
        payload = json.dumps({"ok": True, "lan": lan, "pub": primary}, ensure_ascii=False)
        html = (STATIC / "go.html").read_text(encoding="utf-8")
        local_json = STATE_DIR / "heavy.json"
        local_html = STATE_DIR / "heavy.html"
        local_json.write_text(payload + "\n", encoding="utf-8")
        local_html.write_text(html, encoding="utf-8")
        key = Path.home() / ".ssh" / "codex_hetzner_degero"
        if not key.is_file():
            return
        subprocess.run(
            [
                "scp",
                "-i",
                str(key),
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ConnectTimeout=8",
                str(local_html),
                str(local_json),
                "root@204.168.149.212:/var/www/bumblly/",
            ],
            capture_output=True,
            timeout=25,
        )
        icon = STATIC / "icons" / "apple-touch-icon.png"
        app_png = STATIC / "icons" / "app.png"
        if icon.is_file():
            subprocess.run(
                [
                    "ssh",
                    "-i",
                    str(key),
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "ConnectTimeout=8",
                    "root@204.168.149.212",
                    "mkdir -p /var/www/bumblly/icons /var/www/bumblly/s /var/www/bumblly/s/icons",
                ],
                capture_output=True,
                timeout=15,
            )
            subprocess.run(
                [
                    "scp",
                    "-i",
                    str(key),
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "ConnectTimeout=8",
                    str(icon),
                    str(app_png),
                    "root@204.168.149.212:/var/www/bumblly/icons/",
                ],
                capture_output=True,
                timeout=25,
            )
            subprocess.run(
                [
                    "scp",
                    "-i",
                    str(key),
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "ConnectTimeout=8",
                    str(icon),
                    "root@204.168.149.212:/var/www/bumblly/apple-touch-icon.png",
                ],
                capture_output=True,
                timeout=20,
            )
            subprocess.run(
                [
                    "scp",
                    "-i",
                    str(key),
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "ConnectTimeout=8",
                    str(icon),
                    str(app_png),
                    "root@204.168.149.212:/var/www/bumblly/s/icons/",
                ],
                capture_output=True,
                timeout=25,
            )
            subprocess.run(
                [
                    "scp",
                    "-i",
                    str(key),
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "ConnectTimeout=8",
                    str(icon),
                    "root@204.168.149.212:/var/www/bumblly/s/apple-touch-icon.png",
                ],
                capture_output=True,
                timeout=20,
            )
    except Exception:
        pass


def ensure_browse_daemon() -> None:
    """Start the per-bot Chrome helper if Playwright is installed."""
    try:
        urllib.request.urlopen("http://127.0.0.1:8791/health", timeout=0.5).read()
        return
    except Exception:
        pass
    try:
        import playwright  # noqa: F401
    except Exception:
        print("→ browser off (pip install playwright && playwright install chromium)", flush=True)
        return
    try:
        subprocess.Popen(
            [sys.executable, str(ROOT / "browse_daemon.py")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print("→ per-bot browser on :8791", flush=True)
    except Exception as exc:
        print("browse daemon", exc, flush=True)


def write_urls(primary: str, lan: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    URL_FILE.write_text(
        f"{primary}\n# lan: {lan}\n# updated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
        encoding="utf-8",
    )
    desktop = Path.home() / "Desktop" / "imac-phone-url.txt"
    try:
        desktop.write_text(primary + "\n", encoding="utf-8")
    except Exception:
        pass
    threading.Thread(target=publish_phone_page, args=(primary, lan), daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Control iMac windows from your iPhone")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--local-only", action="store_true")
    args = parser.parse_args()

    port = pick_port(args.port)
    token = load_or_create_token()
    app = App(port=port, token=token, public=not args.local_only)

    def _stop(signum=None, frame=None):
        print("\n→ stop")
        app.cleanup()
        os._exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    handler = make_handler(app)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=_refresh_windows_bg, daemon=True).start()

    def _adopt_soon():
        time.sleep(1.2)
        global _last_keep
        _last_keep = 0
        try:
            rosterlib.ensure_team_roles()
        except Exception:
            pass
        try:
            keep_bots_alive()
        except Exception:
            pass

    threading.Thread(target=_adopt_soon, daemon=True).start()
    threading.Thread(target=ensure_browse_daemon, daemon=True).start()

    # Keep display awake while this remote is up
    caff = subprocess.Popen(["caffeinate", "-dims"])

    ip = lan_ip()
    lan = f"http://{ip}:{port}/?k={token}"
    pub = public_base()
    primary = f"{pub}/?k={token}" if pub else lan
    print(f"→ Swarm v{VERSION}")
    print(f"→ UI on 0.0.0.0:{port}")
    write_urls(primary, lan)
    print()
    print("=" * 56)
    print("  Swarm  ·  Mac → iPhone")
    print("=" * 56)
    print(f"  Phone (same Wi-Fi):  {lan}")
    if pub:
        print(f"  Public / 5G:         {primary}")
    else:
        print("  Public / 5G:         set ~/.grok/imac-phone/public-url.txt")
    print("=" * 56)
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(primary)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        pass

    try:
        while not app._stop.is_set():
            time.sleep(1)
            if app.public and app.tunnel_proc and app.tunnel_proc.poll() is not None:
                print("! tunnel gestopt — wifi-link blijft werken")
                app.tunnel_proc = None
    finally:
        app.cleanup()
        caff.terminate()
        httpd.shutdown()


if __name__ == "__main__":
    main()

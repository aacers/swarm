"""Which bot owns a gbrowse call — no Playwright import."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

DEFAULT_BOT = "shared"
_SLUG_RE = re.compile(r"[^a-z0-9._-]+")
_WORK_RE = re.compile(r"/workspaces/([A-Za-z0-9._-]+)")


def normalize_bot(raw: str | None) -> str:
    s = _SLUG_RE.sub("-", (raw or "").strip().lower()).strip("-")
    if s.startswith("heavy-"):
        s = s[6:]
    if s.startswith("h--"):
        s = s[3:]
    return s or DEFAULT_BOT


def tmux_session_name() -> str:
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True,
            text=True,
            timeout=1,
        )
        return (r.stdout or "").strip()
    except Exception:
        return ""


def detect_bot(
    explicit: str | None = None,
    env: dict | None = None,
    cwd: str | None = None,
    tmux_name: str | None = None,
) -> str:
    if explicit:
        return normalize_bot(explicit)
    env = env if env is not None else os.environ
    for key in ("SWARM_SLUG", "GROK_BOT", "SWARM_BOT"):
        if env.get(key):
            return normalize_bot(env[key])
    name = tmux_name if tmux_name is not None else tmux_session_name()
    if name:
        return normalize_bot(name)
    here = cwd if cwd is not None else os.getcwd()
    hit = _WORK_RE.search(here.replace("\\", "/"))
    if hit:
        return normalize_bot(hit.group(1))
    return DEFAULT_BOT


def profile_dir(bot: str) -> Path:
    bot = normalize_bot(bot)
    root = Path.home() / ".grok" / "browser"
    if bot == DEFAULT_BOT:
        return root / "profile"
    return root / "profiles" / bot

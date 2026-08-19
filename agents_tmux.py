"""Hidden Grok agents — tmux, no Terminal.app windows."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

TMUX = shutil.which("tmux") or "/opt/homebrew/bin/tmux"
GROK = (
    shutil.which("grok")
    or str(Path.home() / ".local" / "bin" / "grok")
    or str(Path.home() / ".grok" / "bin" / "grok")
)
CLAUDE = shutil.which("claude") or "/opt/homebrew/bin/claude"
CODEX = shutil.which("codex") or str(Path.home() / ".local" / "bin" / "codex")
PREFIX = "heavy-"
HELPER_PREFIX = "h--"
WORK_ROOT = Path.home() / ".grok" / "imac-phone" / "workspaces"
AIS = ("grok", "claude", "codex")
# Other terminal AIs we probe. Anything else in PATH can still be a bot.
EXTRA_AIS = ("gemini", "aider", "opencode", "cursor-agent", "amp", "crush")
# Live status only. Do not match cwd paths like thinking-remote-… or old tool names.
LIVE_BUSY_RE = re.compile(
    r"(?:^|[\s—–-])(?:Thinking|Preparing)(?:…|\.\.\.|:|\s|$)|"
    r"Waiting for response|"
    r"esc to interrupt|Esc to interrupt|Esc:cancel|"
    r"ctrl\+c to stop|\[stop\]|"
    r"Working…|Working\.\.\.|Crafting|"
    r"Running…|Running\.\.\.|"
    r"command still running|"
    r"queued — Enter to send",
    re.I,
)
REWIND_RE = re.compile(r"Rewind to which turn\?", re.I)
BUSY_RE = LIVE_BUSY_RE
SPINNER_RE = re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]")


def normalize_ai(ai: str | None) -> str:
    a = re.sub(r"[^a-z0-9_-]", "", (ai or "grok").strip().lower())[:32]
    if a in AIS or a in EXTRA_AIS:
        return a
    if a and shutil.which(a):
        return a
    return "grok"


_LOGIN_HINT = re.compile(
    r"sign in|log in|logged out|not logged|device.?code|enter (the )?code|"
    r"visit |open .*browser|authenticate|oauth|verification code",
    re.I,
)
_LOGIN_CODE = re.compile(r"\b([A-Z0-9]{4,5}-[A-Z0-9]{4,5})\b")


_logged_cache: dict[str, tuple[float, bool]] = {}


def logged_in(ai: str) -> bool:
    """True if this CLI already has credentials on this Mac."""
    ai = normalize_ai(ai)
    now = time.time()
    hit = _logged_cache.get(ai)
    if hit and now - hit[0] < 8:
        return hit[1]
    ok = False
    try:
        exe = bin_for(ai)
        if ai == "grok":
            p = Path.home() / ".grok" / "auth.json"
            data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
            ok = isinstance(data, dict) and any(
                isinstance(v, dict) and v.get("key") for v in data.values()
            )
        elif ai == "claude":
            r = subprocess.run([exe, "auth", "status"], capture_output=True, timeout=8)
            t = (r.stdout or b"").decode("utf-8", "replace")
            compact = t.replace(" ", "")
            if "loggedIn" in t:
                ok = '"loggedIn":true' in compact
            else:
                ok = r.returncode == 0 and "not logged" not in t.lower()
        elif ai == "codex":
            r = subprocess.run([exe, "login", "status"], capture_output=True, timeout=8)
            t = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace").lower()
            ok = "logged in" in t and "not logged" not in t and "logged out" not in t
        else:
            ok = True
    except Exception:
        ok = False
    _logged_cache[ai] = (now, ok)
    return ok


def login_launch_cmd(ai: str, work: Path) -> str:
    ai = normalize_ai(ai)
    exe = bin_for(ai)
    path = 'export PATH="$HOME/.grok/bin:$HOME/.local/bin:/opt/homebrew/bin:$PATH"; '
    work_q = shlex.quote(str(work))
    exe_q = shlex.quote(exe)
    if ai == "grok":
        args = "login --device-auth"
    elif ai == "claude":
        args = "auth login"
    elif ai == "codex":
        args = "login --device-auth"
    else:
        args = "login"
    return f"{path}cd {work_q} && exec {exe_q} {args}"


def parse_login_pane(pane: str) -> dict:
    t = pane or ""
    url = ""
    for u in re.findall(r"https://[^\s>'\"\]|]+", t):
        u = u.rstrip(".,);")
        low = u.lower()
        if any(x in low for x in ("auth", "login", "device", "oauth", "claude.ai", "openai.com", "x.ai", "google.com")):
            url = u
            break
        if not url:
            url = u
    code = ""
    m = _LOGIN_CODE.search(t)
    if m:
        code = m.group(1)
    needed = bool(url or code or _LOGIN_HINT.search(t))
    return {"needed": needed, "url": url, "code": code}


def bin_for(ai: str) -> str:
    ai = normalize_ai(ai)
    special = {"grok": GROK, "claude": CLAUDE, "codex": CODEX}.get(ai)
    path = special if special and Path(special).exists() else shutil.which(ai)
    if not path or not Path(path).exists():
        raise RuntimeError(f"{ai} is not on this Mac")
    return path


def providers_ok() -> dict:
    out = {}
    for name in AIS + EXTRA_AIS:
        try:
            out[name] = bool(bin_for(name))
        except Exception:
            out[name] = False
    return out


CODEX_CONFIG = Path.home() / ".codex" / "config.toml"


def ensure_codex_trust(work: Path) -> None:
    """Skip the first-run 'trust this directory?' gate for Swarm workspaces."""
    try:
        path = str(Path(work).expanduser().resolve())
    except Exception:
        return
    if not path:
        return
    header = f'[projects."{path}"]'
    try:
        text = CODEX_CONFIG.read_text(encoding="utf-8") if CODEX_CONFIG.is_file() else ""
    except Exception:
        text = ""
    if header in text:
        return
    try:
        CODEX_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        with CODEX_CONFIG.open("a", encoding="utf-8") as fh:
            fh.write(f"\n{header}\ntrust_level = \"trusted\"\n")
    except Exception:
        pass


def dismiss_codex_trust(name: str) -> None:
    pane = capture_pane(name, 30)
    if re.search(r"Do you trust the contents of this directory", pane or "", re.I):
        send_keys(name, "C-m")


def launch_cmd(ai: str, work: Path, sid: str, model: str = "", resume: bool = False) -> str:
    ai = normalize_ai(ai)
    exe = bin_for(ai)
    path = 'export PATH="$HOME/.grok/bin:$HOME/.local/bin:/opt/homebrew/bin:$PATH"; '
    work_q = shlex.quote(str(work))
    exe_q = shlex.quote(exe)
    model = (model or "").strip()
    sid_q = shlex.quote(str(sid))
    if ai == "claude":
        m = model or "sonnet"
        return (
            f"{path}cd {work_q} && exec {exe_q} --session-id {sid_q} "
            f"--permission-mode bypassPermissions --model {shlex.quote(m)}"
        )
    if ai == "codex":
        mflag = f" -m {shlex.quote(model)}" if model else ""
        return (
            f"{path}cd {work_q} && exec {exe_q}{mflag} -C {work_q} "
            f"--ask-for-approval never --sandbox danger-full-access"
        )
    if ai not in AIS:
        extra = f" {shlex.quote(model)}" if model else ""
        return f"{path}cd {work_q} && exec {exe_q}{extra}"
    mflag = f" -m {shlex.quote(model)}" if model else ""
    if resume:
        return f"{path}exec {exe_q}{mflag} --resume {sid_q} --always-approve --cwd {work_q}"
    return f"{path}exec {exe_q}{mflag} --session-id {sid_q} --always-approve --cwd {work_q}"


ACTIVITY_ALIAS = {
    "Bezig": "Busy",
    "Klaar": "Ready",
    "Denkt": "Thinking",
    "Denkt na": "Thinking",
    "Schrijft": "Writing",
    "Leest": "Reading",
    "Zoekt": "Searching",
    "Zoekt web": "Searching web",
    "Leest web": "Reading web",
    "Plant": "Planning",
    "Beeld": "Image",
    "Wacht": "Waiting",
    "Busy": "Busy",
    "Ready": "Ready",
    "Thinking": "Thinking",
    "Writing": "Writing",
    "Reading": "Reading",
    "Searching": "Searching",
    "Searching web": "Searching web",
    "Reading web": "Reading web",
    "Planning": "Planning",
    "Image": "Image",
    "Waiting": "Waiting",
    "Browser": "Browser",
    "Terminal": "Terminal",
    "Agent": "Agent",
    "Tool": "Tool",
}
KNOWN_ACTIVITY = {
    "Busy",
    "Ready",
    "Thinking",
    "Writing",
    "Reading",
    "Searching",
    "Searching web",
    "Reading web",
    "Browser",
    "Terminal",
    "Planning",
    "Image",
    "Waiting",
    "Agent",
    "Tool",
}


def clean_activity(s: str | None, busy: bool) -> str:
    t = ACTIVITY_ALIAS.get((s or "").strip(), (s or "").strip())
    if t in KNOWN_ACTIVITY:
        return t
    return "Busy" if busy else "Ready"


def title_busy(title: str) -> bool:
    t = title or ""
    return bool(SPINNER_RE.search(t) or LIVE_BUSY_RE.search(t))


def pane_overlay(pane: str) -> str:
    t = pane or ""
    if REWIND_RE.search(t):
        return "rewind"
    return ""


_GUI_LOOP_RE = re.compile(
    r"(?i)gbrowse|browse_daemon|desktop-harness|shadow[- ]dom|"
    r"\bclick\b|\bchrome\b|\bsafari\b|accounts\.google|"
    r"search console|screenshot|window bounds|activate chrome|"
    r"property selecteren|\bvolgende\b"
)
_PANE_NOISE_RE = re.compile(
    r"(?:"
    r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]|"
    r"\b\d+[smh](?:\d+[smh])?\b|"
    r"⇣\s*[\d.]+[kKmM]?|"
    r"\[\s*(?:stop|↓|↑|\+)\s*\]"
    r")",
    re.I,
)


_IDLE_BOX_RE = re.compile(
    r"│\s*❯\s*│|╰[^╮]*always-approve|Grok [^\n]*always-approve",
    re.I,
)
# Grok 4.6 keeps the composer visible while a turn runs. Footer then says
# Esc:cancel / Enter:send now / [stop] — that is work, not idle.
_LIVE_WORK_RE = re.compile(
    r"Esc:cancel|Enter:send now|ctrl\+b:send to bg|"
    r"esc to interrupt|Esc to interrupt|Esc:cancel|"
    r"queued — Enter to send",
    re.I,
)


def pane_working(pane: str) -> bool:
    """True when the TUI is in a cancelable turn (composer may still be visible)."""
    tail = "\n".join((pane or "").splitlines()[-16:])
    if not tail.strip():
        return False
    if _LIVE_WORK_RE.search(tail):
        return True
    if SPINNER_RE.search(tail) and re.search(r"\[stop\]", tail, re.I):
        return True
    return False


def pane_has_idle_prompt(pane: str) -> bool:
    """True when the live Grok composer is empty and waiting for a new question."""
    if pane_working(pane):
        return False
    tail = "\n".join((pane or "").splitlines()[-10:])
    if not tail.strip() or not _IDLE_BOX_RE.search(tail):
        return False
    live = "\n".join((pane or "").splitlines()[-6:])
    if SPINNER_RE.search(live) and re.search(r"Waiting for response|esc to interrupt", live, re.I):
        return False
    if re.search(r"command still running|queued — Enter to send", live, re.I):
        return False
    return True


def pane_busy(pane: str) -> bool:
    t = pane or ""
    if not t:
        return False
    if pane_overlay(t):
        return False
    if pane_working(t):
        return True
    if pane_has_idle_prompt(t):
        return False
    tail = "\n".join(t.splitlines()[-12:])
    if SPINNER_RE.search(tail):
        return True
    if re.search(r"\bloop still running\b", tail, re.I):
        tail = re.sub(r"\b\d+\s+loop still running\b", "", tail, flags=re.I)
    return bool(LIVE_BUSY_RE.search(tail))


def pane_fingerprint(pane: str) -> str:
    """Stable pane text: ignore spinners, elapsed clocks and token counters."""
    t = _PANE_NOISE_RE.sub("", pane or "")
    t = re.sub(r"(?i)Waiting for response[^\n]*", "Waiting for response", t)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in t.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def pane_gui_loop(pane: str) -> bool:
    tail = "\n".join((pane or "").splitlines()[-22:])
    return len(_GUI_LOOP_RE.findall(tail)) >= 3


def activity_from_text(blob: str, busy: bool) -> str:
    t = blob or ""
    tail = "\n".join(t.splitlines()[-12:]) if t else ""
    live = bool(busy or SPINNER_RE.search(t) or LIVE_BUSY_RE.search(tail))
    if not live:
        return "Ready"
    if re.search(r"web_search|Searching", tail, re.I):
        return "Searching web"
    if re.search(r"web_fetch|open_page", tail, re.I):
        return "Reading web"
    if re.search(r"search_replace|\bwrite\b", tail, re.I):
        return "Writing"
    if re.search(r"read_file|list_dir", tail, re.I):
        return "Reading"
    if re.search(r"\bgrep\b", tail, re.I):
        return "Searching"
    if re.search(r"gbrowse|browse_daemon", tail, re.I):
        return "Browser"
    if re.search(r"run_terminal_command", tail, re.I):
        return "Terminal"
    if re.search(r"todo_write", tail, re.I):
        return "Planning"
    if re.search(r"image_gen|image_edit", tail, re.I):
        return "Image"
    if re.search(r"Thinking|reasoning", tail, re.I):
        return "Thinking"
    return "Busy"


def cmd_ai(cmd: str) -> str:
    """AI from an argv/command string. Only the executable name, never prompt text."""
    for tok in (cmd or "").split():
        base = Path(tok.strip("\"'")).name.lower()
        if base in AIS or base in EXTRA_AIS:
            return base
    return ""


def sniff_ai(pane: str) -> str:
    if re.search(r"\bClaude\b", pane or ""):
        return "claude"
    if re.search(r"\bCodex\b", pane or ""):
        return "codex"
    return "grok"


def running_ai(name: str) -> str:
    """Which CLI is actually in this tmux pane (process tree, not chat text)."""
    pid = pane_pid(name)
    if not pid:
        return ""
    seen: set[int] = set()
    queue = [pid]
    while queue:
        p = queue.pop(0)
        if p in seen or len(seen) > 24:
            continue
        seen.add(p)
        hit = cmd_ai(_cmd_of(p))
        if hit:
            return hit
        queue.extend(_children(p))
    return ""


def slug_of_session(name: str) -> str:
    return name[len(PREFIX) :] if name.startswith(PREFIX) else name


def is_helper_name(name: str) -> bool:
    return slug_of_session(name or "").startswith(HELPER_PREFIX)


def _run(args: list[str], input_bytes: bytes | None = None, timeout: float = 2) -> subprocess.CompletedProcess:
    return subprocess.run(
        [TMUX, *args],
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
    )


def pane_title(name: str) -> str:
    """Live Grok status line of this tmux pane (not the synthetic Swarm title)."""
    if not name:
        return ""
    r = _run(["display-message", "-p", "-t", name, "#{pane_title}"])
    if r.returncode != 0:
        return ""
    return (r.stdout or b"").decode("utf-8", "replace").strip()


def session_id(name: str) -> int:
    h = hashlib.sha1(name.encode()).hexdigest()
    return 900000 + (int(h[:6], 16) % 90000)


_list_cache: tuple[float, list[dict]] = (0.0, [])
_list_lock = threading.Lock()


def _filter_sessions(rows: list[dict], include_helpers: bool) -> list[dict]:
    if include_helpers:
        return list(rows)
    return [s for s in rows if not is_helper_name(s.get("tmux") or "")]


def list_sessions(include_helpers: bool = False) -> list[dict]:
    global _list_cache
    now = time.time()
    if now - _list_cache[0] < 0.8 and _list_cache[1]:
        return _filter_sessions(_list_cache[1], include_helpers)
    if not _list_lock.acquire(blocking=False):
        if _list_cache[1]:
            return _filter_sessions(_list_cache[1], include_helpers)
        _list_lock.acquire()
    try:
        now = time.time()
        if now - _list_cache[0] < 0.8 and _list_cache[1]:
            return _filter_sessions(_list_cache[1], include_helpers)
        r = _run(["ls", "-F", "#{session_name}"])
        if r.returncode != 0:
            return _filter_sessions(_list_cache[1], include_helpers) if _list_cache[1] else []
        out = []
        for line in r.stdout.decode().splitlines():
            name = line.strip()
            if not name.startswith(PREFIX):
                continue
            slug = slug_of_session(name)
            pane = capture_pane(name, 40)
            busy = pane_busy(pane)
            ai = running_ai(name) or sniff_ai(pane)
            act = activity_from_text(pane, busy)
            real = pane_title(name)
            title = real or f"{slug} — {ai}"
            if busy and not title_busy(title):
                title = f"{act} — {title}" if act and act not in {"Busy", "Ready"} else ("Waiting for response… — " + title)
            out.append(
                {
                    "id": session_id(name),
                    "app": "Terminal",
                    "title": title,
                    "tmux": name,
                    "slug": slug,
                    "busy": busy,
                    "activity": act,
                    "ai": ai,
                    "x": 0,
                    "y": 0,
                    "w": 800,
                    "h": 600,
                    "pid": 0,
                    "minimized": False,
                    "hidden": True,
                }
            )
        _list_cache = (time.time(), out)
        return _filter_sessions(out, include_helpers)
    finally:
        _list_lock.release()


def spawn(label: str | None = None, cwd: str | None = None, ai: str = "grok", model: str = "", sid: str | None = None, resume: bool = False, login: bool = False) -> dict:
    ai = normalize_ai(ai)
    slug = (label or f"bot-{int(time.time()) % 100000}").lower()
    slug = "".join(ch if ch.isalnum() else "-" for ch in slug).strip("-")[:40] or "bot"
    name = PREFIX + slug
    existing = {s["tmux"] for s in list_sessions(include_helpers=True)}
    base = name
    n = 2
    while name in existing:
        name = f"{base}-{n}"
        n += 1
        slug = slug_of_session(name)
    sid = str(sid or uuid.uuid4())
    work = Path(cwd).expanduser() if cwd else (WORK_ROOT / slug)
    work.mkdir(parents=True, exist_ok=True)
    shared = Path.home() / ".grok" / "imac-phone" / "shared-memory.md"
    dest = work / "SHARED.md"
    try:
        if not dest.exists() and not dest.is_symlink() and shared.exists():
            dest.symlink_to(shared)
    except Exception:
        pass
    if ai == "codex" and not login:
        ensure_codex_trust(work)
    cmd = login_launch_cmd(ai, work) if login else launch_cmd(ai, work, sid, model=model, resume=resume)
    r = _run(["new-session", "-d", "-s", name, "-c", str(work), cmd])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).decode() or "tmux spawn failed")
    if ai == "codex" and not login:
        time.sleep(0.5)
        dismiss_codex_trust(name)
    return {
        "id": session_id(name),
        "tmux": name,
        "slug": slug,
        "title": f"{slug} — {ai}",
        "app": "Terminal",
        "hidden": True,
        "session_id": sid,
        "cwd": str(work),
        "ai": ai,
        "model": model or "",
    }


def respawn(name: str, ai: str = "grok", cwd: str | None = None, sid: str | None = None, model: str = "", login: bool = False) -> dict:
    ai = normalize_ai(ai)
    slug = slug_of_session(name)
    work = Path(cwd).expanduser() if cwd else (WORK_ROOT / slug)
    work.mkdir(parents=True, exist_ok=True)
    sid = sid or str(uuid.uuid4())
    if ai == "codex" and not login:
        ensure_codex_trust(work)
    cmd = login_launch_cmd(ai, work) if login else launch_cmd(ai, work, sid, model=model)
    r = _run(["respawn-pane", "-k", "-c", str(work), "-t", name, cmd])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).decode() or "tmux respawn failed")
    if ai == "codex" and not login:
        time.sleep(0.5)
        dismiss_codex_trust(name)
    return {
        "id": session_id(name),
        "tmux": name,
        "slug": slug,
        "session_id": sid,
        "cwd": str(work),
        "ai": ai,
        "model": model or "",
        "title": f"{slug} — {ai}",
    }


def kill(name: str) -> None:
    _run(["kill-session", "-t", name])


_send_lock = threading.Lock()


def send_text(name: str, text: str, enter: bool = True) -> None:
    """Paste into the Grok TUI, then submit.

    Enter must wait: Grok uses bracketed paste, so an Enter in the same
    tick becomes a newline in the composer instead of a send.
    A second Enter/Escape while waiting_for_model aborts the turn.
    """
    if not name:
        return
    try:
        pane = capture_pane(name, 24)
        if pane_overlay(pane) == "rewind":
            send_keys(name, "Escape")
            time.sleep(0.2)
    except Exception:
        pass
    text = (text or "").replace("\r\n", "\n").rstrip("\n\r")
    if text:
        payload = text.encode("utf-8")
        buf = f"heavy-{uuid.uuid4().hex[:12]}"
        with _send_lock:
            r = _run(["load-buffer", "-b", buf, "-"], input_bytes=payload, timeout=5)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or b"tmux load-buffer failed").decode())
            r2 = _run(["paste-buffer", "-d", "-b", buf, "-t", name], timeout=5)
            if r2.returncode != 0:
                try:
                    _run(["delete-buffer", "-b", buf], timeout=2)
                except Exception:
                    pass
                raise RuntimeError((r2.stderr or b"tmux paste failed").decode())
        time.sleep(0.35)
    if enter:
        time.sleep(0.2)
        r = _run(["send-keys", "-t", name, "C-m"])
        if r.returncode != 0:
            raise RuntimeError((r.stderr or b"tmux enter failed").decode())


def send_keys(name: str, *keys: str) -> None:
    if not name or not keys:
        return
    r = _run(["send-keys", "-t", name, *keys])
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b"tmux send-keys failed").decode() or "tmux send-keys failed")


def _command_running(pane: str) -> bool:
    return bool(re.search(r"command still running", pane or "", re.I))


def _still_running(pane: str) -> bool:
    return bool(pane_busy(pane) or pane_working(pane) or _command_running(pane))


def _clear_idle_draft(name: str, pane: str | None = None) -> None:
    """After a cancel, leftover text in the composer must not become the next turn."""
    p = pane if pane is not None else capture_pane(name, 16)
    if _still_running(p) or pane_overlay(p):
        return
    if re.search(r"│\s*❯\s+\S", p or ""):
        send_keys(name, "C-c")


def _esc(name: str) -> None:
    # Grok 4.6: Esc cancels the turn. Send the named key and C-[ so tmux
    # delivers a real ESC even when the keyboard normalizer drops one form.
    send_keys(name, "Escape")
    send_keys(name, "C-[")


def interrupt(name: str, force: bool = False) -> None:
    """Cancel the live Grok turn (same as tapping [stop] / Esc:cancel).

    Esc first — never Ctrl+C first. With a draft, Ctrl+C only clears the
    composer and the turn keeps going. Two Escapes while idle opens rewind.
    """
    if not name:
        raise RuntimeError("geen sessie")
    pane = capture_pane(name, 40)
    if pane_overlay(pane) == "rewind":
        send_keys(name, "Escape")
        return
    running = _still_running(pane)
    if not running:
        if not force or pane_has_idle_prompt(pane):
            if force:
                _clear_idle_draft(name, pane)
            return
    _esc(name)
    time.sleep(0.12)
    pane = capture_pane(name, 24)
    if pane_overlay(pane) == "rewind":
        send_keys(name, "Escape")
        return
    if not _still_running(pane):
        _clear_idle_draft(name, pane)
        return
    _esc(name)
    time.sleep(0.12)
    pane = capture_pane(name, 24)
    if pane_overlay(pane) == "rewind":
        send_keys(name, "Escape")
        return
    if not _still_running(pane):
        _clear_idle_draft(name, pane)
        return
    # Empty composer: Ctrl+C cancels. Mid-tool: also kills the shell child.
    send_keys(name, "C-c")
    time.sleep(0.08)
    if _command_running(capture_pane(name, 16)):
        send_keys(name, "C-c")
        time.sleep(0.06)
    _esc(name)
    time.sleep(0.08)
    _clear_idle_draft(name)


def capture_pane(name: str, lines: int = 90) -> str:
    r = _run(["capture-pane", "-t", name, "-p", "-J", "-S", f"-{max(20, int(lines))}"])
    if r.returncode != 0:
        return ""
    return r.stdout.decode("utf-8", "replace")


_SID_RE = re.compile(r"(?:--session-id|--resume)[=\s]+(\S+)")


def pane_pid(name: str) -> int:
    r = _run(["display-message", "-p", "-t", name, "#{pane_pid}"])
    try:
        return int((r.stdout or b"").decode().strip())
    except (TypeError, ValueError):
        return 0


def _cmd_of(pid: int) -> str:
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


def _children(pid: int) -> list[int]:
    try:
        r = subprocess.run(
            ["pgrep", "-P", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return []
    out: list[int] = []
    for bit in (r.stdout or "").split():
        try:
            out.append(int(bit))
        except ValueError:
            continue
    return out


_live_sid_cache: dict[str, tuple[float, str]] = {}


def live_session_id(name: str) -> str:
    """Grok --session-id of the process actually running in this tmux pane."""
    now = time.time()
    hit = _live_sid_cache.get(name)
    if hit and now - hit[0] < 8:
        return hit[1]
    pid = pane_pid(name)
    if not pid:
        return ""
    seen: set[int] = set()
    queue = [pid]
    sid = ""
    while queue:
        p = queue.pop(0)
        if p in seen or len(seen) > 24:
            continue
        seen.add(p)
        m = _SID_RE.search(_cmd_of(p))
        if m:
            sid = m.group(1).strip().strip("\"'")
            break
        queue.extend(_children(p))
    if sid:
        _live_sid_cache[name] = (now, sid)
    return sid


def find_by_id(wid: int) -> dict | None:
    for s in list_sessions(include_helpers=True):
        if s["id"] == wid:
            return s
    return None


def wait_ready(name: str, timeout: float = 18.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        pane = capture_pane(name, 24)
        if not pane.strip():
            time.sleep(0.35)
            continue
        if pane_busy(pane):
            time.sleep(0.35)
            continue
        return True
    return False


def spawn_helper(parent_slug: str, cwd: str | None = None, ai: str = "grok", model: str = "") -> dict:
    parent = "".join(ch if ch.isalnum() else "-" for ch in (parent_slug or "bot")).strip("-")[:24] or "bot"
    existing = {s["tmux"] for s in list_sessions(include_helpers=True)}
    n = 1
    while True:
        slug = f"{HELPER_PREFIX}{parent}-{n}"
        name = PREFIX + slug
        if name not in existing:
            break
        n += 1
    info = spawn(slug, cwd=cwd, ai=ai, model=model)
    info["helper"] = True
    info["parent"] = parent
    return info

"""CEO, per-agent memory, tasks and schedule."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.home() / ".grok" / "imac-phone"
ROSTER_FILE = ROOT / "roster.json"
NICK_FILE = ROOT / "nicknames.json"
AGENTS_DIR = ROOT / "agents"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (label or "").lower())
    s = re.sub(r"-+", "-", s).strip("-")[:36]
    return s or "agent"


_roster_lock = threading.Lock()


def _empty_roster() -> dict:
    return {"ceo": "", "home": "", "agents": {}, "order": [], "forgotten": []}


def _read_roster() -> dict:
    if ROSTER_FILE.is_file():
        try:
            data = json.loads(ROSTER_FILE.read_text(encoding="utf-8"))
            data.setdefault("ceo", "")
            data.setdefault("home", "")
            data.setdefault("agents", {})
            data.setdefault("order", [])
            data.setdefault("forgotten", [])
            _normalize_order(data)
            return data
        except Exception:
            pass
    return _empty_roster()


def _union_helpers(old, new, keep: int = 24) -> list:
    """Merge helper records. A stale save must not wipe extra-agent chats."""
    out: list[dict] = []

    def match(a: dict, b: dict) -> bool:
        for field in ("session_id", "slug", "tmux"):
            va, vb = str(a.get(field) or ""), str(b.get(field) or "")
            if va and vb and va == vb:
                return True
        return False

    def merge(a: dict, b: dict) -> dict:
        m = dict(a)
        for k, v in b.items():
            if v:
                m[k] = v
        return m

    for src in list(old or []) + list(new or []):
        if not isinstance(src, dict):
            continue
        if not (src.get("session_id") or src.get("slug") or src.get("tmux")):
            continue
        found = False
        for i, ex in enumerate(out):
            if match(ex, src):
                out[i] = merge(ex, src)
                found = True
                break
        if not found:
            out.append(dict(src))
    if len(out) > keep:
        out = sorted(out, key=lambda h: str(h.get("created") or ""))[-keep:]
    return out


def load_roster() -> dict:
    with _roster_lock:
        return _read_roster()


def _normalize_order(data: dict) -> None:
    agents = data.setdefault("agents", {})
    ceo = data.get("ceo") or ""
    home = data.get("home") or ""
    seen = []
    if ceo and ceo in agents:
        seen.append(ceo)
    if home and home in agents and home not in seen:
        seen.append(home)
    for s in data.get("order") or []:
        if s in agents and s not in seen:
            seen.append(s)
    for s in agents:
        if s not in seen:
            seen.append(s)
    data["order"] = seen


def save_roster(data: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with _roster_lock:
        disk = _read_roster()
        agents = data.setdefault("agents", {})
        for slug, meta in agents.items():
            if not isinstance(meta, dict):
                continue
            dmeta = (disk.get("agents") or {}).get(slug) or {}
            if dmeta.get("helpers") or meta.get("helpers"):
                meta["helpers"] = _union_helpers(dmeta.get("helpers"), meta.get("helpers"))
        _normalize_order(data)
        try:
            _write_roster_raw(data)
        except OSError as exc:
            print(f"save_roster failed: {exc}", flush=True)


def agent_dir(slug: str) -> Path:
    d = AGENTS_DIR / slug
    d.mkdir(parents=True, exist_ok=True)
    mem = d / "memory.md"
    if not mem.exists():
        mem.write_text(f"# Memory · {slug}\n\nShort facts. No passwords.\n", encoding="utf-8")
    tasks = d / "tasks.json"
    if not tasks.exists():
        tasks.write_text("[]\n", encoding="utf-8")
    sched = d / "schedule.json"
    if not sched.exists():
        sched.write_text("[]\n", encoding="utf-8")
    loops = d / "loops.json"
    if not loops.exists():
        loops.write_text("[]\n", encoding="utf-8")
    return d


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def add_event(slug: str, kind: str, text: str) -> None:
    d = agent_dir(slug)
    ev = _read_json(d / "schedule.json", [])
    ev.append({"at": _now(), "kind": kind, "text": text})
    d.joinpath("schedule.json").write_text(json.dumps(ev[-80:], ensure_ascii=False, indent=2), encoding="utf-8")


def load_tasks(slug: str) -> list:
    return _read_json(agent_dir(slug) / "tasks.json", [])


def save_tasks(slug: str, tasks: list) -> None:
    agent_dir(slug).joinpath("tasks.json").write_text(
        json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def assign_task(slug: str, title: str, source: str = "CEO") -> dict:
    tasks = load_tasks(slug)
    item = {
        "id": str(uuid.uuid4())[:8],
        "title": title.strip(),
        "status": "open",
        "from": source,
        "created": _now(),
    }
    tasks.append(item)
    save_tasks(slug, tasks)
    add_event(slug, "task", f"{source}: {title.strip()}")
    return item


def complete_task(slug: str, task_id: str) -> None:
    tasks = load_tasks(slug)
    for t in tasks:
        if t.get("id") == task_id:
            t["status"] = "done"
            t["done"] = _now()
    save_tasks(slug, tasks)
    add_event(slug, "done", task_id)


def memory_path(slug: str) -> Path:
    return agent_dir(slug) / "memory.md"


def read_memory(slug: str) -> str:
    return memory_path(slug).read_text(encoding="utf-8")


def write_memory(slug: str, text: str) -> None:
    memory_path(slug).write_text(text, encoding="utf-8")
    add_event(slug, "memory", "memory updated")


ROLE_START = "<!-- ROLE -->"
ROLE_END = "<!-- /ROLE -->"
TEAM_START = "<!-- TEAM -->"
TEAM_END = "<!-- /TEAM -->"

_ROLE_BY_LABEL = {
    "ceo": "boss",
    "degero": "degero.nl — SEO, site, GACS. No Apple Ads, no X posts, no App Store.",
    "ads bot": "Apple Search Ads / campaigns. No degero.nl code, no X posts.",
    "apple ads": "Apple Search Ads / campaigns. No degero.nl code, no X posts.",
    "apps bot": "Improve Tim Grootes Apple apps (code, TestFlight, bugs). No ads steering, no degero.nl.",
    "apps store bot": "App Store Connect: sales, listings, review. No ads, no degero.nl.",
    "swarm": "Swarm app (UI/server). No client work, no ads.",
    "swarm bot": "Swarm app (UI/server). No client work, no ads.",
    "video bot": "YouTube / video / Klantads visuals. No ads budget, no degero.nl code.",
    "x": "X/Twitter and Klantads content. NEVER t.grootes@gmail.com — only hello@sitebirds.com.",
}


def _label_role(label: str, is_ceo: bool) -> str:
    if is_ceo:
        return "boss"
    key = (label or "").strip().lower()
    if key in _ROLE_BY_LABEL:
        return _ROLE_BY_LABEL[key]
    for needle, text in _ROLE_BY_LABEL.items():
        if needle != "ceo" and needle in key:
            return text
    return "specialist — stay on your topic, send the rest via the boss."


def role_card(slug: str, meta: dict, roster: dict) -> str:
    label = str((meta or {}).get("label") or slug)
    is_ceo = (roster.get("ceo") == slug) or str((meta or {}).get("role") or "") == "ceo"
    others = []
    for s, m in (roster.get("agents") or {}).items():
        if s.startswith("h--") or (m or {}).get("helper"):
            continue
        lab = str((m or {}).get("label") or s)
        others.append(f"{lab} ({'boss' if s == roster.get('ceo') else 'worker'})")
    team = ", ".join(others) or "just you"
    role = _label_role(label, is_ceo)
    if is_ceo:
        return (
            f"You are **{label}**, the boss of Swarm.\n"
            f"Team: {team}.\n"
            "- You assign work. Do not do ads, SEO, X or App Store yourself if a specialist exists.\n"
            "- Typed to you: Swarm forwards automatically to the right bot (Degero, ADS, X, …).\n"
            "- Manual: huddle to one owner. Never `to: all` (Tim does that in Swarm).\n"
            "- Workers do not huddle. They write SHARED.md or wait. Ack/STOP-ack is noise — ignore it.\n"
            "- When someone is done: read SHARED.md, forward real work, or close it. Do not ack-pingpong."
        )
    return (
        f"You are **{label}**, a worker in Swarm.\n"
        f"Role: {role}\n"
        f"Team: {team}.\n"
        "- The boss delegates; you do your part and stop. Do not ack.\n"
        "- Do not huddle. No outbox. Facts go in SHARED.md. Wait for the boss.\n"
        "- Stay in your role. Other work: wait. Do not message the boss."
    )


def upsert_marked_block(text: str, start: str, end: str, body: str) -> str:
    block = f"{start}\n{body.strip()}\n{end}"
    cur = text or ""
    i = cur.find(start)
    j = cur.find(end, i + len(start)) if i >= 0 else -1
    if i >= 0 and j >= 0:
        return cur[:i] + block + cur[j + len(end) :]
    if cur.strip():
        return block + "\n\n" + cur.lstrip()
    return block + "\n"


def ensure_role_memory(slug: str, meta: dict | None = None, roster: dict | None = None) -> None:
    roster = roster if roster is not None else load_roster()
    meta = meta or (roster.get("agents") or {}).get(slug) or {}
    if not slug or slug.startswith("h--") or (meta or {}).get("helper"):
        return
    path = memory_path(slug)
    agent_dir(slug)
    cur = path.read_text(encoding="utf-8") if path.exists() else f"# Memory · {slug}\n"
    card = role_card(slug, meta, roster)
    nxt = upsert_marked_block(cur, ROLE_START, ROLE_END, card)
    if nxt != cur:
        path.write_text(nxt, encoding="utf-8")


def ensure_team_roles(roster: dict | None = None) -> None:
    roster = roster if roster is not None else load_roster()
    for slug, meta in (roster.get("agents") or {}).items():
        try:
            ensure_role_memory(slug, meta, roster)
        except Exception:
            pass
    try:
        shared = ROOT / "shared-memory.md"
        if shared.is_file():
            cur = shared.read_text(encoding="utf-8")
            lines = ["## Team", "One boss assigns. Specialists stay in role. Bots do not huddle each other."]
            for slug, meta in (roster.get("agents") or {}).items():
                if slug.startswith("h--") or (meta or {}).get("helper"):
                    continue
                lab = str((meta or {}).get("label") or slug)
                is_ceo = slug == roster.get("ceo")
                lines.append(f"- **{lab}** — {_label_role(lab, is_ceo)}")
            nxt = upsert_marked_block(cur, TEAM_START, TEAM_END, "\n".join(lines))
            if nxt != cur:
                shared.write_text(nxt, encoding="utf-8")
    except Exception:
        pass


def sender_is_ceo(from_label: str, roster: dict | None = None) -> bool:
    roster = roster if roster is not None else load_roster()
    raw = (from_label or "").strip().lower()
    if not raw:
        return False
    if raw in {"ceo", "boss", "baas"}:
        return True
    ceo = str(roster.get("ceo") or "")
    if raw == ceo.lower():
        return True
    meta = (roster.get("agents") or {}).get(ceo) or {}
    return raw == str(meta.get("label") or "").strip().lower()


_LEAK_CMD = re.compile(
    r"^\s*(?:lek|leak|stuur|leg|go)\s+(?:naar|to|na)\s+(.+?)\s*$",
    re.I,
)


def parse_leak_command(text: str) -> str | None:
    """'lek naar ads' / 'leak to degero' → target name, else None."""
    m = _LEAK_CMD.match((text or "").strip())
    if not m:
        return None
    name = (m.group(1) or "").strip()
    return name or None


def last_user_question(slug: str, skip: str = "", max_age_s: int = 600) -> str:
    """Most recent real user line in this chat, skipping leak-commands and system prefixes."""
    skip_n = re.sub(r"\s+", " ", (skip or "").strip()).lower()
    now = time.time()
    for item in reversed(load_swarm_msgs(slug)):
        if item.get("role") != "user":
            continue
        t = str(item.get("text") or "").strip()
        if not t or t.startswith("["):
            continue
        if parse_leak_command(t):
            continue
        if re.sub(r"\s+", " ", t).lower() == skip_n:
            continue
        at = str(item.get("at") or "")
        if at and max_age_s > 0:
            try:
                ts = datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
                if now - ts > max_age_s:
                    continue
            except ValueError:
                pass
        return t
    return ""


_KEEP_CEO = re.compile(
    r"^(ja|nee|ok|oké|okay|prima|top|goed|klopt|stop|wacht|thanks|dank|"
    r"wie is|welke bots|wat is de stand|hoe werkt|automatiseer|"
    r"alleen jij|blijf jij)\b|"
    r"2de agent|tweede agent|extra agent|nog afmaakt|nog bezig|"
    r"zet je deze vraag|zie ik even niet",
    re.I,
)

_ROUTE_HINTS = (
    (3, ("degero.nl", "degero", "gacs", "gbs systeem", "meet- en regel"), ("degero",)),
    (3, ("apple ads", "search ads", "campagne", "campagnes", "cpa", "ads spend"), ("ads",)),
    (3, ("app store connect", "testflight", "aso", "listings", "verkopen app"), ("store", "apps store")),
    (2, ("naptara", "pupwatch", "homevel", "bloomcove", "docmint", "xcode", "testflight"), ("apps bot",)),
    (3, ("youtube", "short", "video bot", "klantads video"), ("video",)),
    (3, ("twitter", "tweet", "x.com", "posten op x", "klantads"), ("x",)),
    (3, ("swarm", "bubbel", "chatvenster", "live balk"), ("swarm",)),
)


def pick_routes(text: str, roster: dict | None = None, ceo_slug: str = "") -> list[str]:
    """Which worker slugs should get this CEO message. Empty = stay with the baas."""
    roster = roster if roster is not None else load_roster()
    body = (text or "").strip()
    if not body or body.startswith("["):
        return []
    if _KEEP_CEO.match(body):
        return []
    ceo_slug = ceo_slug or str(roster.get("ceo") or "")
    low = body.lower()
    if re.search(r"\b(iedereen|alle bots|heel het team|allemaal)\b", low):
        return [
            s
            for s, m in (roster.get("agents") or {}).items()
            if s != ceo_slug and not str(s).startswith("h--") and not (m or {}).get("helper")
        ]
    scores: dict[str, int] = {}
    agents = roster.get("agents") or {}
    for slug, meta in agents.items():
        if slug == ceo_slug or str(slug).startswith("h--") or (meta or {}).get("helper"):
            continue
        lab = str((meta or {}).get("label") or slug).strip().lower()
        if len(lab) >= 3 and (re.search(rf"\b{re.escape(lab)}\b", low) or lab in low):
            scores[slug] = scores.get(slug, 0) + 5
        elif len(lab) < 3 and re.search(rf"\b{re.escape(lab)}\b", low):
            scores[slug] = scores.get(slug, 0) + 5
    for weight, needles, labels in _ROUTE_HINTS:
        if not any(n in low for n in needles):
            continue
        for slug, meta in agents.items():
            if slug == ceo_slug or str(slug).startswith("h--") or (meta or {}).get("helper"):
                continue
            lab = str((meta or {}).get("label") or slug).strip().lower()
            if any(tag in lab for tag in labels):
                scores[slug] = scores.get(slug, 0) + weight
    if not scores:
        return []
    best = max(scores.values())
    if best < 2:
        return []
    picked = [s for s, n in scores.items() if n >= best and n >= 2]
    return picked[:2]


def auto_delegate(ceo_slug: str, text: str, dispatch, helper=None, is_busy=None, inbox=None) -> list[str]:
    """Idle specialist gets the job. Busy specialist gets a visible pill (default: queue)."""
    rost = load_roster()
    slugs = pick_routes(text, rost, ceo_slug)
    sent: list[str] = []
    for slug in slugs:
        meta = (rost.get("agents") or {}).get(slug) or {}
        label = str(meta.get("label") or slug)
        win = {
            "slug": slug,
            "id": meta.get("window_id"),
            "tmux": meta.get("tmux") or "",
            "session_id": meta.get("session_id") or "",
            "title": meta.get("title") or label,
        }
        try:
            assign_task(slug, text[:200], source="CEO")
        except Exception:
            pass
        payload = f"[Boss → {label}]: {text.strip()}"
        try:
            busy = bool(is_busy and is_busy(win))
            if busy and inbox:
                inbox(slug, text.strip())
                sent.append(label + " · pill")
            elif busy and helper:
                helper(win, slug, payload)
                sent.append(label + " · extra agent")
            else:
                dispatch(win, payload, True)
                sent.append(label)
        except Exception:
            pass
    return sent


def find_agent_by_name(name: str, roster: dict | None = None) -> tuple[str, dict] | None:
    roster = roster if roster is not None else load_roster()
    want = (name or "").strip().lower()
    if not want or want == "all":
        return None
    agents = roster.get("agents") or {}
    if want in agents:
        return want, agents[want]
    hits = []
    for slug, meta in agents.items():
        if slug.startswith("h--") or (meta or {}).get("helper"):
            continue
        lab = str((meta or {}).get("label") or "").strip().lower()
        if lab == want or want in lab or lab in want:
            hits.append((slug, meta))
        elif str(meta.get("window_id") or "") == want:
            hits.append((slug, meta))
    if len(hits) == 1:
        return hits[0]
    exact = [h for h in hits if str((h[1] or {}).get("label") or "").strip().lower() == want]
    return exact[0] if len(exact) == 1 else None


def _wid(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


_STATUS_LABEL = re.compile(
    r"thinking|waiting|preparing|inspect|read_file|search_replace|worked for|"
    r"dump|open |remote iphone|live roster|waiting for response|"
    r"\bgrok\s*[—–-]\s*grok\b",
    re.I,
)


def is_status_label(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if t.startswith("- ") or t.startswith("—"):
        return True
    if len(t.split()) > 3:
        return True
    if len(t) > 36 and ("—" in t or "–" in t):
        return True
    return bool(_STATUS_LABEL.search(t))


_TITLE_SKIP = {
    "timgrootes",
    "terminal",
    "waiting",
    "thinking",
    "writing",
    "reading",
    "command",
    "preparing",
    "response",
    "grok",
}


def slug_from_title(title: str) -> str:
    """Stable bot slug from a Terminal title. Never a chat/status sentence."""
    t = (title or "").strip()
    if not t:
        return ""
    m = re.search(r"\b(bot-\d+)\b", t, re.I)
    if m:
        return m.group(1).lower()
    # "naptara — grok" / "naptara - grok" at the start only.
    m = re.match(r"^([a-z][a-z0-9-]{1,32})\s*[—–-]\s*grok\b", t, re.I)
    if m:
        slug = m.group(1).lower()
        if slug not in _TITLE_SKIP and not is_status_label(slug):
            return slug
    # "Waiting for response… — naptara — grok" (em/en dash, not hyphen soup).
    m = re.search(r"[—–]\s*([a-z][a-z0-9-]{1,32})\s*[—–]\s*grok\s*$", t, re.I)
    if m:
        slug = m.group(1).lower()
        if slug not in _TITLE_SKIP and not is_status_label(slug):
            return slug
    return ""


def window_looks_claimed(w: dict, label: str = "") -> bool:
    """True only for a real Swarm bot window, not a random Grok TUI."""
    if str(w.get("tmux") or "").strip():
        return True
    explicit = str(w.get("slug") or "").strip()
    if explicit and not explicit.startswith("h--") and not is_status_label(explicit):
        return True
    if slug_from_title(str(w.get("title") or label or "")):
        return True
    return False


def load_nicks() -> dict:
    data = _read_json(NICK_FILE, {})
    return {
        "by_window": {str(k): v for k, v in (data.get("by_window") or {}).items() if isinstance(v, dict)},
        "by_session": {str(k): v for k, v in (data.get("by_session") or {}).items() if isinstance(v, dict)},
    }


def save_nicks(data: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    NICK_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def remember_nick(meta: dict, name: str, extra: dict | None = None) -> None:
    name = (name or "").strip()[:40]
    if not name or is_status_label(name):
        return
    nicks = load_nicks()
    patch = {"label": name}
    if extra:
        patch.update({k: v for k, v in extra.items() if v})
    sid = str(meta.get("session_id") or "").strip()
    if sid:
        patch["session_id"] = sid
    wid = _wid(meta.get("window_id"))
    if wid is not None:
        nicks["by_window"][str(wid)] = {**nicks["by_window"].get(str(wid), {}), **patch}
    if sid:
        nicks["by_session"][sid] = {**nicks["by_session"].get(sid, {}), **patch}
    save_nicks(nicks)


def nick_for(meta: dict) -> dict:
    nicks = load_nicks()
    hit: dict = {}
    sid = str(meta.get("session_id") or "").strip()
    if sid:
        hit = {**hit, **(nicks["by_session"].get(sid) or {})}
    wid = _wid(meta.get("window_id"))
    if wid is not None:
        hit = {**hit, **(nicks["by_window"].get(str(wid)) or {})}
    return hit


def apply_nicks(roster: dict) -> dict:
    ceo_slug = None
    owned = {
        str(m.get("session_id") or "")
        for s, m in (roster.get("agents") or {}).items()
        if m.get("session_id") and not (str(s).startswith("h--") or m.get("helper"))
    }
    for slug, meta in (roster.get("agents") or {}).items():
        if slug.startswith("h--") or meta.get("helper"):
            continue
        hit = nick_for(meta)
        if hit.get("label"):
            hid = str(hit.get("session_id") or "").strip()
            sid = str(meta.get("session_id") or "").strip()
            # A recycled window id must not rename another bot (ASK onto a stray).
            if hid and sid and hid != sid and _has_custom_label(meta):
                pass
            else:
                meta["label"] = hit["label"]
                meta["auto"] = False
        if hit.get("icon") and not meta.get("icon"):
            meta["icon"] = hit["icon"]
        if hit.get("color") and not meta.get("color"):
            meta["color"] = hit["color"]
        if hit.get("role") == "ceo":
            ceo_slug = slug
        sid = str(hit.get("session_id") or "").strip()
        if sid and not meta.get("session_id") and sid not in owned:
            meta["session_id"] = sid
            owned.add(sid)
    if ceo_slug:
        roster["ceo"] = ceo_slug
        for s, meta in roster["agents"].items():
            if s.startswith("h--") or meta.get("helper"):
                continue
            meta["role"] = "ceo" if s == ceo_slug else "worker"
    return roster


def _is_helper_slug(slug: str, meta: dict | None = None) -> bool:
    return str(slug or "").startswith("h--") or bool((meta or {}).get("helper"))


def _has_custom_label(meta: dict) -> bool:
    if meta.get("auto") is False:
        return True
    cur = str(meta.get("label") or "").strip()
    return bool(cur and not is_status_label(cur))


def _find_agent_slug(roster: dict, w: dict) -> str | None:
    """Reuse an existing agent. Window IDs and tmux names are stable; titles are not."""
    agents = roster.get("agents") or {}
    wid = _wid(w.get("id"))
    tmux = str(w.get("tmux") or "").strip()
    sid = str(w.get("session_id") or "").strip()
    if wid is not None:
        for s, meta in agents.items():
            if _is_helper_slug(s, meta):
                continue
            if _wid(meta.get("window_id")) == wid:
                return s
    if tmux:
        for s, meta in agents.items():
            if _is_helper_slug(s, meta):
                continue
            if str(meta.get("tmux") or "") == tmux:
                return s
    if sid:
        for s, meta in agents.items():
            if _is_helper_slug(s, meta):
                continue
            if str(meta.get("session_id") or "") == sid:
                return s
    title_slug = slug_from_title(str(w.get("title") or ""))
    if title_slug and title_slug in agents and not _is_helper_slug(title_slug, agents.get(title_slug)):
        return title_slug
    win_slug = str(w.get("slug") or "").strip()
    if win_slug and win_slug in agents and not _is_helper_slug(win_slug, agents.get(win_slug)):
        return win_slug
    return None


def _merge_agent_meta(keep: dict, extra: dict) -> None:
    extra_label = str(extra.get("label") or "").strip()
    extra_custom = extra.get("auto") is False or (extra_label and not is_status_label(extra_label))
    if extra_custom and not _has_custom_label(keep) and extra_label:
        keep["label"] = extra_label
        keep["auto"] = False
    elif extra.get("auto") is False:
        keep["auto"] = False
    for k in ("icon", "color", "ai", "model", "session_id", "tmux", "tty"):
        if extra.get(k) and not keep.get(k):
            keep[k] = extra[k]
    hs = list(keep.get("helpers") or [])
    seen_h = {(h.get("slug"), h.get("tmux")) for h in hs}
    for h in extra.get("helpers") or []:
        key = (h.get("slug"), h.get("tmux"))
        if key not in seen_h:
            hs.append(h)
            seen_h.add(key)
    if hs:
        keep["helpers"] = hs
    if extra.get("role") == "ceo":
        keep["role"] = "ceo"


def _pick_keeper(sa: str, ma: dict, sb: str, mb: dict, roster: dict) -> str:
    if roster.get("ceo") == sa:
        return sa
    if roster.get("ceo") == sb:
        return sb
    ca, cb = _has_custom_label(ma), _has_custom_label(mb)
    if ca != cb:
        return sa if ca else sb
    score = lambda m: (
        len(m.get("helpers") or []),
        1 if m.get("session_id") else 0,
        1 if m.get("icon") else 0,
        1 if m.get("color") else 0,
    )
    return sa if score(ma) >= score(mb) else sb


def _drop_slug(roster: dict, slug: str) -> None:
    roster["agents"].pop(slug, None)
    roster["order"] = [s for s in roster.get("order") or [] if s != slug]
    if roster.get("ceo") == slug:
        roster["ceo"] = next((s for s in roster["agents"] if not _is_helper_slug(s, roster["agents"].get(s))), "")
    if roster.get("home") == slug:
        roster["home"] = ""


def merge_duplicate_agents(roster: dict) -> dict:
    """One agent per window_id / tmux. Keep custom names, helpers, icons."""
    agents = roster.get("agents") or {}

    def collapse(key_fn) -> None:
        buckets: dict = {}
        for s, meta in list(agents.items()):
            if _is_helper_slug(s, meta):
                continue
            key = key_fn(meta)
            if key is None:
                continue
            buckets.setdefault(key, []).append(s)
        for slugs in buckets.values():
            if len(slugs) < 2:
                continue
            keep = slugs[0]
            for s in slugs[1:]:
                keep = _pick_keeper(keep, agents[keep], s, agents[s], roster)
            for s in slugs:
                if s == keep:
                    continue
                _merge_agent_meta(agents[keep], agents[s])
                _drop_slug(roster, s)

    collapse(lambda m: _wid(m.get("window_id")))
    collapse(lambda m: str(m.get("tmux") or "") or None)
    return roster


def _stable_new_slug(roster: dict, w: dict, label: str) -> str:
    """Never mint a slug from a changing Terminal/chat title."""
    cand = str(w.get("slug") or "").strip()
    if cand and not cand.startswith("h--") and cand not in roster["agents"]:
        if cand.startswith("bot-") or not is_status_label(cand):
            return cand
    wid = _wid(w.get("id"))
    if wid is not None:
        return f"bot-{wid}"
    if label and not is_status_label(label):
        s = slugify(label)
        if s and s not in roster["agents"]:
            return s
    return f"bot-{int(time.time()) % 100000}"


def sync_from_windows(windows: list[dict], label_fn) -> dict:
    roster = load_roster()
    merge_duplicate_agents(roster)
    seen = set()
    for w in windows:
        if is_forgotten(
            slug=str(w.get("slug") or ""),
            window_id=w.get("id"),
            tmux=str(w.get("tmux") or ""),
            session_id=str(w.get("session_id") or ""),
            roster=roster,
        ):
            continue
        label = label_fn(w)
        slug = _find_agent_slug(roster, w)
        if slug and is_forgotten(slug=slug, roster=roster):
            continue
        if not slug:
            if not window_looks_claimed(w, label):
                continue
            slug = _stable_new_slug(roster, w, label)
            if is_forgotten(slug=slug, roster=roster):
                continue
        if _is_helper_slug(slug, roster["agents"].get(slug)):
            continue
        if slug in seen:
            continue
        seen.add(slug)
        meta = roster["agents"].setdefault(slug, {"role": "worker", "auto": True, "ai": "grok"})
        cur = str(meta.get("label") or "").strip()
        # Never overwrite a locked or custom name with a chat/status title.
        if meta.get("auto") is False or (cur and not is_status_label(cur)):
            meta["auto"] = False
        elif not is_status_label(label):
            meta["label"] = label
            meta["auto"] = False
        elif not cur:
            meta["label"] = "Bot"
        meta["window_id"] = _wid(w.get("id")) if _wid(w.get("id")) is not None else w.get("id")
        meta["title"] = w.get("title")
        if w.get("tmux"):
            meta["tmux"] = w["tmux"]
        if w.get("session_id") and not meta.get("session_id"):
            taken = {
                str(m.get("session_id") or "")
                for s, m in roster["agents"].items()
                if s != slug and m.get("session_id")
            }
            if str(w["session_id"]) not in taken:
                meta["session_id"] = w["session_id"]
        if not meta.get("session_id"):
            nicks = load_nicks()
            hit = (nicks.get("by_window") or {}).get(str(meta.get("window_id") or "")) or {}
            sid = str(hit.get("session_id") or "").strip()
            if sid:
                taken = {
                    str(m.get("session_id") or "")
                    for s, m in roster["agents"].items()
                    if s != slug and m.get("session_id")
                }
                if sid not in taken:
                    meta["session_id"] = sid
        if w.get("tty") and not meta.get("tty"):
            meta["tty"] = w["tty"]
        meta.setdefault("ai", "grok")
        meta["updated"] = _now()
        agent_dir(slug)
    merge_duplicate_agents(roster)
    if not roster.get("ceo") and roster["agents"]:
        pick = next((s for s, m in roster["agents"].items() if "ceo" in (m.get("label") or "").lower()), None)
        roster["ceo"] = pick or next(iter(roster["agents"]))
        roster["agents"][roster["ceo"]]["role"] = "ceo"
    if roster.get("ceo") and roster["ceo"] in roster["agents"]:
        roster["agents"][roster["ceo"]]["role"] = "ceo"
    apply_nicks(roster)
    save_roster(roster)
    return roster


def rename_agent(slug: str, name: str) -> dict:
    roster = load_roster()
    name = (name or "").strip()[:40]
    if slug not in roster["agents"] or not name:
        raise ValueError("unknown agent or empty name")
    roster["agents"][slug]["label"] = name
    roster["agents"][slug]["auto"] = False
    remember_nick(roster["agents"][slug], name)
    save_roster(roster)
    return roster


def _forget_entry(slug: str = "", meta: dict | None = None) -> dict:
    meta = meta or {}
    out = {}
    if slug:
        out["slug"] = str(slug)
    if meta.get("tmux"):
        out["tmux"] = str(meta["tmux"])
    sid = str(meta.get("session_id") or "").strip()
    if sid:
        out["session_id"] = sid
    wid = _wid(meta.get("window_id"))
    if wid is not None:
        out["window_id"] = wid
    if meta.get("tty"):
        out["tty"] = str(meta["tty"])
    return out


def forgotten_list(roster: dict | None = None) -> list[dict]:
    rost = roster if roster is not None else load_roster()
    rows = rost.get("forgotten") or []
    return [r for r in rows if isinstance(r, dict)]


def is_forgotten(slug: str = "", window_id=None, tmux: str = "", session_id: str = "", tty: str = "", roster: dict | None = None) -> bool:
    wid = _wid(window_id)
    slug = str(slug or "")
    tmux = str(tmux or "")
    sid = str(session_id or "").strip()
    tty = str(tty or "").strip()
    for row in forgotten_list(roster):
        if slug and row.get("slug") == slug:
            return True
        if tmux and row.get("tmux") == tmux:
            return True
        if sid and row.get("session_id") == sid:
            return True
        if wid is not None and _wid(row.get("window_id")) == wid:
            return True
        if tty and str(row.get("tty") or "") == tty:
            return True
    return False


def remember_forgotten(slug: str, meta: dict | None = None) -> dict:
    """Mark a bot as deleted so sync/list will not resurrect it."""
    roster = load_roster()
    entry = _forget_entry(slug, meta)
    if not entry:
        return roster
    rows = forgotten_list(roster)
    if not any(
        (entry.get("slug") and r.get("slug") == entry.get("slug"))
        or (entry.get("tmux") and r.get("tmux") == entry.get("tmux"))
        or (entry.get("session_id") and r.get("session_id") == entry.get("session_id"))
        or (
            entry.get("window_id") is not None
            and _wid(r.get("window_id")) == _wid(entry.get("window_id"))
        )
        for r in rows
    ):
        rows.append(entry)
    roster["forgotten"] = rows[-80:]
    save_roster(roster)
    return roster


def forget_nicks(meta: dict | None = None) -> None:
    meta = meta or {}
    nicks = load_nicks()
    wid = _wid(meta.get("window_id"))
    if wid is not None:
        (nicks.get("by_window") or {}).pop(str(wid), None)
    sid = str(meta.get("session_id") or "").strip()
    if sid:
        (nicks.get("by_session") or {}).pop(sid, None)
    save_nicks(nicks)


def drop_agent(window_id: int | None = None, slug: str | None = None) -> dict:
    roster = load_roster()
    drop = []
    for s, meta in roster["agents"].items():
        if slug and s == slug:
            drop.append(s)
        elif window_id and meta.get("window_id") == window_id:
            drop.append(s)
    for s in drop:
        roster["agents"].pop(s, None)
        if roster.get("ceo") == s:
            roster["ceo"] = next(iter(roster["agents"]), "")
            if roster["ceo"]:
                roster["agents"][roster["ceo"]]["role"] = "ceo"
        if roster.get("home") == s:
            roster["home"] = ""
    if drop:
        roster["order"] = [s for s in roster.get("order") or [] if s not in drop]
    save_roster(roster)
    return roster


def set_ceo(slug: str) -> dict:
    roster = load_roster()
    if slug not in roster["agents"]:
        raise ValueError("unknown agent")
    roster["ceo"] = slug
    for s, meta in roster["agents"].items():
        meta["role"] = "ceo" if s == slug else "worker"
    order = roster.setdefault("order", [])
    if slug in order:
        order.remove(slug)
    order.insert(0, slug)
    save_roster(roster)
    return roster


def set_home(slug: str) -> dict:
    """One default chat bot. Empty slug clears it."""
    roster = load_roster()
    slug = str(slug or "").strip()
    if slug and slug not in roster["agents"]:
        raise ValueError("unknown agent")
    if slug and (roster["agents"].get(slug) or {}).get("helper"):
        raise ValueError("a helper cannot be the default chat")
    roster["home"] = slug
    for s, meta in roster["agents"].items():
        if not isinstance(meta, dict):
            continue
        if slug and s == slug:
            meta["home"] = True
        else:
            meta.pop("home", None)
    save_roster(roster)
    return roster


def is_home(slug: str, roster: dict | None = None) -> bool:
    if not slug:
        return False
    rost = roster if roster is not None else load_roster()
    if str(rost.get("home") or "") == str(slug):
        return True
    meta = (rost.get("agents") or {}).get(slug) or {}
    return bool(meta.get("home"))


def public_roster(windows: list[dict], label_fn) -> dict:
    roster = sync_from_windows(windows, label_fn)
    agents = []
    for slug, meta in roster["agents"].items():
        if slug.startswith("h--") or meta.get("helper"):
            continue
        tasks = load_tasks(slug)
        open_tasks = [t for t in tasks if t.get("status") != "done"]
        sched = _read_json(agent_dir(slug) / "schedule.json", [])[-8:]
        agents.append(
            {
                "slug": slug,
                "label": (meta.get("label") if not is_status_label(str(meta.get("label") or "")) else "") or slug,
                "role": meta.get("role") or "worker",
                "window_id": meta.get("window_id"),
                "tmux": meta.get("tmux") or "",
                "last_submit_at": meta.get("last_submit_at"),
                "icon": meta.get("icon") or "",
                "color": meta.get("color") or "",
                "ai": normalize_ai(meta.get("ai")),
                "model": normalize_model(meta.get("ai"), meta.get("model")),
                "open_tasks": open_tasks,
                "schedule": list(reversed(sched)),
                "loops": public_loops(slug),
                "task_count": len(open_tasks),
                "home": slug == (roster.get("home") or ""),
            }
        )
    order = list(roster.get("order") or [])
    idx = {s: i for i, s in enumerate(order)}
    agents.sort(key=lambda a: (idx.get(a["slug"], 10_000), a["label"].lower()))
    return {
        "ceo": roster.get("ceo") or "",
        "home": roster.get("home") or "",
        "agents": agents,
        "order": order,
    }


AIS = ("grok", "claude", "codex")
MODELS = {
    "grok": [("grok-4.6", "4.6"), ("grok-4.5", "4.5")],
    "claude": [("sonnet", "Sonnet"), ("opus", "Opus"), ("haiku", "Haiku")],
    "codex": [("", "Default"), ("gpt-5.4", "GPT-5.4")],
}


def normalize_ai(ai: str | None) -> str:
    import re
    import shutil

    a = re.sub(r"[^a-z0-9_-]", "", (ai or "grok").strip().lower())[:32]
    if a in AIS:
        return a
    if a and shutil.which(a):
        return a
    return "grok"


def normalize_model(ai: str | None, model: str | None) -> str:
    ai = normalize_ai(ai)
    opts = [m[0] for m in MODELS.get(ai) or [("", "Default")]]
    if model in opts:
        return str(model)
    if ai not in MODELS:
        return str(model or "")
    return opts[0]


def set_ai(slug: str, ai: str) -> dict:
    roster = load_roster()
    if slug not in roster["agents"]:
        raise ValueError("unknown bot")
    roster["agents"][slug]["ai"] = normalize_ai(ai)
    save_roster(roster)
    return roster


def update_bot(
    slug: str,
    name: str | None = None,
    icon: str | None = None,
    color: str | None = None,
    ceo: bool | None = None,
    home: bool | None = None,
    ai: str | None = None,
    model: str | None = None,
) -> dict:
    roster = load_roster()
    if slug not in roster["agents"]:
        raise ValueError("unknown bot")
    if name is not None:
        name = (name or "").strip()[:40]
        if name:
            roster["agents"][slug]["label"] = name
            roster["agents"][slug]["auto"] = False
            remember_nick(roster["agents"][slug], name)
    if icon:
        icon = re.sub(r"[^a-z0-9-]+", "", icon.lower())[:24]
        if icon:
            roster["agents"][slug]["icon"] = icon
    if color:
        color = re.sub(r"[^#A-Fa-f0-9]", "", color)[:7]
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            roster["agents"][slug]["color"] = color
    if ai is not None:
        roster["agents"][slug]["ai"] = normalize_ai(ai)
        if model is None:
            roster["agents"][slug]["model"] = normalize_model(ai, roster["agents"][slug].get("model"))
    if model is not None:
        roster["agents"][slug]["model"] = normalize_model(roster["agents"][slug].get("ai"), model)
    if ceo:
        roster["ceo"] = slug
        for s, meta in roster["agents"].items():
            meta["role"] = "ceo" if s == slug else "worker"
    if home is True:
        roster["home"] = slug
        for s, meta in roster["agents"].items():
            if isinstance(meta, dict):
                if s == slug:
                    meta["home"] = True
                else:
                    meta.pop("home", None)
    elif home is False and roster.get("home") == slug:
        roster["home"] = ""
        roster["agents"][slug].pop("home", None)
    save_roster(roster)
    return roster


def set_icon(slug: str, icon: str, color: str = "") -> dict:
    roster = load_roster()
    icon = re.sub(r"[^a-z0-9-]+", "", (icon or "").lower())[:24]
    color = re.sub(r"[^#A-Fa-f0-9]", "", color or "")[:7]
    if slug not in roster["agents"]:
        raise ValueError("unknown bot")
    if icon:
        roster["agents"][slug]["icon"] = icon
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        roster["agents"][slug]["color"] = color
    save_roster(roster)
    return roster


def set_order(slugs: list[str]) -> dict:
    roster = load_roster()
    seen: list[str] = []
    for s in slugs:
        s = str(s or "").strip()
        if s and s in roster["agents"] and s not in seen:
            seen.append(s)
    for s in roster["agents"]:
        if s not in seen:
            seen.append(s)
    roster["order"] = seen
    save_roster(roster)
    return roster


def helpers_of(slug: str) -> list[dict]:
    meta = (load_roster().get("agents") or {}).get(slug) or {}
    return list(meta.get("helpers") or [])


def swarm_chat_path(slug: str) -> Path:
    return agent_dir(slug) / "swarm.jsonl"


def remember_swarm_msg(slug: str, role: str, text: str, **extra) -> None:
    text = (text or "").strip()
    if not slug or not text:
        return
    try:
        prev = load_swarm_msgs(slug)
        if prev and (prev[-1].get("role"), (prev[-1].get("text") or "").strip()) == (role, text):
            return
    except Exception:
        pass
    row = {"role": role, "text": text[:8000], "at": _now()}
    for key in ("helper", "thread", "task", "n", "meta", "name", "path", "mime"):
        if extra.get(key) is not None:
            row[key] = extra[key]
    line = json.dumps(row, ensure_ascii=False)
    p = swarm_chat_path(slug)
    with p.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    try:
        raw = p.read_text(encoding="utf-8").splitlines()
        if len(raw) > 120:
            p.write_text("\n".join(raw[-80:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def load_swarm_msgs(slug: str) -> list[dict]:
    p = swarm_chat_path(slug)
    if not p.is_file():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines()[-80:]:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if item.get("role") and item.get("text"):
                row = {"role": item["role"], "text": str(item["text"]), "swarm": True}
                for key in ("at", "helper", "thread", "task", "n", "meta", "name", "path", "mime"):
                    if item.get(key) is not None:
                        row[key] = item[key]
                out.append(row)
    except Exception:
        return []
    return out


def _write_roster_raw(data: dict) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = ROOT / f".roster.{os.getpid()}.{time.time_ns()}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ROSTER_FILE)


def remove_helper(parent_slug: str, helper_slug: str) -> dict | None:
    if not parent_slug:
        return None
    want = str(helper_slug or "").strip()
    hs = helpers_of(parent_slug)
    found = None
    keep = []
    for h in hs:
        slug = str(h.get("slug") or "")
        tmux = str(h.get("tmux") or "")
        if want and want in {slug, tmux, tmux.replace("heavy-", "", 1)}:
            found = h
            continue
        keep.append(h)
    if found is not None:
        replace_helpers(parent_slug, keep)
    return found


def replace_helpers(parent_slug: str, items: list) -> None:
    """Overwrite helpers. save_roster() would union old ones back."""
    if not parent_slug:
        return
    with _roster_lock:
        roster = _read_roster()
        meta = (roster.get("agents") or {}).get(parent_slug)
        if not meta:
            return
        meta["helpers"] = list(items or [])
        _write_roster_raw(roster)


def clear_helpers(slug: str) -> None:
    if not slug:
        return
    with _roster_lock:
        roster = _read_roster()
        meta = (roster.get("agents") or {}).get(slug)
        if not meta:
            return
        meta["helpers"] = []
        _write_roster_raw(roster)


def clear_all_helpers() -> None:
    with _roster_lock:
        roster = _read_roster()
        changed = False
        for meta in (roster.get("agents") or {}).values():
            if meta.get("helpers"):
                meta["helpers"] = []
                changed = True
        if changed:
            _write_roster_raw(roster)


def add_helper(parent_slug: str, info: dict, keep: int = 24) -> dict:
    roster = load_roster()
    if parent_slug not in roster["agents"]:
        raise ValueError("unknown bot")
    item = {
        "slug": info.get("slug") or "",
        "tmux": info.get("tmux") or "",
        "session_id": info.get("session_id") or "",
        "task": (info.get("task") or "")[:400],
        "created": _now(),
    }
    if info.get("busy_since") is not None:
        item["busy_since"] = info.get("busy_since")
    if info.get("last_submit_at") is not None:
        item["last_submit_at"] = info.get("last_submit_at")
    hs = _union_helpers(roster["agents"][parent_slug].get("helpers"), [item], keep=keep)
    dropped = []
    roster["agents"][parent_slug]["helpers"] = hs
    save_roster(roster)
    return {"helper": item, "dropped": dropped}


def merge_helpers(parent_slug: str, items: list, keep: int = 24) -> list:
    roster = load_roster()
    if parent_slug not in roster.get("agents", {}):
        return []
    hs = _union_helpers(roster["agents"][parent_slug].get("helpers"), items, keep=keep)
    roster["agents"][parent_slug]["helpers"] = hs
    save_roster(roster)
    return hs


def update_helper(parent_slug: str, helper_slug: str, **fields) -> None:
    roster = load_roster()
    hs = (roster.get("agents") or {}).get(parent_slug, {}).get("helpers") or []
    for h in hs:
        if h.get("slug") == helper_slug:
            h.update({k: v for k, v in fields.items() if v is not None})
            break
    save_roster(roster)


HOLD_SECS = 12


def load_queue(slug: str) -> list:
    return _read_json(agent_dir(slug) / "queue.json", [])


def save_queue(slug: str, items: list) -> None:
    agent_dir(slug).joinpath("queue.json").write_text(
        json.dumps(items[-40:], ensure_ascii=False, indent=2), encoding="utf-8"
    )


def drop_huddle_queue(slug: str) -> int:
    """Drop queued huddles so a STOP does not play after a stale instruction."""
    items = load_queue(slug)
    keep = []
    dropped = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "")
        if re.match(r"^\[(?:Huddle from|Overleg van)\s", text, re.I):
            dropped += 1
            continue
        keep.append(item)
    if dropped:
        save_queue(slug, keep)
    return dropped


def enqueue(
    slug: str,
    text: str,
    source: str = "user",
    hold: bool = False,
    choice: str = "",
    chosen_by: str = "",
) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("lege tekst")
    items = mature_queue(slug)
    kind = str(choice or "").strip().lower()
    if kind == "agent":
        kind = "helper"
    who = str(chosen_by or "").strip().lower()
    item = {
        "id": str(uuid.uuid4())[:8],
        "text": text[:2000],
        "at": _now(),
        "source": source or "user",
        "status": "hold" if hold else "queued",
    }
    if kind:
        item["choice"] = kind
    if who:
        item["chosen_by"] = who
    if hold:
        item["hold_until"] = time.time() + HOLD_SECS
    items.append(item)
    save_queue(slug, items)
    add_event(slug, "queue", text[:80])
    return item


def mature_queue(slug: str) -> list:
    now = time.time()
    items = load_queue(slug)
    changed = False
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("status") in (None, "", "hold"):
            until = item.get("hold_until")
            if until is None or now >= float(until):
                if item.get("status") == "hold" or until is not None:
                    item["status"] = "queued"
                    item.pop("hold_until", None)
                    changed = True
            elif not item.get("status"):
                item["status"] = "hold"
                changed = True
        elif not item.get("status"):
            item["status"] = "queued"
            changed = True
    if changed:
        save_queue(slug, items)
    return items


def take_queue(slug: str, qid: str) -> dict | None:
    qid = str(qid or "")
    items = mature_queue(slug)
    keep, found = [], None
    for x in items:
        if isinstance(x, dict) and str(x.get("id") or "") == qid and found is None:
            found = x
        else:
            keep.append(x)
    if found is not None:
        save_queue(slug, keep)
    return found


def remove_queue(slug: str, qid: str) -> None:
    take_queue(slug, qid)


def assign_queue(slug: str, qid: str, action: str) -> dict | None:
    action = (action or "").strip().lower()
    if action == "queue":
        items = mature_queue(slug)
        for item in items:
            if isinstance(item, dict) and str(item.get("id") or "") == str(qid):
                item["status"] = "queued"
                item.pop("hold_until", None)
                save_queue(slug, items)
                return item
        return None
    if action in {"helper", "steer", "agent"}:
        return take_queue(slug, qid)
    return take_queue(slug, qid)


def public_queue(slug: str) -> list[dict]:
    out = []
    now = time.time()
    for i, item in enumerate(mature_queue(slug)):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        status = str(item.get("status") or "queued")
        until = item.get("hold_until")
        hold_left = 0
        if status == "hold" and until:
            hold_left = max(0, int(float(until) - now))
        choice = str(item.get("choice") or "").strip().lower()
        if choice == "agent":
            choice = "helper"
        out.append(
            {
                "id": item.get("id") or f"q{i}",
                "text": text[:200],
                "at": item.get("at") or "",
                "source": item.get("source") or "user",
                "status": status,
                "hold_left": hold_left,
                "choice": choice,
                "chosen_by": str(item.get("chosen_by") or ""),
            }
        )
    return out


def set_last_route(slug: str, route: dict | None) -> dict:
    """Remember Swarm's last this-chat / queue / extra-agent pick."""
    if not slug:
        return {}
    roster = load_roster()
    agents = roster.get("agents") or {}
    if slug not in agents:
        return {}
    if not route:
        agents[slug].pop("last_route", None)
        save_roster(roster)
        return {}
    kind = str(route.get("choice") or "").strip().lower()
    if kind == "agent":
        kind = "helper"
    if kind not in {"steer", "helper", "queue"}:
        agents[slug].pop("last_route", None)
        save_roster(roster)
        return {}
    try:
        at = float(route.get("at") or time.time())
    except (TypeError, ValueError):
        at = time.time()
    clean = {
        "choice": kind,
        "text": str(route.get("text") or "").strip()[:200],
        "at": at,
        "chosen_by": str(route.get("chosen_by") or "swarm").strip().lower() or "swarm",
        "qid": str(route.get("qid") or ""),
        "hid": str(route.get("hid") or ""),
    }
    agents[slug]["last_route"] = clean
    save_roster(roster)
    return clean


LOOP_CMD = re.compile(r"^/loop(?:\s+(\d+)\s*([smhd]))?\s+(.+)$", re.I | re.S)
EVERY_RE = re.compile(
    r"\b(?:every|elk|elke)\s+(?:(\d+)\s*)?(minutes?|minuten|mins?|hours?|uren|uur|days?|dagen)\b",
    re.I,
)


def interval_from_fields(n, unit: str) -> str:
    try:
        num = int(n or 0)
    except (TypeError, ValueError):
        num = 0
    u = str(unit or "m").strip().lower()[:1]
    if u == "u":
        u = "h"
    if u not in {"s", "m", "h", "d"}:
        u = "m"
    return f"{num}{u}"


def parse_interval(raw) -> tuple[str, int] | None:
    s = str(raw or "").strip().lower()
    m = re.match(r"^(\d+)\s*([smhd])$", s)
    unit = ""
    n = 0
    if m:
        n, unit = int(m.group(1)), m.group(2)
    else:
        m = re.match(
            r"^(\d+)\s*(min|mins|minuut|minuten|uur|uren|hour|hours|dag|dagen|day|days)$",
            s,
        )
        if not m:
            return None
        n = int(m.group(1))
        word = m.group(2)
        if word.startswith("min"):
            unit = "m"
        elif word.startswith("u") or word.startswith("h"):
            unit = "h"
        else:
            unit = "d"
    if n <= 0:
        return None
    if unit == "s":
        mins = max(1, n // 60)
        return f"{mins}m", mins
    if unit == "m":
        return f"{n}m", n
    if unit == "h":
        return f"{n}h", n * 60
    return f"{n}d", n * 1440


def interval_label(mins: int) -> str:
    mins = max(1, int(mins or 1))
    if mins == 1:
        return "every minute"
    if mins < 60:
        return f"every {mins} min"
    if mins % 1440 == 0:
        days = mins // 1440
        return "every day" if days == 1 else f"every {days} days"
    if mins % 60 == 0:
        hours = mins // 60
        return "every hour" if hours == 1 else f"every {hours} hours"
    hours, rem = divmod(mins, 60)
    return f"every {hours}h {rem}m"


def loop_name_from_prompt(prompt: str) -> str:
    p = EVERY_RE.sub("", prompt or "")
    p = re.sub(r"\s+", " ", p).strip(" .,-")
    words = p.split()
    return " ".join(words[:4])[:36] or "Loop"


def parse_loop_command(text: str) -> dict | None:
    t = (text or "").strip()
    m = LOOP_CMD.match(t)
    if not m:
        return None
    n, u, prompt = m.group(1), m.group(2), (m.group(3) or "").strip()
    if not prompt:
        return None
    parsed = None
    if n and u:
        parsed = parse_interval(f"{n}{u}")
    if not parsed:
        em = EVERY_RE.search(prompt)
        if em:
            num = int(em.group(1) or 1)
            word = em.group(2).lower()
            if word.startswith("m"):
                parsed = parse_interval(f"{num}m")
            elif word.startswith("h") or word.startswith("u"):
                parsed = parse_interval(f"{num}h")
            else:
                parsed = parse_interval(f"{num}d")
    if not parsed:
        return None
    interval, every_min = parsed
    return {
        "name": loop_name_from_prompt(prompt),
        "interval": interval,
        "every_min": every_min,
        "prompt": prompt,
    }


def load_loops(slug: str) -> list:
    items = _read_json(agent_dir(slug) / "loops.json", [])
    return [x for x in items if isinstance(x, dict)]


def save_loops(slug: str, loops: list) -> None:
    agent_dir(slug).joinpath("loops.json").write_text(
        json.dumps(loops[:20], ensure_ascii=False, indent=2), encoding="utf-8"
    )


def public_loops(slug: str) -> list[dict]:
    out = []
    for item in load_loops(slug):
        mins = int(item.get("every_min") or 0)
        if mins <= 0:
            parsed = parse_interval(item.get("interval") or "")
            if not parsed:
                continue
            mins = parsed[1]
        out.append(
            {
                "id": item.get("id") or "",
                "name": item.get("name") or loop_name_from_prompt(item.get("prompt") or ""),
                "interval": item.get("interval") or "",
                "every": interval_label(mins),
                "every_min": mins,
                "prompt": (item.get("prompt") or "")[:160],
                "source": item.get("source") or "swarm",
            }
        )
    return out


def upsert_loop(
    slug: str,
    name: str,
    interval: str,
    prompt: str,
    source: str = "swarm",
    loop_id: str | None = None,
) -> dict:
    parsed = parse_interval(interval)
    if not parsed:
        raise ValueError("ongeldig interval")
    canon, mins = parsed
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("no loop text")
    name = (name or "").strip()[:40] or loop_name_from_prompt(prompt)
    loops = load_loops(slug)
    for item in loops:
        if loop_id and item.get("id") == loop_id:
            item["name"] = name
            item["interval"] = canon
            item["every_min"] = mins
            item["prompt"] = prompt[:800]
            save_loops(slug, loops)
            return item
        if (item.get("prompt") or "").strip() == prompt and int(item.get("every_min") or 0) == mins:
            if not item.get("name"):
                item["name"] = name
            save_loops(slug, loops)
            return item
    item = {
        "id": str(uuid.uuid4())[:8],
        "name": name,
        "interval": canon,
        "every_min": mins,
        "prompt": prompt[:800],
        "source": source if source in {"grok", "swarm"} else "swarm",
        "created": _now(),
        "next": time.time() + mins * 60,
        "last": None,
    }
    loops.append(item)
    save_loops(slug, loops)
    add_event(slug, "loop", f"{name} · {interval_label(mins)}")
    return item


def remove_loop(slug: str, loop_id: str) -> None:
    save_loops(slug, [item for item in load_loops(slug) if item.get("id") != loop_id])


def due_swarm_loops() -> list[tuple[str, dict]]:
    now = time.time()
    out: list[tuple[str, dict]] = []
    roster = load_roster()
    for slug, meta in (roster.get("agents") or {}).items():
        if slug.startswith("h--") or meta.get("helper"):
            continue
        for item in load_loops(slug):
            if (item.get("source") or "swarm") != "swarm":
                continue
            try:
                nxt = float(item.get("next") or 0)
            except (TypeError, ValueError):
                nxt = 0
            if nxt and nxt <= now:
                out.append((slug, item))
    return out


def mark_loop_fired(slug: str, loop_id: str) -> None:
    loops = load_loops(slug)
    now = time.time()
    for item in loops:
        if item.get("id") == loop_id:
            mins = max(1, int(item.get("every_min") or 5))
            item["last"] = _now()
            item["next"] = now + mins * 60
    save_loops(slug, loops)


def ingest_loop_texts(slug: str, texts: list) -> None:
    if not slug:
        return
    for text in texts or []:
        parsed = parse_loop_command(str(text or ""))
        if parsed:
            try:
                upsert_loop(
                    slug,
                    parsed["name"],
                    parsed["interval"],
                    parsed["prompt"],
                    source="grok",
                )
            except Exception:
                pass

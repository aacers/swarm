#!/usr/bin/env python3
"""Deliver inter-bot overleg via tmux. Only the boss may broadcast."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import roster as rosterlib
from server import STATE_DIR, dispatch_text

BUS = STATE_DIR / "bus"
OUTBOX = BUS / "outbox.jsonl"
OFFSET = BUS / "outbox.offset"
AGENTS = STATE_DIR / "agents.json"


def write_agents() -> list[dict]:
    rost = rosterlib.load_roster()
    agents = []
    for slug, meta in (rost.get("agents") or {}).items():
        if slug.startswith("h--") or (meta or {}).get("helper"):
            continue
        if rosterlib.is_forgotten(slug=slug, tmux=str((meta or {}).get("tmux") or "")):
            continue
        lab = str((meta or {}).get("label") or slug)
        agents.append(
            {
                "id": meta.get("window_id"),
                "slug": slug,
                "label": lab,
                "title": lab,
                "role": "ceo" if slug == rost.get("ceo") else "worker",
                "tmux": meta.get("tmux") or "",
            }
        )
    BUS.mkdir(parents=True, exist_ok=True)
    AGENTS.write_text(json.dumps(agents, ensure_ascii=False, indent=2), encoding="utf-8")
    return agents


def win_for(slug: str, meta: dict) -> dict:
    return {
        "slug": slug,
        "id": meta.get("window_id"),
        "tmux": meta.get("tmux") or "",
        "session_id": meta.get("session_id") or "",
        "title": meta.get("title") or meta.get("label") or slug,
    }


def plan_targets(msg: dict, rost: dict) -> tuple[list[tuple[str, dict]], str]:
    """Who should receive this overleg. Empty list + reason if blocked."""
    to = str(msg.get("to") or "").strip()
    src = str(msg.get("from") or "").strip()
    if not to:
        return [], "no target"
    sender = rosterlib.find_agent_by_name(src, rost) if src else None
    sender_slug = sender[0] if sender else ""
    ceo = rosterlib.sender_is_ceo(src, rost)
    agents = rost.get("agents") or {}

    def skip(slug: str, meta: dict) -> bool:
        if slug.startswith("h--") or (meta or {}).get("helper"):
            return True
        if sender_slug and slug == sender_slug:
            return True
        return rosterlib.is_forgotten(slug=slug, tmux=str((meta or {}).get("tmux") or ""))

    if to.lower() == "all":
        if not ceo:
            return [], "only the boss may message everyone"
        out = [(s, m) for s, m in agents.items() if not skip(s, m)]
        return out, ""

    hit = rosterlib.find_agent_by_name(to, rost)
    if not hit:
        return [], "unknown bot"
    slug, meta = hit
    if skip(slug, meta):
        return [], "doel is jijzelf of verwijderd"
    return [(slug, meta)], ""


def deliver(msg: dict) -> None:
    text = str(msg.get("text") or "").strip()
    if not text:
        return
    rost = rosterlib.load_roster()
    targets, err = plan_targets(msg, rost)
    src = str(msg.get("from") or "agent")
    if err or not targets:
        print("overleg skip", src, "->", msg.get("to"), err or "no target", flush=True)
        return
    payload = f"[Huddle from {src}]: {text}"
    for slug, meta in targets:
        try:
            dispatch_text(win_for(slug, meta), payload, True)
            print("overleg", src, "->", meta.get("label") or slug, flush=True)
        except Exception as e:
            print("deliver fail", slug, e, flush=True)


def read_offset() -> int:
    try:
        return int(OFFSET.read_text().strip() or "0")
    except Exception:
        return 0


def main() -> None:
    BUS.mkdir(parents=True, exist_ok=True)
    OUTBOX.touch(exist_ok=True)
    print("overleg-watch ready", OUTBOX, flush=True)
    last_roles = 0.0
    while True:
        try:
            write_agents()
            now = time.time()
            if now - last_roles > 90:
                rosterlib.ensure_team_roles()
                last_roles = now
            data = OUTBOX.read_bytes()
            off = read_offset()
            if off > len(data):
                off = 0
            chunk = data[off:]
            if chunk:
                text = chunk.decode("utf-8", errors="replace")
                used = 0
                for line in text.splitlines(keepends=True):
                    used += len(line.encode("utf-8"))
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if isinstance(msg, dict):
                        deliver(msg)
                OFFSET.write_text(str(off + used), encoding="utf-8")
        except Exception as e:
            print("watch error", e, flush=True)
        time.sleep(1.2)


if __name__ == "__main__":
    main()

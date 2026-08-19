#!/usr/bin/env python3
"""In-app browser. Headless Chrome per bot — no OS window beside Swarm."""

from __future__ import annotations

import base64
import json
import os
import queue
import shutil
import sys
import re
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from browse_ids import DEFAULT_BOT, normalize_bot, profile_dir  # noqa: E402

_FOCUS_JS = """() => {
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) return {editable: false};
  const t = (el.tagName || '').toLowerCase();
  const typ = (el.getAttribute('type') || '').toLowerCase();
  const skip = ['button','submit','checkbox','radio','file','hidden','image','reset','range','color'];
  const editable = !!(el.isContentEditable
    || t === 'textarea'
    || t === 'select'
    || (t === 'input' && skip.indexOf(typ) < 0)
    || el.closest('[contenteditable="true"],[contenteditable=""]'));
  return {editable, tag: t, type: typ};
}"""


def _focus_info(page) -> dict:
    try:
        return page.evaluate(_FOCUS_JS) or {}
    except Exception:
        return {}


_PAGE_JS = """(n) => {
  const vw = window.innerWidth || 1, vh = window.innerHeight || 1;
  const sel = 'a[href],button,input,textarea,select,summary,[role="button"],[role="link"],[role="tab"],[role="menuitem"],[contenteditable="true"]';
  const list = [];
  const seen = new Set();
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    if (r.bottom < 0 || r.right < 0 || r.top > vh || r.left > vw) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity) === 0) continue;
    const typ = (el.getAttribute('type') || '').toLowerCase();
    if (typ === 'hidden') continue;
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || '')
      .replace(/\\s+/g, ' ').trim().slice(0, 80);
    const tag = el.tagName.toLowerCase();
    const key = tag + '|' + text + '|' + Math.round(r.x) + '|' + Math.round(r.y);
    if (seen.has(key)) continue;
    seen.add(key);
    list.push({
      el,
      tag,
      type: typ,
      text,
      href: (el.href || '').slice(0, 180),
      nx: (r.x + r.width / 2) / vw,
      ny: (r.y + r.height / 2) / vh,
    });
    if (list.length >= 60) break;
  }
  let tapped = null;
  if (n) {
    const hit = list[n - 1];
    if (!hit) return { ok: false, error: 'no control ' + n };
    try { hit.el.focus(); } catch (e) {}
    try { hit.el.click(); } catch (e) {}
    tapped = { n, tag: hit.tag, text: hit.text, href: hit.href };
  }
  const controls = list.map((c, i) => ({
    n: i + 1, tag: c.tag, type: c.type, text: c.text, href: c.href, nx: c.nx, ny: c.ny,
  }));
  const text = (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').trim().slice(0, 12000);
  const links = controls.filter(c => c.href).slice(0, 40).map(c => ({ text: c.text, href: c.href }));
  return { ok: true, text, controls, links, tapped };
}"""


def _page_info(page, tap: int = 0) -> dict:
    try:
        info = page.evaluate(_PAGE_JS, int(tap or 0)) or {}
    except Exception:
        info = {}
    out = {
        "ok": bool(info.get("ok", True)),
        "text": info.get("text") or "",
        "controls": info.get("controls") or [],
        "links": info.get("links") or [],
    }
    if info.get("error"):
        out["ok"] = False
        out["error"] = info.get("error")
    if info.get("tapped"):
        out["tapped"] = info["tapped"]
    try:
        out["url"] = page.url
        out["title"] = page.title()
    except Exception:
        pass
    return out


def _selected_text(page) -> str:
    try:
        return str(page.evaluate("() => window.getSelection ? window.getSelection().toString() : ''") or "")
    except Exception:
        return ""


def _clean_cookies(raw) -> list:
    out = []
    for c in raw or []:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        d = {
            k: c[k]
            for k in ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
            if k in c
        }
        if d.get("sameSite") not in {"Strict", "Lax", "None"}:
            d["sameSite"] = "Lax"
        if d.get("expires") in (-1, None, 0):
            d.pop("expires", None)
        out.append(d)
    return out


def _inherit_shared_auth(page, slug: str) -> int:
    if slug == DEFAULT_BOT:
        return 0
    try:
        url = page.url or ""
    except Exception:
        url = ""
    if url and url != "about:blank" and "accounts.google.com" not in url:
        return 0
    src = _existing(DEFAULT_BOT)
    if not src or src.slug == slug:
        return 0
    try:
        got = src.call("state", timeout=2)
        cookies = _clean_cookies((got.get("out") or {}).get("cookies") or [])
        if not cookies:
            return 0
        page.context.add_cookies(cookies)
        return len(cookies)
    except Exception:
        return 0


# Copying Service Worker / Sessions / Cache / IndexedDB is minutes. Login is a few files.
_SEED_FILES = (
    "Cookies",
    "Cookies-journal",
    "Login Data",
    "Login Data-journal",
    "Login Data For Account",
    "Login Data For Account-journal",
    "Web Data",
    "Web Data-journal",
    "Account Web Data",
    "Account Web Data-journal",
    "Preferences",
    "Secure Preferences",
    "Network/Cookies",
    "Network/Cookies-journal",
    "Network/TransportSecurity",
)


def seed_profile(slug: str, force: bool = False) -> None:
    """Copy Google/Play cookies onto a new bot — never the whole Chrome tree."""
    if slug == DEFAULT_BOT:
        return
    src = profile_dir(DEFAULT_BOT)
    dest = profile_dir(slug)
    marker = dest / ".seeded-from-shared"
    if marker.exists() and not force:
        return
    src_def = src / "Default"
    if not src_def.exists():
        return
    dest.mkdir(parents=True, exist_ok=True)
    dest_def = dest / "Default"
    dest_def.mkdir(parents=True, exist_ok=True)
    for rel in _SEED_FILES:
        sp, dp = src_def / rel, dest_def / rel
        if not sp.is_file():
            continue
        try:
            dp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sp, dp)
        except Exception:
            pass
    for name in ("Local State", "First Run"):
        sp = src / name
        if sp.is_file():
            try:
                shutil.copy2(sp, dest / name)
            except Exception:
                pass
    try:
        marker.touch()
    except Exception:
        pass


PROFILE = Path.home() / ".grok" / "browser" / "profile"
DOWNLOADS = Path.home() / ".grok" / "browser" / "downloads"
HOST, PORT = "127.0.0.1", 8791
MAX_LIVE = 10
IDLE_SEC = 40 * 60
SHOT_TTL = 0.4

WATCH_CMDS = {"open", "click", "type", "key", "back", "sel", "tap", "read", "page", "scroll", "eval", "files"}
# read/eval are text-first — skip the JPEG so bots don't wait on screenshots.
SNAP_CMDS = {"open", "click", "type", "key", "back", "sel", "scroll", "files", "mouse"}


_pool_lock = threading.Lock()
_pool: dict[str, "BotBrowser"] = {}


class BotBrowser:
    def __init__(self, slug: str):
        self.slug = normalize_bot(slug)
        self.jobs: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = 0
        self.lock = threading.Lock()
        self.state: dict = {
            "ok": True,
            "bot": self.slug,
            "url": "about:blank",
            "title": "",
            "busy": False,
            "cmd": "",
            "at": 0.0,
            "shot_n": 0,
            "shot_at": 0.0,
            "want_shot": False,
        }
        self.last_shot = b""
        self.last_shot_at = 0.0
        self.thread = threading.Thread(target=self.worker, name=f"browse-{self.slug}", daemon=True)
        self.thread.start()
        threading.Thread(target=self.snapper, name=f"snap-{self.slug}", daemon=True).start()

    def health(self) -> dict:
        with self.lock:
            return dict(self.state)

    def shot(self) -> bytes:
        with self.lock:
            return self.last_shot

    def mark(self, cmd: str, busy: bool, url: str | None = None, title: str | None = None) -> None:
        with self.lock:
            self.state["cmd"] = cmd
            self.state["busy"] = busy
            self.state["at"] = time.time()
            self.state["bot"] = self.slug
            if url is not None:
                self.state["url"] = url
            if title is not None:
                self.state["title"] = title

    def store_shot(self, data: bytes, url: str = "", title: str = "") -> None:
        with self.lock:
            if data:
                self.last_shot = data
                self.last_shot_at = time.time()
                self.state["shot_n"] = int(self.state.get("shot_n") or 0) + 1
                self.state["shot_at"] = self.last_shot_at
            if url:
                self.state["url"] = url
            if title:
                self.state["title"] = title
            self.state["ok"] = True

    def request_shot(self) -> None:
        with self.lock:
            self.state["want_shot"] = True

    def call(self, cmd: str, args: dict | None = None, timeout: float = 30):
        ev = threading.Event()
        box: dict = {}
        pri = 1 if cmd in {"health", "snap"} else 0
        with self.lock:
            self._seq = int(self._seq) + 1
            seq = self._seq
        self.jobs.put((pri, seq, (cmd, args or {}, ev, box)))
        if not ev.wait(timeout):
            raise TimeoutError("browser timeout")
        if "error" in box:
            raise RuntimeError(box["error"])
        return box

    def worker(self) -> None:
        profile = profile_dir(self.slug)
        profile.mkdir(parents=True, exist_ok=True)
        seed_profile(self.slug)
        pw = sync_playwright().start()
        ctx = None
        page = None
        cdp = None
        downloads = DOWNLOADS / self.slug
        downloads.mkdir(parents=True, exist_ok=True)

        def live():
            nonlocal ctx, page, cdp
            try:
                if page is not None and not page.is_closed():
                    _ = page.url
                    return page
            except Exception:
                page = None
                cdp = None
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass
                ctx = None
            cdp = None
            launch = {
                "user_data_dir": str(profile),
                "headless": True,
                "accept_downloads": True,
                "downloads_path": str(downloads),
                "viewport": {"width": 1280, "height": 860},
                "device_scale_factor": 1,
                "locale": "nl-NL",
                "timezone_id": "Europe/Amsterdam",
                "color_scheme": "light",
                "ignore_default_args": ["--enable-automation"],
                "args": [
                    "--headless=new",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-sync",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-extensions",
                    "--disable-component-update",
                    "--disable-background-networking",
                    "--disable-breakpad",
                    "--disable-hang-monitor",
                    "--disable-features=Translate,MediaRouter,DialMediaRouteProvider,PaintHolding,TabRestore",
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-ipc-flooding-protection",
                    "--renderer-process-limit=4",
                    "--window-size=1280,860",
                    "--hide-crash-restore-bubble",
                    "--disable-session-crashed-bubble",
                ],
            }
            try:
                ctx = pw.chromium.launch_persistent_context(channel="chrome", **launch)
            except Exception:
                ctx = pw.chromium.launch_persistent_context(**launch)
            try:
                ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                )
            except Exception:
                pass
            page = ctx.pages[-1] if ctx.pages else ctx.new_page()
            try:
                page.set_default_timeout(2500)
                page.set_default_navigation_timeout(8000)
            except Exception:
                pass
            _inherit_shared_auth(page, self.slug)
            return page

        def snap(p, force: bool = False) -> bytes:
            cached = self.shot()
            if not force and cached and (time.time() - self.last_shot_at) < SHOT_TTL:
                return cached
            try:
                data = p.screenshot(
                    type="jpeg",
                    quality=32,
                    full_page=False,
                    animations="disabled",
                    caret="initial",
                    scale="css",
                    timeout=800,
                )
                if data:
                    url = ""
                    try:
                        url = p.url
                    except Exception:
                        pass
                    self.store_shot(data, url, "")
                return data or cached
            except Exception:
                return cached

        while True:
            job = self.jobs.get()
            if job is None or (isinstance(job, tuple) and job[-1] is None):
                break
            cmd, args, ev, box = job[2] if isinstance(job, tuple) and len(job) == 3 else job
            try:
                if cmd in WATCH_CMDS:
                    self.mark(cmd, True)
                gated = False
                if cmd == "open" and not args.get("chrome") and not args.get("force"):
                    url = args.get("url") or "about:blank"
                    if not str(url).startswith(("http://", "https://", "about:")):
                        url = "https://" + url
                    from page_fetch import gate

                    g = gate(str(url), self.slug)
                    if g.get("action") != "chrome":
                        out = dict(g.get("page") or {})
                        out["ok"] = True
                        out["action"] = g.get("action")
                        out["bot"] = self.slug
                        box["out"] = out
                        gated = True
                if gated:
                    pass
                elif cmd in {"health", "snap"} and page is None:
                    box["out"] = self.health()
                    if cmd == "snap":
                        box["bytes"] = self.shot()
                else:
                    p = live()
                    if cmd in {"health", "snap"}:
                        box["out"] = self.health()
                        if cmd == "snap":
                            box["bytes"] = snap(p)
                    elif cmd == "shot":
                        box["bytes"] = snap(p) or self.shot()
                        box["out"] = self.health()
                    elif cmd == "open":
                        url = args.get("url") or "about:blank"
                        if not str(url).startswith(("http://", "https://", "about:")):
                            url = "https://" + url
                        self.mark("open", True, url=str(url))
                        try:
                            p.goto(str(url), wait_until="commit", timeout=8000)
                        except Exception as nav_err:
                            low = str(nav_err).lower()
                            if any(
                                s in low
                                for s in ("target closed", "browser has been closed", "crashed")
                            ):
                                raise
                        self.request_shot()
                        if args.get("page"):
                            try:
                                p.wait_for_load_state("domcontentloaded", timeout=2000)
                            except Exception:
                                pass
                            info = _page_info(p)
                            info["bot"] = self.slug
                            box["out"] = info
                        else:
                            title = ""
                            try:
                                title = p.title()
                            except Exception:
                                pass
                            box["out"] = {
                                "ok": True,
                                "url": p.url,
                                "title": title,
                                "bot": self.slug,
                            }
                        try:
                            from page_fetch import save_cookies

                            save_cookies(
                                self.slug,
                                (p.context.storage_state() or {}).get("cookies") or [],
                            )
                        except Exception:
                            pass
                    elif cmd == "click":
                        nx, ny = float(args.get("nx", 0.5)), float(args.get("ny", 0.5))
                        vp = p.viewport_size or {"width": 1280, "height": 800}
                        p.mouse.click(nx * vp["width"], ny * vp["height"], delay=0)
                        focus = _focus_info(p)
                        self.request_shot()
                        if args.get("page"):
                            info = _page_info(p)
                            info["bot"] = self.slug
                            info["editable"] = bool(focus.get("editable"))
                            info["tag"] = focus.get("tag") or ""
                            box["out"] = info
                        else:
                            box["out"] = {
                                "ok": True,
                                "url": p.url,
                                "bot": self.slug,
                                "editable": bool(focus.get("editable")),
                                "tag": focus.get("tag") or "",
                            }
                    elif cmd == "mouse":
                        action = str(args.get("action") or "click")
                        nx, ny = float(args.get("nx", 0.5)), float(args.get("ny", 0.5))
                        vp = p.viewport_size or {"width": 1280, "height": 800}
                        x, y = nx * vp["width"], ny * vp["height"]
                        selected = ""
                        if action == "down":
                            p.mouse.move(x, y)
                            p.mouse.down()
                        elif action == "move":
                            p.mouse.move(x, y)
                        elif action in {"dbl", "dblclick"}:
                            p.mouse.dblclick(x, y)
                            selected = _selected_text(p)
                            self.request_shot()
                        elif action == "up":
                            p.mouse.move(x, y)
                            p.mouse.up()
                            selected = _selected_text(p)
                            self.request_shot()
                        else:
                            p.mouse.click(x, y, delay=0)
                            self.request_shot()
                        out = {"ok": True, "url": p.url, "bot": self.slug}
                        if selected:
                            out["selected"] = selected
                        box["out"] = out
                    elif cmd == "type":
                        text = str(args.get("text") or "")
                        if text:
                            p.keyboard.insert_text(text)
                        if args.get("submit"):
                            p.keyboard.press("Enter")
                        self.request_shot()
                        box["out"] = {"ok": True, "url": p.url, "bot": self.slug}
                    elif cmd == "key":
                        key = str(args.get("key") or "Enter")
                        if key == "Backspace" and args.get("n"):
                            for _ in range(min(40, int(args["n"]))):
                                p.keyboard.press("Backspace")
                        else:
                            p.keyboard.press(key)
                        self.request_shot()
                        box["out"] = {"ok": True, "bot": self.slug}
                    elif cmd == "scroll":
                        nx, ny = float(args.get("nx", 0.5)), float(args.get("ny", 0.5))
                        dy = float(args.get("dy") or 0)
                        dx = float(args.get("dx") or 0)
                        vp = p.viewport_size or {"width": 1280, "height": 800}
                        p.mouse.move(nx * vp["width"], ny * vp["height"])
                        p.mouse.wheel(dx, dy)
                        self.request_shot()
                        box["out"] = {"ok": True, "bot": self.slug}
                    elif cmd == "back":
                        try:
                            p.go_back(wait_until="commit", timeout=5000)
                        except Exception:
                            pass
                        self.request_shot()
                        title = ""
                        try:
                            title = p.title()
                        except Exception:
                            pass
                        box["out"] = {"ok": True, "url": p.url, "title": title, "bot": self.slug}
                    elif cmd in {"front", "wake"}:
                        title = ""
                        try:
                            title = p.title()
                        except Exception:
                            pass
                        box["out"] = {"ok": True, "url": p.url, "title": title, "bot": self.slug}
                    elif cmd in {"read", "page"}:
                        info = _page_info(p)
                        info["bot"] = self.slug
                        box["out"] = info
                        self.mark("read", False, url=p.url, title=info.get("title") or "")
                    elif cmd == "tap":
                        n = int(args.get("n") or 0)
                        if n < 1:
                            raise RuntimeError("tap needs n>=1")
                        info = _page_info(p, tap=n)
                        if not info.get("ok"):
                            raise RuntimeError(info.get("error") or "tap failed")
                        href = str(((info.get("tapped") or {}).get("href") or ""))
                        if href.startswith("http"):
                            try:
                                p.wait_for_load_state("domcontentloaded", timeout=2000)
                            except Exception:
                                pass
                            info = _page_info(p)
                            info["tapped"] = {"n": n, "href": href}
                        self.request_shot()
                        info["bot"] = self.slug
                        box["out"] = info
                    elif cmd == "sel":
                        sel = str(args.get("selector") or "").strip()
                        if not sel:
                            raise RuntimeError("no selector")
                        if sel.isdigit():
                            info = _page_info(p, tap=int(sel))
                            if not info.get("ok"):
                                raise RuntimeError(info.get("error") or "tap failed")
                        else:
                            p.click(sel, timeout=2500, no_wait_after=True)
                            info = _page_info(p)
                        self.request_shot()
                        info["bot"] = self.slug
                        box["out"] = info
                    elif cmd == "state":
                        incoming = args.get("cookies")
                        if incoming is not None:
                            cookies = _clean_cookies(incoming)
                            if cookies:
                                p.context.add_cookies(cookies)
                            box["out"] = {"ok": True, "n": len(cookies), "bot": self.slug}
                        else:
                            st = p.context.storage_state()
                            box["out"] = {
                                "ok": True,
                                "cookies": st.get("cookies") or [],
                                "bot": self.slug,
                                "url": p.url,
                            }
                    elif cmd == "eval":
                        js = str(args.get("js") or "")
                        if not js:
                            raise RuntimeError("no js")
                        result = p.evaluate(js)
                        box["out"] = {
                            "ok": True,
                            "url": p.url,
                            "title": p.title(),
                            "result": result,
                            "bot": self.slug,
                        }
                    elif cmd == "files":
                        sel = str(args.get("selector") or "input[type=file]").strip()
                        raw = args.get("paths") or args.get("path") or []
                        if isinstance(raw, str):
                            paths = [raw]
                        else:
                            paths = [str(x) for x in raw]
                        paths = [x for x in paths if x]
                        if not paths:
                            raise RuntimeError("no files")
                        for fp in paths:
                            if not Path(fp).is_file():
                                raise RuntimeError(f"missing file: {fp}")
                        loc = p.locator(sel).first
                        loc.set_input_files(paths, timeout=4000)
                        self.request_shot()
                        box["out"] = {
                            "ok": True,
                            "n": len(paths),
                            "url": p.url,
                            "title": p.title(),
                            "bot": self.slug,
                        }
                    else:
                        box["out"] = {"ok": False, "error": "unknown"}
            except Exception as e:
                box["error"] = str(e)
                low = str(e).lower()
                if any(
                    s in low
                    for s in (
                        "target closed",
                        "target page, context or browser has been closed",
                        "browser has been closed",
                        "has been closed",
                        "crashed",
                    )
                ):
                    page = None
                    cdp = None
                self.mark(cmd or "error", False)
            else:
                if cmd in WATCH_CMDS and cmd not in {"read"}:
                    self.mark(
                        cmd,
                        False,
                        url=(box.get("out") or {}).get("url"),
                        title=(box.get("out") or {}).get("title"),
                    )
            ev.set()

    def snapper(self) -> None:
        while True:
            time.sleep(0.4)
            st = self.health()
            now = time.time()
            age = now - float(st.get("at") or 0)
            shot_age = now - float(st.get("shot_at") or 0)
            want = bool(st.get("want_shot"))
            if not want and not st.get("busy") and age > 2.5:
                continue
            if shot_age < SHOT_TTL:
                continue
            if not self.jobs.empty():
                continue
            try:
                self.call("snap", timeout=1.5)
                with self.lock:
                    self.state["want_shot"] = False
            except Exception:
                pass


def cached_health(bot: str | None = None) -> dict:
    slug = normalize_bot(bot) if bot else ""
    with _pool_lock:
        live = {k: v.health() for k, v in _pool.items()}
    if slug and slug in live:
        out = dict(live[slug])
        out["bots"] = list(live)
        return out
    if slug:
        return {
            "ok": True,
            "bot": slug,
            "url": "about:blank",
            "title": "",
            "busy": False,
            "cmd": "",
            "at": 0.0,
            "shot_n": 0,
            "bots": list(live),
        }
    busy = [s for s in live.values() if s.get("busy")]
    pick = busy[0] if busy else (live.get(DEFAULT_BOT) or (next(iter(live.values())) if live else None))
    if not pick:
        return {
            "ok": True,
            "bot": DEFAULT_BOT,
            "url": "about:blank",
            "title": "",
            "busy": False,
            "cmd": "",
            "at": 0.0,
            "shot_n": 0,
            "bots": [],
        }
    out = dict(pick)
    out["bots"] = list(live)
    out["pages"] = {k: {"url": v.get("url") or "", "title": v.get("title") or ""} for k, v in live.items()}
    return out


def cached_shot(bot: str | None = None) -> bytes:
    bb = _existing(bot)
    return bb.shot() if bb else b""


def _existing(bot: str | None) -> BotBrowser | None:
    slug = normalize_bot(bot) if bot else ""
    with _pool_lock:
        if slug:
            return _pool.get(slug)
        if DEFAULT_BOT in _pool:
            return _pool[DEFAULT_BOT]
        return next(iter(_pool.values()), None)


def _evict_idle() -> None:
    now = time.time()
    with _pool_lock:
        if len(_pool) <= MAX_LIVE:
            return
        idle = sorted(
            (
                (k, float(v.health().get("at") or 0))
                for k, v in _pool.items()
                if k != DEFAULT_BOT and not v.health().get("busy")
            ),
            key=lambda kv: kv[1],
        )
        while len(_pool) > MAX_LIVE and idle:
            k, at = idle.pop(0)
            if now - at < 90:
                break
            dead = _pool.pop(k, None)
            if dead:
                dead.jobs.put((0, 0, None))


def get_bot(bot: str | None) -> BotBrowser:
    slug = normalize_bot(bot)
    with _pool_lock:
        hit = _pool.get(slug)
        if hit:
            return hit
    _evict_idle()
    with _pool_lock:
        hit = _pool.get(slug)
        if hit:
            return hit
        bb = BotBrowser(slug)
        _pool[slug] = bb
        return bb


def _xai_key() -> str:
    try:
        data = json.loads((Path.home() / ".grok" / "auth.json").read_text())
        first = next(iter(data.values()))
        if isinstance(first, dict):
            return str(first.get("key") or "")
    except Exception:
        pass
    env = Path.home() / ".grok" / "secrets.env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("XAI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"')
    return ""


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


FFMPEG = "/opt/homebrew/bin/ffmpeg"


def imac_audio_dev() -> str:
    r = subprocess.run(
        [FFMPEG, "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True,
        text=True,
        timeout=8,
    )
    audio = False
    first = "1"
    for line in (r.stderr or "").splitlines():
        if "audio devices" in line.lower():
            audio = True
            continue
        if not audio:
            continue
        m = re.search(r"\[(\d+)\]\s+(.*)", line)
        if not m:
            continue
        idx, name = m.group(1), m.group(2).strip()
        if first == "1":
            first = idx
        low = name.lower()
        if "imac" in low and "micro" in low:
            return idx
        if "macbook" in low and "micro" in low:
            return idx
    return first


def stt_bytes(data: bytes, filename: str = "talk.wav") -> str:
    if not data:
        return ""
    key = _xai_key()
    if not key:
        raise RuntimeError("no speech key")
    import uuid
    import urllib.request

    boundary = "----Heavy" + uuid.uuid4().hex
    fname = filename or "talk.wav"
    ctype = "audio/wav"
    if fname.endswith(".mp4") or fname.endswith(".m4a"):
        ctype = "audio/mp4"
    elif fname.endswith(".webm"):
        ctype = "audio/webm"
    elif fname.endswith(".mp3"):
        ctype = "audio/mpeg"
    parts = []

    def field(name: str, val: str) -> None:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode()
        )

    field("format", "true")
    field("language", "nl")
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
            "Authorization": f"Bearer {key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        payload = json.loads(resp.read().decode())
    return str(payload.get("text") or "").strip()


def listen_imac(seconds: float = 8.0) -> str:
    """Record until you stop talking (silence), max 8s."""
    seconds = max(3.0, min(8.0, float(seconds or 8)))
    dev = imac_audio_dev()
    try:
        subprocess.Popen(
            ["afplay", "/System/Library/Sounds/Pop.aiff"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    proc = None
    try:
        proc = subprocess.Popen(
            [
                FFMPEG,
                "-y",
                "-f",
                "avfoundation",
                "-i",
                f":{dev}",
                "-t",
                f"{seconds:.1f}",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-af",
                "silencedetect=noise=-32dB:d=0.55",
                "-c:a",
                "pcm_s16le",
                path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        heard = False
        deadline = time.time() + seconds + 2
        buf = ""
        while proc.poll() is None and time.time() < deadline:
            line = proc.stderr.readline() if proc.stderr else ""
            if not line:
                time.sleep(0.05)
                continue
            buf += line
            if "silence_end" in line:
                heard = True
            if heard and "silence_start" in line:
                time.sleep(0.12)
                proc.terminate()
                break
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        if not os.path.exists(path) or os.path.getsize(path) < 400:
            raise RuntimeError(
                "microphone gave no sound. Allow microphone for ffmpeg "
                "in System Settings → Privacy."
            )
        return stt_bytes(Path(path).read_bytes(), "talk.wav")
    finally:
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            os.unlink(path)
        except Exception:
            pass


def tts_mp3(text: str) -> tuple[bytes, str]:
    text = speakable(text)
    if not text:
        raise RuntimeError("no text")
    key = _xai_key()
    if key:
        import urllib.request

        body = json.dumps(
            {
                "text": text,
                "voice_id": "ara",
                "language": "auto",
                "speed": 1.02,
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
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = resp.read()
        if data:
            return data, "audio/mpeg"
    fd, path = tempfile.mkstemp(suffix=".aiff")
    os.close(fd)
    try:
        subprocess.run(
            ["say", "-v", "Xander", "-r", "185", "-o", path, text],
            capture_output=True,
            timeout=40,
        )
        data = Path(path).read_bytes()
        if not data:
            raise RuntimeError("tts leeg")
        return data, "audio/aiff"
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


def _bot_from(body: dict | None, query: str = "") -> str:
    from urllib.parse import parse_qs

    qs = parse_qs(query.split("?", 1)[-1] if query else "")
    if qs.get("bot"):
        return normalize_bot(qs["bot"][0])
    if body and body.get("bot"):
        return normalize_bot(str(body.get("bot")))
    return DEFAULT_BOT


def call(cmd: str, args: dict | None = None, timeout: float = 30, bot: str | None = None):
    args = dict(args or {})
    slug = normalize_bot(bot or args.pop("bot", None) or DEFAULT_BOT)
    return get_bot(slug).call(cmd, args, timeout)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except BrokenPipeError:
            pass

    def _bytes(self, data: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def do_GET(self):
        raw = self.path
        path = raw.split("?", 1)[0]
        bot = _bot_from({}, raw)
        try:
            if path == "/health":
                self._json(cached_health(bot if "bot=" in raw else None))
                return
            if path == "/fetch":
                from urllib.parse import parse_qs

                from page_fetch import fetch_page

                qs = parse_qs(raw.split("?", 1)[-1] if "?" in raw else "")
                url = (qs.get("url") or [""])[0]
                self._json(fetch_page(url, bot=bot))
                return
            if path in {"/read", "/page"}:
                self._json(call("read", bot=bot)["out"])
                return
            if path in {"/shot", "/frame"}:
                data = cached_shot(bot)
                if not data:
                    bb = _existing(bot)
                    if bb:
                        bb.request_shot()
                self._bytes(data or b"", "image/jpeg")
                return
            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    def do_POST(self):
        body = self._read()
        path = self.path.split("?", 1)[0].lstrip("/")
        bot = _bot_from(body, self.path)
        try:
            if path == "listen":
                raw_b64 = str(body.get("audio_b64") or "")
                if raw_b64:
                    blob = base64.b64decode(raw_b64)
                    text = stt_bytes(blob, str(body.get("name") or "talk.m4a"))
                else:
                    text = listen_imac(float(body.get("seconds") or 5))
                self._json({"ok": True, "text": text})
                return
            if path == "tts":
                audio, ctype = tts_mp3(str(body.get("text") or ""))
                if body.get("play"):
                    ext = ".mp3" if "mpeg" in ctype else ".aiff"
                    fd, pth = tempfile.mkstemp(suffix=ext)
                    os.write(fd, audio)
                    os.close(fd)
                    subprocess.Popen(
                        ["afplay", pth],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                self._bytes(audio, ctype)
                return
            if path == "authcopy":
                src = normalize_bot(body.get("from") or DEFAULT_BOT)
                cookies = _clean_cookies((call("state", bot=src)["out"] or {}).get("cookies") or [])
                call("state", {"cookies": cookies}, bot=bot)
                url = str(body.get("url") or "").strip()
                extra = {}
                if url:
                    extra = call("open", {"url": url}, bot=bot)["out"] or {}
                self._json({"ok": True, "n": len(cookies), "bot": bot, "from": src, **extra})
                return
            if path in {"open", "click", "type", "key", "back", "front", "wake", "read", "page", "sel", "tap", "scroll", "eval", "files", "mouse", "state"}:
                self._json(call(path, body, bot=bot)["out"])
                return
            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)


def main() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"browse-daemon {HOST}:{PORT} per-bot", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()

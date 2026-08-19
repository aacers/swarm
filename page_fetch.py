"""Web as MCP: fetch any page as text + links. No Chrome."""

from __future__ import annotations

import html as htmlmod
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_CHROME_HOSTS = (
    "accounts.google.com",
    "idmsa.apple.com",
    "appleid.apple.com",
    "login.live.com",
    "login.microsoftonline.com",
    "paypal.com",
)


def api_hint(url: str) -> tuple[str, str] | None:
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return None
    host = (p.hostname or "").lower()
    path = p.path or ""
    if "appstoreconnect.apple.com" in host:
        return (
            "asc",
            "App Store Connect is an API. Use `asc apps` / `asc app <name>`. Never Chrome login.",
        )
    if host.endswith("play.google.com") and ("/console" in path or "/developer" in path):
        return (
            "play",
            "Play Console has an API (PLAY_API_SA_JSON in kluis). Do not click-login in Chrome.",
        )
    if "etsy.com" in host:
        return ("etsy", "Etsy is an API. Use `etsy sales`. Never Chrome login.")
    if "gumroad.com" in host:
        return ("gumroad", "Gumroad is an API. Use `gumroad sales`. Never Chrome login.")
    if "searchads.apple.com" in host or host in {"ads.apple.com"}:
        return ("ads", "Apple Search Ads is an API. Use `ads today`. Never Chrome login.")
    return None


def must_chrome(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in _CHROME_HOSTS)

_URL_RE = re.compile(r"https?://[^\s\]\)>'\"<>]+", re.I)
_BARE_RE = re.compile(
    r"(?<![\w./@])((?:www\.)?[a-z0-9][a-z0-9-]*\.(?:nl|com|org|net|io|dev|app|be|de|uk))(?![\w.])",
    re.I,
)
_SITE_HINTS = (
    (
        re.compile(r"app ?store connect|testflight|\basc\b|appstoreconnect", re.I),
        "https://api.appstoreconnect.apple.com/v1/apps",
    ),
    (re.compile(r"\betsy\b", re.I), "https://www.etsy.com/"),
    (re.compile(r"\bgumroad\b", re.I), "https://gumroad.com/"),
    (
        re.compile(r"apple ads|search ads|ads\.apple|searchads", re.I),
        "https://api.searchads.apple.com/",
    ),
)

COOKIE_DIR = Path.home() / ".grok" / "browser" / "cookies"


def cookie_path(bot: str) -> Path:
    slug = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in (bot or "shared"))[:48]
    return COOKIE_DIR / f"{slug}.json"


def save_cookies(bot: str, cookies: list) -> None:
    if not bot:
        return
    path = cookie_path(bot)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        clean = []
        for c in cookies or []:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            clean.append(
                {
                    "name": c.get("name"),
                    "value": c.get("value") or "",
                    "domain": c.get("domain") or "",
                    "path": c.get("path") or "/",
                }
            )
        path.write_text(json.dumps(clean), encoding="utf-8")
        path.chmod(0o600)
    except Exception:
        pass


def cookie_header(url: str, bot: str) -> dict[str, str]:
    if not bot:
        return {}
    path = cookie_path(bot)
    if not path.is_file():
        return {}
    try:
        cookies = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return {}
    parts = []
    for c in cookies:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        d = str(c.get("domain") or "").lstrip(".").lower()
        if not d:
            continue
        if host == d or host.endswith("." + d):
            parts.append(f"{c.get('name')}={c.get('value') or ''}")
    if not parts:
        return {}
    return {"Cookie": "; ".join(parts)}


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._in_title = False
        self._in_a = False
        self._href = ""
        self._a_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in {"script", "style", "noscript", "svg", "template"}:
            self._skip += 1
            return
        if self._skip:
            return
        if t == "title":
            self._in_title = True
            return
        if t == "a":
            href = ""
            for k, v in attrs:
                if k.lower() == "href" and v:
                    href = v.strip()
                    break
            if href and not href.startswith(("#", "javascript:")):
                self._in_a = True
                self._href = href
                self._a_text = []
        if t in {"p", "div", "br", "li", "h1", "h2", "h3", "tr", "section"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in {"script", "style", "noscript", "svg", "template"} and self._skip:
            self._skip -= 1
            return
        if self._skip:
            return
        if t == "title":
            self._in_title = False
        if t == "a" and self._in_a:
            label = re.sub(r"\s+", " ", "".join(self._a_text)).strip()[:80]
            self.links.append((label, self._href))
            self._in_a = False
            self._href = ""

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title_parts.append(data)
        if self._in_a:
            self._a_text.append(data)
        s = data.strip()
        if s:
            self.text_parts.append(s + " ")


def looks_like_js_shell(page: dict) -> bool:
    text = (page.get("text") or "").strip()
    if page.get("api"):
        return False
    if len(text) >= 120:
        return False
    low = text.lower()
    if "enable javascript" in low or "enable js" in low:
        return True
    return len(text) < 40


def _cli_snapshot(tool: str) -> str:
    exe = Path.home() / ".grok" / "bin" / tool
    if not exe.is_file():
        return ""
    try:
        import subprocess

        args = [str(exe), "sales"] if tool in {"etsy", "gumroad"} else [str(exe), "today"]
        r = subprocess.run(args, capture_output=True, text=True, timeout=8)
        return ((r.stdout or r.stderr or "").strip())[:4000]
    except Exception as exc:
        return f"{tool} snapshot failed: {exc}"


def fetch_page(url: str, timeout: float = 4.0, bot: str = "") -> dict[str, Any]:
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "no url", "url": url, "text": "", "links": [], "controls": []}
    hint = api_hint(url)
    if hint:
        tool, msg = hint
        extra = _asc_snapshot() if tool == "asc" else _cli_snapshot(tool)
        return {
            "ok": True,
            "api": True,
            "tool": tool,
            "url": url,
            "title": "API",
            "text": (msg + ("\n" + extra if extra else "")).strip(),
            "links": [],
            "controls": [],
            "hint": msg,
        }
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    }
    headers.update(cookie_header(url, bot))
    req = urllib.request.Request(url, headers=headers)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read(800_000)
            final = str(resp.geturl() or url)
            ctype = (resp.headers.get("Content-Type") or "").lower()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "url": url, "title": "", "text": "", "links": [], "controls": []}
    if "json" in ctype or (raw[:1] in (b"{", b"[") and "html" not in ctype):
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            payload = raw.decode("utf-8", "replace")[:8000]
        text = json.dumps(payload, ensure_ascii=False, default=str)[:8000] if not isinstance(payload, str) else payload[:8000]
        return {
            "ok": True,
            "url": final,
            "title": "JSON",
            "text": text,
            "links": [],
            "controls": [],
        }
    decoded = raw.decode("utf-8", "replace")
    parser = _Page()
    try:
        parser.feed(decoded)
        parser.close()
    except Exception:
        pass
    title = re.sub(r"\s+", " ", "".join(parser.title_parts)).strip()[:180]
    text = htmlmod.unescape(re.sub(r"[ \t]+", " ", "".join(parser.text_parts)))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()[:12000]
    controls = []
    seen: set[str] = set()
    for i, (label, href) in enumerate(parser.links[:50], start=1):
        abs_href = urllib.parse.urljoin(final, href)
        key = abs_href + "|" + label
        if key in seen:
            continue
        seen.add(key)
        controls.append({"n": i, "tag": "a", "text": label, "href": abs_href[:180]})
    return {
        "ok": True,
        "url": final,
        "title": title,
        "text": text,
        "links": [{"text": c["text"], "href": c["href"]} for c in controls[:40]],
        "controls": controls,
    }


def extract_urls(text: str) -> list[str]:
    found = [m.rstrip(".,);") for m in _URL_RE.findall(text or "")]
    out: list[str] = []
    for u in found:
        if u not in out:
            out.append(u)
        if len(out) >= 3:
            break
    if len(out) < 3:
        for m in _BARE_RE.finditer(text or ""):
            host = m.group(1).lower().rstrip(".")
            if host.startswith("www."):
                host = host[4:]
            u = "https://" + host
            if u not in out:
                out.append(u)
            if len(out) >= 3:
                break
    if out:
        return out
    low = text or ""
    for rx, url in _SITE_HINTS:
        if rx.search(low):
            return [url]
    return []


def _chrome_open(url: str, bot: str) -> None:
    try:
        body = json.dumps({"url": url, "bot": bot, "chrome": True}).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:8791/open?bot=" + urllib.parse.quote(bot),
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=12).read()
    except Exception:
        pass


def gate(url: str, bot: str = "") -> dict[str, Any]:
    """API, then cookie-fetch, Chrome only for login/JS-shell. Enforced in code."""
    url = (url or "").strip()
    if must_chrome(url):
        return {"action": "chrome", "reason": "login", "url": url}
    page = fetch_page(url, bot=bot)
    if page.get("api"):
        return {"action": "api", "page": page, "url": url}
    if useful_page(page):
        return {"action": "fetch", "page": page, "url": page.get("url") or url}
    reason = "error" if not page.get("ok") else "js-shell"
    return {"action": "chrome", "reason": reason, "page": page, "url": url}


def handle_web(text: str, bot: str = "") -> str:
    """User question: open via gate. Chrome thread only when the gate says so."""
    import threading

    urls = extract_urls(text or "")
    if not urls and bot:
        href = follow_page(text, bot)
        return href
    if not urls:
        return ""
    url = urls[0]
    g = gate(url, bot)
    page = g.get("page") or {"url": url, "text": "", "title": g.get("reason") or g.get("action")}
    if bot:
        _write_last_page(bot, [page])
    if g.get("action") == "chrome" and bot:
        threading.Thread(target=_chrome_open, args=(url, bot), daemon=True).start()
    return url


def preview_chrome(text: str, bot: str = "") -> None:
    handle_web(text, bot)


def current_browse_url(bot: str) -> str:
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8791/health?bot=" + urllib.parse.quote(bot or "")
        )
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            d = json.loads(resp.read().decode())
        url = str((d or {}).get("url") or "")
        if url and url != "about:blank":
            return url
    except Exception:
        pass
    return ""


def best_link(page: dict, query: str) -> str:
    q = re.sub(r"\s+", " ", (query or "").strip().lower())
    q = re.sub(r"^(de|het|een|the|a|naar)\s+", "", q)
    if not q:
        return ""
    best, score = "", -1
    rows = list(page.get("controls") or []) + list(page.get("links") or [])
    for c in rows:
        label = re.sub(r"\s+", " ", str(c.get("text") or "").strip().lower())
        href = str(c.get("href") or c.get("url") or "").strip()
        if not href.startswith("http"):
            continue
        s = 0
        if label == q:
            s = 100
        elif q in label:
            s = 80 - min(40, max(0, len(label) - len(q)))
        elif label and label in q and len(label) >= 4:
            s = 55
        else:
            words = [w for w in q.split() if len(w) > 2]
            if words and all(w in label for w in words):
                s = 45
        if s > score:
            score, best = s, href
    return best if score >= 40 else ""


_NAV_RE = re.compile(
    r"^(?:ga naar|klik(?:\s+op)?|open|tap|go to|navigeer naar)\s+(.+?)\s*$",
    re.I,
)


def follow_page(text: str, bot: str = "") -> str:
    """Follow-up like 'ga naar over ons': open that link on the current site now."""
    import threading

    body = (text or "").strip()
    if not body or not bot or extract_urls(body):
        return ""
    m = _NAV_RE.match(body)
    if not m:
        return ""
    query = m.group(1).strip().strip(".!?")
    if not query or extract_urls(query):
        return ""
    here = current_browse_url(bot)
    if not here:
        return ""
    page = fetch_page(here, timeout=3.5, bot=bot)
    href = best_link(page, query)
    if not href:
        return ""
    g = gate(href, bot)
    if bot:
        _write_last_page(bot, [g.get("page") or {"url": href}])
    if g.get("action") == "chrome":
        threading.Thread(target=_chrome_open, args=(href, bot), daemon=True).start()
    return href


def _asc_snapshot() -> str:
    try:
        from pathlib import Path
        import subprocess

        exe = Path.home() / ".grok" / "bin" / "asc"
        if exe.is_file():
            r = subprocess.run(
                [str(exe), "apps"],
                capture_output=True,
                text=True,
                timeout=6,
            )
            out = (r.stdout or r.stderr or "").strip()
            if out:
                return "apps:\n" + out[:4000]
        env = Path.home() / ".appstoreconnect" / "testflight.env"
        if not env.is_file():
            return ""
        import jwt

        key_id = issuer = ""
        for line in env.read_text(encoding="utf-8").splitlines():
            line = re.sub(r"^export\s+", "", line.strip())
            if line.startswith("APPSTORE_KEY_ID="):
                key_id = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("APPSTORE_ISSUER_ID="):
                issuer = line.split("=", 1)[1].strip().strip('"')
        p8 = Path.home() / ".appstoreconnect" / "private_keys" / f"AuthKey_{key_id}.p8"
        if not key_id or not issuer or not p8.is_file():
            return ""
        import time as _t

        token = jwt.encode(
            {"iss": issuer, "exp": int(_t.time()) + 600, "aud": "appstoreconnect-v1"},
            p8.read_text(),
            algorithm="ES256",
            headers={"kid": key_id, "typ": "JWT"},
        )
        req = urllib.request.Request(
            "https://api.appstoreconnect.apple.com/v1/apps?limit=30",
            headers={"Authorization": "Bearer " + token},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode())
        rows = []
        for a in data.get("data") or []:
            attr = a.get("attributes") or {}
            rows.append(f"{attr.get('name')}  {attr.get('bundleId')}  {a.get('id')}")
        return "apps:\n" + "\n".join(rows[:30]) if rows else "apps: (none)"
    except Exception as exc:
        return f"asc snapshot failed: {exc}"


def prefetch_from_text(text: str, timeout: float = 3.5) -> list[dict]:
    urls = extract_urls(text or "")
    if not urls:
        return []
    pages: list[dict] = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {pool.submit(fetch_page, u, timeout): u for u in urls}
        for fut in as_completed(futs):
            try:
                pages.append(fut.result())
            except Exception as exc:
                pages.append({"ok": False, "url": futs[fut], "text": "", "error": str(exc)})
    return pages


_BLOCK_TITLE = re.compile(
    r"privacy|consent|cookie|enable javascript|just a moment|attention required|privacy gate",
    re.I,
)


def useful_page(page: dict) -> bool:
    if not page:
        return False
    if page.get("api"):
        return True
    if not page.get("ok"):
        return False
    if looks_like_js_shell(page):
        return False
    if _BLOCK_TITLE.search(page.get("title") or ""):
        return False
    return len((page.get("text") or "").strip()) >= 80


def _write_last_page(slug: str, pages: list[dict]) -> str:
    slug = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in (slug or ""))[:48]
    if not slug or not pages:
        return ""
    dest = Path.home() / ".grok" / "imac-phone" / "agents" / slug / "last-page.txt"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        chunks = []
        for p in pages:
            chunks.append(p.get("url") or "")
            if p.get("title"):
                chunks.append(str(p["title"]))
            chunks.append((p.get("text") or p.get("error") or "")[:12000])
            chunks.append("---")
        dest.write_text("\n".join(chunks).strip() + "\n", encoding="utf-8")
        return str(dest)
    except Exception:
        return ""


def attach_pages(text: str, slug: str = "") -> str:
    """Short note on the question. Full page stays in the Swarm browser, not the chat."""
    body = (text or "").rstrip()
    if not body or "[web already fetched" in body:
        return text
    urls = extract_urls(body)
    pages = prefetch_from_text(body) if urls else []
    useful = [p for p in pages if useful_page(p)]
    if not urls and not useful:
        return text
    store = _write_last_page(slug, useful or pages)
    bits = [body, "", "---", "[web already fetched — do not gbrowse chrome / login]"]
    if not useful:
        bits.append("Swarm browser opening " + ", ".join(urls) + ".")
        bits.append("Do not start Chrome. Do not paste the page in chat.")
        bits.append("---")
        return "\n".join(bits).strip() + "\n"
    for p in useful:
        url = p.get("url") or ""
        title = (p.get("title") or "").strip()
        if p.get("api"):
            bits.append(str(p.get("hint") or "Use the API, not Chrome."))
            bits.append("Do not paste listings in chat.")
        else:
            line = "Swarm browser opening " + url
            if title:
                line += " — " + title[:80]
            bits.append(line + ".")
            bits.append("Do not gbrowse chrome. Do not paste the page in chat.")
        if store:
            bits.append("Full text: " + store)
        bits.append("---")
    return "\n".join(bits).strip() + "\n"

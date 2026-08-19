#!/usr/bin/env python3
"""Swarm as one native window — no extra Chrome."""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path

import webview

TOKEN = (Path.home() / ".grok" / "imac-phone" / "token").read_text().strip()
URL = f"http://localhost:8790/?k={TOKEN}"
SETTINGS = Path.home() / ".grok" / "imac-phone" / "settings.json"


def window_bg() -> str:
    try:
        if json.loads(SETTINGS.read_text(encoding="utf-8")).get("theme") == "dark":
            return "#14161c"
    except Exception:
        pass
    return "#e6e7ed"


def up(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=0.6).read()
        return True
    except Exception:
        return False


def ensure() -> None:
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    if not up("http://127.0.0.1:8790/health"):
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/com.timgrootes.imac-phone"],
            check=False,
        )
    if not up("http://127.0.0.1:8791/health"):
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/com.timgrootes.heavy-browse"],
            check=False,
        )
    for _ in range(25):
        if up("http://127.0.0.1:8790/health"):
            return
        time.sleep(0.2)


_DROP_OK: set[str] = set()


class Api:
    def dropped_file(self, path: str) -> str:
        p = Path(str(path or "")).expanduser()
        try:
            p = p.resolve()
        except Exception:
            return json.dumps({"error": "bad path"})
        if str(p) not in _DROP_OK and str(path) not in _DROP_OK:
            return json.dumps({"error": "drop expired"})
        if not p.is_file():
            return json.dumps({"error": "not a file"})
        try:
            size = p.stat().st_size
        except OSError:
            return json.dumps({"error": "unreadable"})
        if size > 12_000_000:
            return json.dumps({"error": "file too large (max 12 MB)"})
        try:
            raw = p.read_bytes()
        except OSError:
            return json.dumps({"error": "unreadable"})
        import base64

        return json.dumps(
            {"name": p.name, "mime": "", "data": base64.b64encode(raw).decode("ascii")}
        )

    def clipboard(self) -> str:
        try:
            r = subprocess.run(["pbpaste"], capture_output=True, timeout=2)
            return (r.stdout or b"").decode("utf-8", "replace")
        except Exception:
            return ""

    def clipboard_image(self) -> str:
        try:
            from AppKit import NSPasteboard, NSPasteboardTypePNG, NSPasteboardTypeTIFF
        except Exception:
            return ""
        try:
            import base64
            import tempfile

            pb = NSPasteboard.generalPasteboard()
            data = pb.dataForType_(NSPasteboardTypePNG)
            name, mime = "paste.png", "image/png"
            if data is None:
                data = pb.dataForType_(NSPasteboardTypeTIFF)
                name, mime = "paste.png", "image/png"
                if data is None:
                    return ""
                raw = bytes(data)
                tmp = Path(tempfile.mkstemp(suffix=".tiff")[1])
                out = tmp.with_suffix(".png")
                try:
                    tmp.write_bytes(raw)
                    subprocess.run(
                        ["sips", "-s", "format", "png", str(tmp), "--out", str(out)],
                        capture_output=True,
                        timeout=4,
                    )
                    raw = out.read_bytes() if out.is_file() else raw
                finally:
                    tmp.unlink(missing_ok=True)
                    out.unlink(missing_ok=True)
            else:
                raw = bytes(data)
            if not raw:
                return ""
            return json.dumps(
                {"name": name, "mime": mime, "data": base64.b64encode(raw).decode("ascii")}
            )
        except Exception:
            return ""


def _bind_drops(window) -> None:
    try:
        from webview.dom import DOMEventHandler
    except Exception as exc:
        print("drop bind import", exc, flush=True)
        return

    def on_drag(_e):
        try:
            window.evaluate_js(
                "var p=document.getElementById('p-chat'); if(p) p.classList.add('dropon');"
            )
        except Exception:
            pass

    def on_drop(e):
        files = ((e or {}).get("dataTransfer") or {}).get("files") or []
        paths = []
        for f in files:
            if not isinstance(f, dict):
                continue
            path = f.get("pywebviewFullPath") or ""
            if path:
                paths.append(path)
                _DROP_OK.add(path)
                try:
                    _DROP_OK.add(str(Path(path).resolve()))
                except Exception:
                    pass
        try:
            window.evaluate_js(
                "var p=document.getElementById('p-chat'); if(p) p.classList.remove('dropon');"
            )
        except Exception:
            pass
        if not paths:
            return
        try:
            window.evaluate_js(f"window.swarmNativeDrop({json.dumps(paths)})")
        except Exception as exc:
            print("drop eval", exc, flush=True)

    try:
        window.dom.document.events.dragenter += DOMEventHandler(on_drag, True, True)
        window.dom.document.events.dragover += DOMEventHandler(on_drag, True, True, debounce=200)
        window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
    except Exception as exc:
        print("drop bind", exc, flush=True)


def main() -> None:
    ensure()
    window = webview.create_window(
        "Swarm",
        URL,
        width=1200,
        height=800,
        min_size=(800, 560),
        background_color=window_bg(),
        text_select=True,
        js_api=Api(),
    )
    webview.start(_bind_drops, window)


if __name__ == "__main__":
    main()

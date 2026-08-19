#!/usr/bin/env python3
"""Local iOS/Android lab for Swarm.

Talks to the Simulator / emulator runtime over simctl + Maestro/idb/adb.
Never moves the Mac mouse or steals the desktop.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST, PORT = "127.0.0.1", 8793
LAB_DIR = Path.home() / ".grok" / "imac-phone" / "lab"
SHOT_PATH = LAB_DIR / "shot.png"
DEVICE_NAME = "Swarm Lab"
AVD_NAME = "SwarmLab"
ANDROID_HOME = Path(os.environ.get("ANDROID_HOME") or "/opt/homebrew/share/android-commandlinetools")
SIMCTL = ["xcrun", "simctl"]
ANDROID_IMAGE = "system-images;android-34;google_apis_playstore;arm64-v8a"

_lock = threading.Lock()
_state: dict = {
    "ok": True,
    "udid": "",
    "name": "",
    "bundle": "",
    "busy": False,
    "cmd": "",
    "driver": "",
    "platform": "ios",
    "at": 0.0,
}


def _now() -> float:
    return time.time()


def _mark(cmd: str, busy: bool = False, **extra) -> None:
    _state["cmd"] = cmd
    _state["busy"] = busy
    _state["at"] = _now()
    for k, v in extra.items():
        if v is not None:
            _state[k] = v


def _run(cmd: list[str], timeout: float = 40, input_bytes: bytes | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    e = os.environ.copy()
    if env:
        e.update(env)
    java = _java_home()
    if java:
        e.setdefault("JAVA_HOME", java)
        e["PATH"] = str(Path(java) / "bin") + ":" + e.get("PATH", "")
    e.setdefault("ANDROID_HOME", str(ANDROID_HOME))
    e.setdefault("ANDROID_SDK_ROOT", str(ANDROID_HOME))
    e["PATH"] = ":".join(
        [
            str(ANDROID_HOME / "platform-tools"),
            str(ANDROID_HOME / "emulator"),
            str(ANDROID_HOME / "cmdline-tools" / "latest" / "bin"),
            e.get("PATH", ""),
        ]
    )
    e.setdefault("MAESTRO_CLI_NO_ANALYTICS", "1")
    maestro_bin = Path.home() / ".maestro" / "bin"
    if maestro_bin.is_dir():
        e["PATH"] = str(maestro_bin) + ":" + e.get("PATH", "")
    return subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        input=input_bytes,
        env=e,
    )


def _java_home() -> str:
    for p in (
        os.environ.get("JAVA_HOME") or "",
        "/opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home",
        "/opt/homebrew/opt/openjdk@17",
        "/Library/Java/JavaVirtualMachines/temurin-17.jdk/Contents/Home",
    ):
        if p and (Path(p) / "bin" / "java").exists():
            return p
    return ""


def _which(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    extra = [
        Path.home() / ".maestro" / "bin" / name,
        Path("/opt/homebrew/bin") / name,
        ANDROID_HOME / "platform-tools" / name,
        ANDROID_HOME / "emulator" / name,
        ANDROID_HOME / "cmdline-tools" / "latest" / "bin" / name,
        Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / name,
        Path.home() / "Library" / "Android" / "sdk" / "emulator" / name,
    ]
    for p in extra:
        if p.exists():
            return str(p)
    return ""


def driver_name() -> str:
    if _which("maestro") and _java_home():
        return "maestro"
    if _which("idb"):
        return "idb"
    if _which("adb"):
        return "adb"
    return "simctl"


def simctl_json() -> dict:
    r = _run(SIMCTL + ["list", "devices", "-j"], timeout=20)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).decode()[:400] or "simctl list failed")
    return json.loads(r.stdout.decode() or "{}")


def list_ios() -> list[dict]:
    raw = simctl_json()
    out = []
    for runtime, devices in (raw.get("devices") or {}).items():
        rt = runtime.rsplit(".", 1)[-1].replace("-", ".", 1).replace("-", ".")
        for d in devices or []:
            if not d.get("isAvailable", True):
                continue
            out.append(
                {
                    "udid": d.get("udid") or "",
                    "name": d.get("name") or "",
                    "state": (d.get("state") or "").lower(),
                    "runtime": rt,
                    "platform": "ios",
                    "lab": (d.get("name") or "") == DEVICE_NAME,
                }
            )
    out.sort(key=lambda x: (0 if x["lab"] else 1, 0 if x["state"] == "booted" else 1, x["name"]))
    return out


def _adb() -> str:
    return _which("adb")


def _emulator_bin() -> str:
    return _which("emulator") or str(ANDROID_HOME / "emulator" / "emulator")


def is_android_id(udid: str) -> bool:
    u = (udid or "").strip()
    if not u:
        return False
    if u.lower() in {"android", "play", "emulator", avd_name().lower()}:
        return True
    if u.startswith("emulator-") or u.startswith("adb-"):
        return True
    return bool(re.fullmatch(r"[0-9A-Fa-f]{8}-([0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}", u)) is False and len(u) <= 22


def avd_name() -> str:
    return AVD_NAME


def list_avds() -> list[str]:
    exe = _emulator_bin()
    if not exe or not Path(exe).exists():
        return []
    r = _run([exe, "-list-avds"], timeout=12)
    return [ln.strip() for ln in (r.stdout or b"").decode().splitlines() if ln.strip()]


def list_android() -> list[dict]:
    out = []
    adb = _adb()
    live = set()
    if adb:
        r = _run([adb, "devices", "-l"], timeout=10)
        for line in (r.stdout or b"").decode().splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serial = parts[0]
                live.add(serial)
                model = ""
                for bit in parts[2:]:
                    if bit.startswith("model:"):
                        model = bit.split(":", 1)[1].replace("_", " ")
                out.append(
                    {
                        "udid": serial,
                        "name": model or serial,
                        "state": "booted",
                        "platform": "android",
                        "lab": AVD_NAME.lower() in (model or serial).lower() or serial.startswith("emulator-"),
                    }
                )
    for name in list_avds():
        if any(d.get("avd") == name for d in out):
            continue
        if name == AVD_NAME and any(d.get("lab") and d["state"] == "booted" for d in out):
            continue
        if any(d["state"] == "booted" for d in out) and name == AVD_NAME:
            continue
        out.append(
            {
                "udid": name,
                "name": name,
                "state": "shutdown",
                "platform": "android",
                "lab": name == AVD_NAME,
                "avd": name,
            }
        )
    out.sort(key=lambda x: (0 if x.get("lab") else 1, 0 if x["state"] == "booted" else 1, x["name"]))
    return out


def devices() -> list[dict]:
    return list_ios() + list_android()


def pick_udid(want: str = "") -> str:
    want = (want or "").strip()
    ios = list_ios()
    ands = list_android()
    if want:
        if want.lower() in {"android", "play", "emulator"}:
            for d in ands:
                if d["state"] == "booted":
                    return d["udid"]
            return (ands[0]["udid"] if ands else "")
        if want.lower() in {"ios", "iphone", "simulator"}:
            want = ""
        else:
            for d in ios + ands:
                if d["udid"] == want or d["name"].lower() == want.lower():
                    return d["udid"]
            return want
    if (_state.get("platform") or "") == "android":
        for d in ands:
            if d["state"] == "booted" and str(d["udid"]).startswith("emulator-"):
                return d["udid"]
        for d in ands:
            if d["state"] == "booted":
                return d["udid"]
        return ands[0]["udid"] if ands else ""
    cur = (_state.get("udid") or "").strip()
    if cur:
        return cur
    for d in ios:
        if d["lab"] and d["state"] == "booted":
            return d["udid"]
    for d in ios:
        if d["state"] == "booted" and "watch" not in d["name"].lower():
            return d["udid"]
    for d in ios:
        if d["lab"]:
            return d["udid"]
    phones = [d for d in ios if "watch" not in d["name"].lower() and "ipad" not in d["name"].lower()]
    return (phones[0]["udid"] if phones else (ios[0]["udid"] if ios else ""))


def device_type_iphone() -> str:
    r = _run(SIMCTL + ["list", "devicetypes", "-j"], timeout=20)
    types = json.loads(r.stdout.decode() or "{}").get("devicetypes") or []
    names = [t.get("identifier") or "" for t in types]
    for ident in (
        "com.apple.CoreSimulator.SimDeviceType.iPhone-16",
        "com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro",
        "com.apple.CoreSimulator.SimDeviceType.iPhone-15",
    ):
        if ident in names:
            return ident
    for ident in names:
        if "iPhone" in ident and "Watch" not in ident:
            return ident
    raise RuntimeError("no iPhone simulator type")


def ios_runtime() -> str:
    r = _run(SIMCTL + ["list", "runtimes", "-j"], timeout=20)
    runs = json.loads(r.stdout.decode() or "{}").get("runtimes") or []
    avail = [x for x in runs if x.get("isAvailable") and "iOS" in (x.get("name") or "")]
    if not avail:
        raise RuntimeError("no iOS runtime")
    avail.sort(key=lambda x: x.get("version") or "", reverse=True)
    return avail[0].get("identifier") or ""


def ensure_lab_device() -> str:
    for d in list_ios():
        if d["lab"]:
            return d["udid"]
    r = _run(SIMCTL + ["create", DEVICE_NAME, device_type_iphone(), ios_runtime()], timeout=30)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).decode()[:400] or "simctl create failed")
    return (r.stdout.decode() or "").strip()


def boot_ios(udid: str = "", hide: bool = True) -> dict:
    want = (udid or "").strip()
    if want and want.lower() not in {"ios", "iphone", "simulator"}:
        udid = pick_udid(want)
    else:
        udid = ensure_lab_device()
    if not udid:
        raise RuntimeError("no simulator")
    r = _run(SIMCTL + ["boot", udid], timeout=60)
    err = (r.stderr or b"").decode()
    if r.returncode != 0 and "already booted" not in err.lower() and "current state: booted" not in err.lower():
        raise RuntimeError(err[:400] or "boot failed")
    if hide:
        subprocess.Popen(
            ["open", "-gj", "-a", "Simulator"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    name = next((d["name"] for d in list_ios() if d["udid"] == udid), DEVICE_NAME)
    _mark("boot", udid=udid, name=name, platform="ios")
    return {"ok": True, "udid": udid, "name": name, "platform": "ios", "driver": driver_name()}


def ensure_avd() -> str:
    names = list_avds()
    if AVD_NAME in names:
        return AVD_NAME
    avd = _which("avdmanager")
    if not avd:
        raise RuntimeError("avdmanager missing — Android SDK not ready")
    cfg = (
        "hw.keyboard=yes\n"
        "hw.ramSize=2048\n"
        "disk.dataPartition.size=6G\n"
        "showDeviceFrame=no\n"
    )
    r = _run(
        [
            avd,
            "create",
            "avd",
            "-n",
            AVD_NAME,
            "-k",
            ANDROID_IMAGE,
            "-d",
            "pixel_7",
            "--force",
        ],
        timeout=40,
        input_bytes=b"no\n",
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).decode()[:500] or "avd create failed")
    ini = Path.home() / ".android" / "avd" / f"{AVD_NAME}.avd" / "config.ini"
    if ini.is_file():
        text = ini.read_text(encoding="utf-8")
        if "hw.keyboard=yes" not in text:
            ini.write_text(text + cfg, encoding="utf-8")
    return AVD_NAME


def boot_android() -> dict:
    adb = _adb()
    if not adb:
        raise RuntimeError("adb missing")
    for d in list_android():
        if d["state"] == "booted" and d["udid"].startswith("emulator-"):
            _mark("boot", udid=d["udid"], name=d["name"], platform="android")
            return {"ok": True, "udid": d["udid"], "name": d["name"], "platform": "android", "driver": driver_name()}
    emu = _emulator_bin()
    if not emu or not Path(emu).exists():
        raise RuntimeError("Android emulator not installed")
    name = ensure_avd()
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    log = LAB_DIR / "emulator.log"
    env = os.environ.copy()
    java = _java_home()
    if java:
        env["JAVA_HOME"] = java
        env["PATH"] = str(Path(java) / "bin") + ":" + env.get("PATH", "")
    env["ANDROID_HOME"] = str(ANDROID_HOME)
    env["ANDROID_SDK_ROOT"] = str(ANDROID_HOME)
    with log.open("ab") as fh:
        subprocess.Popen(
            [
                emu,
                "-avd",
                name,
                "-no-window",
                "-no-audio",
                "-no-boot-anim",
                "-gpu",
                "swiftshader_indirect",
                "-netdelay",
                "none",
                "-netspeed",
                "full",
            ],
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    serial = ""
    for _ in range(90):
        r = _run([adb, "devices"], timeout=8)
        for line in (r.stdout or b"").decode().splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device" and parts[0].startswith("emulator-"):
                serial = parts[0]
                break
        if serial:
            boot = _run([adb, "-s", serial, "shell", "getprop", "sys.boot_completed"], timeout=8)
            if (boot.stdout or b"").decode().strip() == "1":
                _mark("boot", udid=serial, name=name, platform="android")
                return {
                    "ok": True,
                    "udid": serial,
                    "name": name,
                    "platform": "android",
                    "driver": driver_name(),
                }
        time.sleep(2)
    raise RuntimeError("Android emulator boot timeout — see ~/.grok/imac-phone/lab/emulator.log")


def boot(udid: str = "", hide: bool = True, platform: str = "") -> dict:
    plat = (platform or udid or "").strip().lower()
    if plat in {"both", "all"}:
        ios = boot_ios("", hide)
        try:
            andr = boot_android()
        except Exception as exc:
            andr = {"ok": False, "error": str(exc), "platform": "android"}
        return {"ok": True, "ios": ios, "android": andr, "driver": driver_name()}
    if plat in {"android", "play", "emulator"} or is_android_id(udid):
        return boot_android()
    return boot_ios(udid if plat not in {"ios", "iphone", "simulator"} else "", hide=hide)


def shutdown(udid: str = "") -> dict:
    udid = pick_udid(udid)
    if not udid:
        return {"ok": True}
    if is_android_id(udid) or (_state.get("platform") == "android"):
        adb = _adb()
        if adb and str(udid).startswith("emulator-"):
            _run([adb, "-s", udid, "emu", "kill"], timeout=15)
        return {"ok": True, "udid": udid, "platform": "android"}
    _run(SIMCTL + ["shutdown", udid], timeout=30)
    return {"ok": True, "udid": udid, "platform": "ios"}


def screenshot(udid: str = "") -> bytes:
    udid = pick_udid(udid)
    if not udid:
        raise RuntimeError("no device")
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    dest = SHOT_PATH
    if is_android_id(udid) or str(udid).startswith("emulator-") or _state.get("platform") == "android":
        adb = _adb()
        if not adb:
            raise RuntimeError("adb missing")
        last = ""
        for _ in range(12):
            r = _run([adb, "-s", udid, "exec-out", "screencap", "-p"], timeout=20)
            data = r.stdout or b""
            if r.returncode == 0 and len(data) > 2000 and data[:4] == b"\x89PNG":
                dest.write_bytes(data)
                return data
            last = (r.stderr or b"").decode()[:300]
            time.sleep(0.8)
        raise RuntimeError(last or "android screenshot failed")
    last = ""
    for _ in range(10):
        r = _run(SIMCTL + ["io", udid, "screenshot", str(dest)], timeout=20)
        last = (r.stderr or r.stdout or b"").decode()[:400]
        if dest.is_file() and dest.stat().st_size > 2000:
            return dest.read_bytes()
        time.sleep(0.7)
    raise RuntimeError(last or "screenshot failed")


def _apk_from_aab(aab: Path) -> Path:
    tool = _which("bundletool")
    jars = list(Path("/opt/homebrew/share").glob("**/bundletool*.jar"))
    dest = LAB_DIR / "from-aab.apks"
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    cmd: list[str]
    if tool:
        cmd = [tool, "build-apks", f"--bundle={aab}", f"--output={dest}", "--mode=universal", "--overwrite"]
    elif jars:
        java = str(Path(_java_home()) / "bin" / "java") if _java_home() else "java"
        cmd = [java, "-jar", str(jars[0]), "build-apks", f"--bundle={aab}", f"--output={dest}", "--mode=universal"]
    else:
        raise RuntimeError("AAB needs bundletool — build a debug APK instead (assembleDebug)")
    r = _run(cmd, timeout=120)
    if r.returncode != 0 or not dest.is_file():
        raise RuntimeError((r.stderr or r.stdout).decode()[:400] or "bundletool failed")
    out_dir = LAB_DIR / "apks"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.unpack_archive(str(dest), str(out_dir), "zip")
    apks = list(out_dir.rglob("*.apk"))
    if not apks:
        raise RuntimeError("no apk inside aab")
    return apks[0]


def install(path: str, udid: str = "") -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        raise RuntimeError(f"not found: {p}")
    suf = p.suffix.lower()
    want_android = suf in {".apk", ".aab"} or (udid and is_android_id(udid)) or _state.get("platform") == "android"
    if want_android:
        adb = _adb()
        if not adb:
            raise RuntimeError("adb missing")
        serial = pick_udid(udid or "android")
        if not serial.startswith("emulator-"):
            boot_android()
            serial = pick_udid("android")
        apk = _apk_from_aab(p) if suf == ".aab" else p
        r = _run([adb, "-s", serial, "install", "-r", "-t", str(apk)], timeout=120)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).decode()[:400] or "adb install failed")
        return {"ok": True, "udid": serial, "path": str(apk), "platform": "android"}
    udid = pick_udid(udid)
    r = _run(SIMCTL + ["install", udid, str(p)], timeout=90)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).decode()[:400] or "install failed")
    return {"ok": True, "udid": udid, "path": str(p), "platform": "ios"}


def launch(bundle: str, udid: str = "") -> dict:
    bundle = (bundle or "").strip()
    if not bundle:
        raise RuntimeError("bundle id required")
    udid = pick_udid(udid)
    if is_android_id(udid) or _state.get("platform") == "android":
        adb = _adb()
        r = _run(
            [adb, "-s", udid, "shell", "monkey", "-p", bundle, "-c", "android.intent.category.LAUNCHER", "1"],
            timeout=25,
        )
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).decode()[:400] or "android launch failed")
        _mark("launch", bundle=bundle, udid=udid, platform="android")
        return {"ok": True, "udid": udid, "bundle": bundle, "platform": "android"}
    r = _run(SIMCTL + ["launch", udid, bundle], timeout=30)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).decode()[:400] or "launch failed")
    _mark("launch", bundle=bundle, udid=udid, platform="ios")
    return {"ok": True, "udid": udid, "bundle": bundle, "platform": "ios"}


def terminate(bundle: str = "", udid: str = "") -> dict:
    udid = pick_udid(udid)
    bundle = (bundle or _state.get("bundle") or "").strip()
    if not bundle:
        return {"ok": True, "udid": udid}
    if is_android_id(udid) or _state.get("platform") == "android":
        _run([_adb(), "-s", udid, "shell", "am", "force-stop", bundle], timeout=15)
        return {"ok": True, "udid": udid, "bundle": bundle, "platform": "android"}
    _run(SIMCTL + ["terminate", udid, bundle], timeout=20)
    return {"ok": True, "udid": udid, "bundle": bundle, "platform": "ios"}


def openurl(url: str, udid: str = "") -> dict:
    udid = pick_udid(udid)
    if is_android_id(udid) or _state.get("platform") == "android":
        r = _run(
            [_adb(), "-s", udid, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url],
            timeout=20,
        )
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).decode()[:400] or "android openurl failed")
        return {"ok": True, "udid": udid, "url": url, "platform": "android"}
    r = _run(SIMCTL + ["openurl", udid, url], timeout=20)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).decode()[:400] or "openurl failed")
    return {"ok": True, "udid": udid, "url": url, "platform": "ios"}


def appearance(value: str = "", udid: str = "") -> dict:
    udid = pick_udid(udid)
    if is_android_id(udid) or _state.get("platform") == "android":
        mode = ""
        if value in {"dark", "yes", "night"}:
            mode = "yes"
        elif value in {"light", "no", "day"}:
            mode = "no"
        if mode:
            _run([_adb(), "-s", udid, "shell", "cmd", "uimode", "night", mode], timeout=15)
        r = _run([_adb(), "-s", udid, "shell", "cmd", "uimode", "night"], timeout=10)
        got = (r.stdout or b"").decode().strip()
        return {"ok": True, "appearance": got or value, "platform": "android"}
    cmd = SIMCTL + ["ui", udid, "appearance"]
    if value:
        cmd.append(value)
    r = _run(cmd, timeout=15)
    got = (r.stdout or r.stderr or b"").decode().strip().splitlines()
    return {"ok": True, "appearance": (got[-1] if got else value or ""), "platform": "ios"}


def content_size(value: str = "", udid: str = "") -> dict:
    udid = pick_udid(udid)
    if is_android_id(udid) or _state.get("platform") == "android":
        scale = {
            "extra-small": "0.85",
            "small": "0.9",
            "medium": "1.0",
            "large": "1.0",
            "extra-large": "1.15",
            "extra-extra-large": "1.3",
            "extra-extra-extra-large": "1.45",
            "accessibility-large": "1.6",
            "accessibility-extra-large": "1.8",
        }.get((value or "").lower(), "")
        if value.replace(".", "", 1).isdigit():
            scale = value
        if scale:
            _run([_adb(), "-s", udid, "shell", "settings", "put", "system", "font_scale", scale], timeout=12)
        r = _run([_adb(), "-s", udid, "shell", "settings", "get", "system", "font_scale"], timeout=8)
        return {"ok": True, "content_size": (r.stdout or b"").decode().strip() or value, "platform": "android"}
    cmd = SIMCTL + ["ui", udid, "content_size"]
    if value:
        cmd.append(value)
    r = _run(cmd, timeout=15)
    got = (r.stdout or r.stderr or b"").decode().strip().splitlines()
    return {"ok": True, "content_size": (got[-1] if got else value or ""), "platform": "ios"}


def contrast(enabled: bool | None = None, udid: str = "") -> dict:
    udid = pick_udid(udid)
    cmd = SIMCTL + ["ui", udid, "increase_contrast"]
    if enabled is True:
        cmd.append("enabled")
    elif enabled is False:
        cmd.append("disabled")
    r = _run(cmd, timeout=15)
    got = (r.stdout or r.stderr or b"").decode().strip().splitlines()
    return {"ok": True, "increase_contrast": (got[-1] if got else "")}


def privacy(action: str, service: str, bundle: str, udid: str = "") -> dict:
    udid = pick_udid(udid)
    r = _run(SIMCTL + ["privacy", udid, action, service, bundle], timeout=15)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).decode()[:400] or "privacy failed")
    return {"ok": True, "action": action, "service": service, "bundle": bundle}


def listapps(udid: str = "") -> list[dict]:
    udid = pick_udid(udid)
    r = _run(SIMCTL + ["listapps", udid], timeout=20)
    text = (r.stdout or b"").decode()
    apps = []
    try:
        data = json.loads(text) if text.strip().startswith("{") else None
    except Exception:
        data = None
    if isinstance(data, dict):
        for bid, info in data.items():
            if not isinstance(info, dict):
                continue
            apps.append(
                {
                    "bundle": bid,
                    "name": info.get("CFBundleDisplayName") or info.get("CFBundleName") or bid,
                    "system": bool(info.get("ApplicationType") == "System"),
                }
            )
    else:
        for m in re.finditer(r'"([^"]+)"\s*=\s*\{[^}]*CFBundleDisplayName\s*=\s*([^;]+);', text):
            apps.append({"bundle": m.group(1), "name": m.group(2).strip().strip('"')})
    apps.sort(key=lambda a: (a.get("system", False), (a.get("name") or "").lower()))
    return apps


def ocr_png(data: bytes) -> list[dict]:
    """Visible text via macOS Vision — no extra install, no focus steal."""
    if not data:
        return []
    try:
        import Vision  # type: ignore
        from Foundation import NSData  # type: ignore
        from Quartz import (  # type: ignore
            CGImageSourceCreateImageAtIndex,
            CGImageSourceCreateWithData,
            kCGImageSourceShouldCache,
        )
    except Exception:
        return []
    ns = NSData.dataWithBytes_length_(data, len(data))
    src = CGImageSourceCreateWithData(ns, {kCGImageSourceShouldCache: False})
    if src is None:
        return []
    image = CGImageSourceCreateImageAtIndex(src, 0, None)
    if image is None:
        return []
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(0)  # accurate
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    ok = handler.performRequests_error_([req], None)
    if not ok:
        return []
    rows = []
    for obs in req.results() or []:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        text = str(cands[0].string())
        conf = float(cands[0].confidence())
        box = obs.boundingBox()
        rows.append(
            {
                "text": text,
                "conf": round(conf, 3),
                "x": round(float(box.origin.x), 3),
                "y": round(float(box.origin.y), 3),
                "w": round(float(box.size.width), 3),
                "h": round(float(box.size.height), 3),
            }
        )
    rows.sort(key=lambda r: (-r["y"], r["x"]))
    return rows


def _rel_luminance(rgb: tuple[int, int, int]) -> float:
    def f(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    l1, l2 = _rel_luminance(a), _rel_luminance(b)
    hi, lo = (l1, l2) if l1 >= l2 else (l2, l1)
    return (hi + 0.05) / (lo + 0.05)


def colors_from_png(data: bytes) -> dict:
    try:
        from AppKit import NSBitmapImageRep  # type: ignore
        from Foundation import NSData  # type: ignore
    except Exception:
        return {"palette": [], "issues": []}
    ns = NSData.dataWithBytes_length_(data, len(data))
    rep = NSBitmapImageRep.imageRepWithData_(ns)
    if rep is None:
        return {"palette": [], "issues": []}
    w, h = int(rep.pixelsWide()), int(rep.pixelsHigh())
    if w < 8 or h < 8:
        return {"palette": [], "issues": []}
    counts: dict[str, int] = {}
    samples: list[tuple[int, int, tuple[int, int, int]]] = []
    steps_x, steps_y = 12, 20
    for iy in range(steps_y):
        for ix in range(steps_x):
            x = int((ix + 0.5) * w / steps_x)
            y = int((iy + 0.5) * h / steps_y)
            c = rep.colorAtX_y_(x, y)
            if c is None:
                continue
            rgb = (
                int(round(float(c.redComponent()) * 255)),
                int(round(float(c.greenComponent()) * 255)),
                int(round(float(c.blueComponent()) * 255)),
            )
            key = f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
            counts[key] = counts.get(key, 0) + 1
            samples.append((x, y, rgb))
    total = sum(counts.values()) or 1
    palette = [
        {"hex": k, "pct": round(100.0 * v / total, 1)}
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:12]
    ]
    issues = []
    for i, (x, y, rgb) in enumerate(samples):
        if i + 1 >= len(samples):
            break
        x2, y2, rgb2 = samples[i + 1]
        if abs(y2 - y) > h / steps_y * 1.5:
            continue
        ratio = contrast_ratio(rgb, rgb2)
        if ratio < 1.15:
            continue
        # Neighbor cells that are "text-like" dark-on-dark or light-on-light
        if 1.15 <= ratio < 3.0:
            issues.append(
                {
                    "type": "low-contrast",
                    "ratio": round(ratio, 2),
                    "a": f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}",
                    "b": f"#{rgb2[0]:02x}{rgb2[1]:02x}{rgb2[2]:02x}",
                    "x": x,
                    "y": y,
                }
            )
    # Keep a handful of the worst
    issues.sort(key=lambda z: z["ratio"])
    return {"palette": palette, "issues": issues[:12], "width": w, "height": h}


def _maestro_env() -> dict:
    java = _java_home()
    path = os.environ.get("PATH") or ""
    maestro = str(Path.home() / ".maestro" / "bin")
    extra = maestro + (":" + str(Path(java) / "bin") if java else "")
    return {"JAVA_HOME": java, "PATH": extra + ":" + path}


def _maestro(args: list[str], timeout: float = 50) -> subprocess.CompletedProcess:
    exe = _which("maestro")
    if not exe:
        raise RuntimeError("maestro not installed")
    if not _java_home():
        raise RuntimeError("Java 17 missing for Maestro (brew install openjdk@17)")
    return _run([exe, *args], timeout=timeout, env=_maestro_env())


def hierarchy(udid: str = "") -> dict:
    udid = pick_udid(udid)
    drv = driver_name()
    if drv == "maestro":
        r = _maestro(["hierarchy"], timeout=40)
        text = (r.stdout or b"").decode()
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).decode()[:600] or "hierarchy failed")
        nodes = _parse_hierarchy_xml(text)
        return {"ok": True, "driver": "maestro", "udid": udid, "nodes": nodes, "raw": text[:12000]}
    if drv == "idb":
        r = _run([_which("idb"), "ui", "describe-all", "--json", "--udid", udid], timeout=40)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).decode()[:400] or "idb describe failed")
        try:
            data = json.loads(r.stdout.decode() or "[]")
        except Exception:
            data = []
        return {"ok": True, "driver": "idb", "udid": udid, "nodes": data}
    # Fallback: OCR of the screen
    png = screenshot(udid)
    texts = ocr_png(png)
    return {
        "ok": True,
        "driver": "ocr",
        "udid": udid,
        "nodes": [{"text": t["text"], "x": t["x"], "y": t["y"]} for t in texts],
        "note": "no Maestro/idb — hierarchy is OCR of the screenshot",
    }


def _parse_hierarchy_xml(text: str) -> list[dict]:
    nodes = []
    try:
        root = ET.fromstring(text)
    except Exception:
        # Maestro sometimes wraps extra logs
        m = re.search(r"<hierarchy[\s\S]+</hierarchy>", text)
        if not m:
            return nodes
        root = ET.fromstring(m.group(0))

    def walk(el: ET.Element) -> None:
        attr = el.attrib or {}
        label = (
            attr.get("text")
            or attr.get("accessibilityText")
            or attr.get("label")
            or attr.get("content-desc")
            or ""
        )
        ident = attr.get("id") or attr.get("resource-id") or attr.get("resourceId") or ""
        node = {
            "class": el.tag,
            "text": label,
            "id": ident,
            "a11y": attr.get("importantForAccessibility") or attr.get("accessible") or "",
            "enabled": attr.get("enabled", ""),
            "bounds": attr.get("bounds") or attr.get("frame") or "",
        }
        if any(node[k] for k in ("text", "id", "bounds")):
            nodes.append(node)
        for ch in list(el):
            walk(ch)

    walk(root)
    return nodes[:400]


def _a11y_issues(nodes: list[dict], texts: list[dict]) -> list[dict]:
    issues = []
    unlabeled = 0
    for n in nodes:
        cls = (n.get("class") or "").lower()
        interactive = any(k in cls for k in ("button", "cell", "switch", "tab", "barbutton", "textfield"))
        if interactive and not (n.get("text") or n.get("id")):
            unlabeled += 1
            issues.append({"type": "missing-label", "class": n.get("class"), "bounds": n.get("bounds")})
    if unlabeled:
        issues.append({"type": "unlabeled-controls", "n": unlabeled})
    if not nodes and not texts:
        issues.append({"type": "empty-screen", "detail": "no accessibility tree and no OCR text"})
    return issues[:30]


def audit(udid: str = "") -> dict:
    udid = pick_udid(udid)
    png = screenshot(udid)
    SHOT_PATH.write_bytes(png)
    texts = ocr_png(png)
    colors = colors_from_png(png)
    ui = {
        "appearance": appearance(udid=udid).get("appearance"),
        "content_size": content_size(udid=udid).get("content_size"),
        "increase_contrast": contrast(udid=udid).get("increase_contrast"),
    }
    tree = {}
    try:
        tree = hierarchy(udid)
    except Exception as exc:
        tree = {"ok": False, "error": str(exc), "nodes": []}
    issues = list(colors.get("issues") or [])
    issues.extend(_a11y_issues(tree.get("nodes") or [], texts))
    return {
        "ok": True,
        "udid": udid,
        "driver": driver_name(),
        "ui": ui,
        "texts": [t["text"] for t in texts],
        "ocr": texts,
        "palette": colors.get("palette") or [],
        "size": {"width": colors.get("width"), "height": colors.get("height")},
        "nodes": (tree.get("nodes") or [])[:120],
        "issues": issues,
        "shot": str(SHOT_PATH),
    }


def _flow_yaml(app: str, steps: list[dict]) -> str:
    lines = [f"appId: {app or '*'}", "---"]
    for s in steps:
        k, v = next(iter(s.items()))
        if isinstance(v, str):
            lines.append(f"- {k}: {json.dumps(v, ensure_ascii=False)}")
        else:
            lines.append(f"- {k}:")
            for kk, vv in v.items():
                lines.append(f"    {kk}: {json.dumps(vv, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def maestro_steps(steps: list[dict], bundle: str = "", udid: str = "") -> dict:
    bundle = bundle or _state.get("bundle") or "*"
    yaml_text = _flow_yaml(bundle, steps)
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    flow = LAB_DIR / "flow.yaml"
    flow.write_text(yaml_text, encoding="utf-8")
    args = ["test", str(flow)]
    if udid:
        args.extend(["--device", udid])
    r = _maestro(args, timeout=90)
    out = (r.stdout or b"").decode()[-2000:]
    err = (r.stderr or b"").decode()[-2000:]
    if r.returncode != 0:
        raise RuntimeError(err or out or "maestro test failed")
    return {"ok": True, "driver": "maestro", "log": out[-800:]}


def tap(label: str = "", x: float | None = None, y: float | None = None, nx: float | None = None, ny: float | None = None, bundle: str = "", udid: str = "") -> dict:
    udid = pick_udid(udid)
    drv = driver_name()
    if drv == "maestro":
        if label:
            return maestro_steps([{"tapOn": label}], bundle=bundle, udid=udid)
        if nx is not None and ny is not None:
            return maestro_steps(
                [{"tapOn": {"point": f"{int(nx * 100)}%,{int(ny * 100)}%"}}],
                bundle=bundle,
                udid=udid,
            )
        if x is not None and y is not None:
            return maestro_steps(
                [{"tapOn": {"point": f"{int(x)},{int(y)}"}}],
                bundle=bundle,
                udid=udid,
            )
        raise RuntimeError("tap needs label or x,y")
    if drv == "idb":
        exe = _which("idb")
        if label:
            r = _run([exe, "ui", "describe-all", "--json", "--udid", udid], timeout=30)
            raise RuntimeError("idb tap-by-label: pass x,y from hierarchy")
        if x is None or y is None:
            raise RuntimeError("tap needs x,y")
        r = _run([exe, "ui", "tap", str(int(x)), str(int(y)), "--udid", udid], timeout=20)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).decode()[:400] or "idb tap failed")
        return {"ok": True, "driver": "idb"}
    raise RuntimeError(
        "no in-sim driver yet. Install Java 17 so Maestro can tap without grabbing the Mac "
        "(brew install openjdk@17). Screenshot/audit/dark-mode already work."
    )


def type_text(text: str, bundle: str = "", udid: str = "") -> dict:
    udid = pick_udid(udid)
    if driver_name() == "maestro":
        return maestro_steps([{"inputText": text}], bundle=bundle, udid=udid)
    if driver_name() == "idb":
        r = _run([_which("idb"), "ui", "text", text, "--udid", udid], timeout=20)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).decode()[:400] or "idb text failed")
        return {"ok": True, "driver": "idb"}
    # Pasteboard + no keypress: still useful
    _run(SIMCTL + ["pbcopy", pick_udid(udid)], input_bytes=text.encode("utf-8"), timeout=10)
    return {"ok": True, "driver": "pasteboard", "note": "text is on the sim pasteboard; Maestro needed to type into a field"}


def swipe(direction: str = "up", bundle: str = "", udid: str = "") -> dict:
    direction = (direction or "up").lower()
    if driver_name() == "maestro":
        return maestro_steps([{"swipe": {"direction": direction}}], bundle=bundle, udid=udid)
    raise RuntimeError("swipe needs Maestro")


def sweep(udid: str = "", appearances: list[str] | None = None, sizes: list[str] | None = None) -> dict:
    """A→Z visual pass: light/dark + Dynamic Type, OCR + contrast each time."""
    udid = pick_udid(udid)
    appearances = appearances or ["light", "dark"]
    sizes = sizes or ["large", "extra-extra-extra-large", "accessibility-large"]
    shots = []
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    for ap in appearances:
        appearance(ap, udid)
        time.sleep(0.35)
        for sz in sizes:
            content_size(sz, udid)
            time.sleep(0.45)
            png = screenshot(udid)
            name = f"{ap}-{sz}.png"
            path = LAB_DIR / name
            path.write_bytes(png)
            texts = ocr_png(png)
            colors = colors_from_png(png)
            shots.append(
                {
                    "name": name,
                    "path": str(path),
                    "appearance": ap,
                    "content_size": sz,
                    "texts": [t["text"] for t in texts],
                    "palette": colors.get("palette") or [],
                    "issues": colors.get("issues") or [],
                }
            )
    appearance("light", udid)
    content_size("large", udid)
    return {"ok": True, "udid": udid, "shots": shots}


def full_test(bundle: str = "", path: str = "", platforms=None) -> dict:
    """Build-ready pass: boot → optional install/launch → audit on each platform."""
    if isinstance(platforms, str):
        plats = ["ios", "android"] if platforms in {"both", "all", ""} else [platforms]
    elif platforms:
        plats = [str(p) for p in platforms]
    else:
        plats = ["ios", "android"]
    results = []
    for plat in plats:
        try:
            booted = boot(platform=plat)
            inst = install(path) if path else None
            launched = launch(bundle) if bundle else None
            time.sleep(0.6)
            results.append(
                {
                    "platform": plat,
                    "ok": True,
                    "boot": booted,
                    "install": inst,
                    "launch": launched,
                    "audit": audit(),
                }
            )
        except Exception as exc:
            results.append({"platform": plat, "ok": False, "error": str(exc)})
    return {"ok": all(r.get("ok") for r in results), "results": results}


def _gate(gid: str, ok: bool, detail: str, blocker: bool = True) -> dict:
    return {"id": gid, "ok": bool(ok), "detail": detail, "blocker": blocker}


def _ios_running(bundle: str, udid: str) -> tuple[bool, str]:
    if not bundle:
        return True, "no bundle"
    r = _run(SIMCTL + ["spawn", udid, "launchctl", "list"], timeout=15)
    blob = (r.stdout or b"").decode(errors="replace")
    token = bundle.rsplit(".", 1)[-1]
    hit = bundle in blob or (token and token in blob)
    return hit, "process listed" if hit else "app not in launchctl list"


def _android_running(bundle: str, udid: str) -> tuple[bool, str]:
    if not bundle:
        return True, "no bundle"
    adb = _adb()
    r = _run([adb, "-s", udid, "shell", "pidof", "-s", bundle], timeout=10)
    pid = (r.stdout or b"").decode().strip().split()
    if pid and pid[0].isdigit():
        return True, f"pid {pid[0]}"
    r2 = _run([adb, "-s", udid, "shell", "pidof", bundle], timeout=10)
    pid = (r2.stdout or b"").decode().strip().split()
    if pid and pid[0].isdigit():
        return True, f"pid {pid[0]}"
    return False, "no process"


def _ios_crash_lines(bundle: str, udid: str) -> list[str]:
    hits: list[str] = []
    r = _run(
        SIMCTL
        + [
            "spawn",
            udid,
            "log",
            "show",
            "--last",
            "45s",
            "--style",
            "compact",
            "--predicate",
            'eventMessage CONTAINS[c] "fatal" OR eventMessage CONTAINS[c] "crash" OR eventMessage CONTAINS[c] "NSException"',
        ],
        timeout=25,
    )
    for ln in (r.stdout or b"").decode(errors="replace").splitlines():
        low = ln.lower()
        if any(k in low for k in ("fatal", "crash", "nsexception", "terminating")):
            if not bundle or bundle.split(".")[-1].lower() in low or "libsystem" in low or "swift" in low:
                hits.append(ln.strip()[:220])
    home = Path.home() / "Library" / "Logs" / "DiagnosticReports"
    if home.is_dir() and bundle:
        needle = bundle.rsplit(".", 1)[-1]
        for p in sorted(home.glob(f"{needle}*.crash"), key=lambda x: x.stat().st_mtime, reverse=True)[:3]:
            if time.time() - p.stat().st_mtime < 600:
                hits.append(f"crashlog {p.name}")
    return hits[:8]


def _android_crash_lines(bundle: str, udid: str) -> list[str]:
    adb = _adb()
    r = _run(
        [adb, "-s", udid, "logcat", "-d", "-t", "400", "AndroidRuntime:E", "FATAL:E", "*:S"],
        timeout=15,
    )
    hits = []
    for ln in (r.stdout or b"").decode(errors="replace").splitlines():
        if any(k in ln for k in ("FATAL EXCEPTION", "AndroidRuntime", "Fatal signal", "ANR in")):
            if not bundle or bundle in ln or "AndroidRuntime" in ln:
                hits.append(ln.strip()[:220])
    return hits[:8]


def _run_repo_tests(repo: str) -> list[dict]:
    """Host unit tests (not the device UI). Skip silently if no project."""
    root = Path(repo).expanduser()
    if not root.is_dir():
        return [_gate("repo", False, f"not a dir: {root}")]
    gates = []
    gradlew = root / "gradlew"
    if not gradlew.is_file():
        for p in root.glob("*/gradlew"):
            gradlew = p
            break
    if gradlew.is_file():
        r = _run(["bash", str(gradlew), "test", "--quiet"], timeout=240)
        ok = r.returncode == 0
        tail = (r.stdout or r.stderr or b"").decode(errors="replace")[-300:]
        gates.append(_gate("android-unit", ok, "gradle test ok" if ok else tail or "gradle test failed"))
    xc = list(root.glob("*.xcodeproj")) + list(root.glob("*/*.xcodeproj"))
    if xc:
        listed = _run(["xcodebuild", "-list", "-json", "-project", str(xc[0])], timeout=40)
        scheme = ""
        try:
            data = json.loads(listed.stdout.decode() or "{}")
            schemes = (data.get("project") or {}).get("schemes") or []
            scheme = next((s for s in schemes if "test" not in s.lower()), schemes[0] if schemes else "")
        except Exception:
            scheme = ""
        if scheme:
            dest = "platform=iOS Simulator,name=Swarm Lab"
            r = _run(
                [
                    "xcodebuild",
                    "test",
                    "-project",
                    str(xc[0]),
                    "-scheme",
                    scheme,
                    "-destination",
                    dest,
                    "-quiet",
                ],
                timeout=300,
            )
            ok = r.returncode == 0
            tail = (r.stdout or r.stderr or b"").decode(errors="replace")[-300:]
            gates.append(_gate("ios-unit", ok, "xcodebuild test ok" if ok else tail or "xcodebuild test failed"))
        else:
            gates.append(_gate("ios-unit", True, "no test scheme", blocker=False))
    if not gates:
        gates.append(_gate("repo-tests", True, "no gradle/xcode tests found", blocker=False))
    return gates


def protocol(
    bundle: str = "",
    ios_path: str = "",
    android_path: str = "",
    repo: str = "",
) -> dict:
    """UI + technical green/red pass. Only when explicitly asked. Submit illegal while red."""
    gates = []
    h = health()
    gates.append(_gate("lab", bool(h.get("ok")), "device lab up" if h.get("ok") else str(h.get("error") or "lab down")))
    ios_udid = ""
    try:
        boot(platform="ios")
        ios_udid = _state.get("udid") or pick_udid()
        if ios_path:
            install(ios_path)
        if bundle:
            launch(bundle)
        time.sleep(0.8)
        ia = audit()
        texts = ia.get("texts") or []
        unlabeled = sum(
            1
            for x in (ia.get("issues") or [])
            if x.get("type") in {"missing-label", "unlabeled-controls", "empty-screen"}
        )
        ios_ok = bool(texts) or bool(ia.get("nodes"))
        gates.append(_gate("ios-boot", True, f"udid {ia.get('udid') or ios_udid}"))
        gates.append(_gate("ios-screen", ios_ok, f"{len(texts)} OCR strings, {len(ia.get('nodes') or [])} a11y nodes"))
        gates.append(
            _gate(
                "ios-a11y",
                unlabeled == 0,
                "no unlabeled controls" if unlabeled == 0 else f"{unlabeled} unlabeled",
                blocker=False,
            )
        )
        if bundle:
            alive, detail = _ios_running(bundle, ios_udid)
            gates.append(_gate("ios-alive", alive, detail))
            crashes = _ios_crash_lines(bundle, ios_udid)
            gates.append(_gate("ios-nocrash", not crashes, "no crash logs" if not crashes else crashes[0][:180]))
        try:
            appearance("dark")
            time.sleep(0.3)
            screenshot()
            appearance("light")
            gates.append(_gate("ios-dark", True, "dark mode screenshot ok", blocker=False))
        except Exception as exc:
            gates.append(_gate("ios-dark", False, str(exc)[:160], blocker=False))
    except Exception as exc:
        gates.append(_gate("ios-boot", False, str(exc)[:240]))
        gates.append(_gate("ios-screen", False, "skipped"))
    try:
        boot(platform="android")
        and_udid = pick_udid("android")
        if android_path:
            install(android_path)
        elif ios_path and str(ios_path).endswith(".apk"):
            install(ios_path)
        if bundle:
            try:
                launch(bundle)
            except Exception as exc:
                gates.append(_gate("android-launch", False, str(exc)[:200]))
        time.sleep(1.0)
        aa = audit()
        texts = aa.get("texts") or []
        and_ok = bool((aa.get("size") or {}).get("width")) or bool(texts)
        gates.append(_gate("android-boot", True, f"udid {and_udid}"))
        gates.append(_gate("android-screen", and_ok, f"{len(texts)} OCR strings"))
        if bundle:
            alive, detail = _android_running(bundle, and_udid)
            gates.append(_gate("android-alive", alive, detail))
            crashes = _android_crash_lines(bundle, and_udid)
            gates.append(_gate("android-nocrash", not crashes, "no FATAL/ANR" if not crashes else crashes[0][:180]))
    except Exception as exc:
        gates.append(_gate("android-boot", False, str(exc)[:240]))
        gates.append(_gate("android-screen", False, "skipped"))
    if repo:
        gates.extend(_run_repo_tests(repo))
    blockers = [g["id"] for g in gates if g.get("blocker") and not g.get("ok")]
    green = not blockers
    report = {
        "ok": True,
        "green": green,
        "submit_ready": green,
        "bundle": bundle,
        "repo": repo,
        "gates": gates,
        "blockers": blockers,
        "score": sum(1 for g in gates if g.get("ok")),
        "max": len(gates),
        "at": _now(),
        "note": "UI + process/crash + optional unit tests. Only when you run protocol. Submit stays Tim-gated.",
    }
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    dest = LAB_DIR / "protocol.json"
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["path"] = str(dest)
    return report


def ship(confirm: str = "") -> dict:
    """Store submit. Refuses unless the last protocol is green AND confirm is SUBMIT."""
    path = LAB_DIR / "protocol.json"
    if not path.is_file():
        return {"ok": False, "error": "no protocol.json — run glab protocol first"}
    try:
        rep = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"ok": False, "error": "protocol.json unreadable"}
    if not rep.get("green"):
        return {
            "ok": False,
            "error": "protocol red — fix blockers, re-run glab protocol",
            "blockers": rep.get("blockers") or [],
            "path": str(path),
        }
    if (confirm or "").strip().upper() != "SUBMIT":
        return {
            "ok": True,
            "green": True,
            "submit_ready": True,
            "submitted": False,
            "next": "Protocol is green. Tim must say SUBMIT (or glab ship --submit) to push App Store + Play.",
            "path": str(path),
        }
    # Standing lock: Play identity and App Store submits are Tim-gated in shared memory.
    packet = LAB_DIR / "SHIP.md"
    packet.write_text(
        "# Ship packet\n\n"
        "Protocol was green. Do **not** upload until Tim said SUBMIT in Swarm.\n\n"
        "- iOS: archive for iphoneos (not simulator), upload via Apps bot / Transporter / existing TestFlight script.\n"
        "- Play: AAB + Play Console. Blocked while Play identity is not green.\n"
        f"- Report: {path}\n",
        encoding="utf-8",
    )
    return {
        "ok": True,
        "green": True,
        "submitted": False,
        "held": True,
        "reason": "Store upload is Tim-gated (Play identity / App Review). Packet written.",
        "path": str(packet),
    }


def health() -> dict:
    drv = driver_name()
    _state["driver"] = drv
    _state["ok"] = True
    try:
        devs = devices()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "driver": drv}
    return {
        "ok": True,
        "driver": drv,
        "maestro": bool(_which("maestro")),
        "java": bool(_java_home()),
        "idb": bool(_which("idb")),
        "adb": bool(_which("adb")),
        "emulator": bool(Path(_emulator_bin()).exists()) if _emulator_bin() else False,
        "avds": list_avds(),
        "udid": _state.get("udid") or "",
        "name": _state.get("name") or "",
        "platform": _state.get("platform") or "",
        "bundle": _state.get("bundle") or "",
        "devices": devs,
        "cmd": _state.get("cmd") or "",
    }


def dispatch(cmd: str, body: dict) -> dict:
    udid = str(body.get("udid") or body.get("device") or "")
    bundle = str(body.get("bundle") or body.get("app") or "")
    with _lock:
        _mark(cmd, True)
        try:
            if cmd == "boot":
                return boot(
                    udid,
                    hide=body.get("hide", True) is not False,
                    platform=str(body.get("platform") or ""),
                )
            if cmd == "test":
                return full_test(
                    bundle=bundle,
                    path=str(body.get("path") or ""),
                    platforms=body.get("platforms") or body.get("platform"),
                )
            if cmd == "protocol":
                return protocol(
                    bundle=bundle,
                    ios_path=str(body.get("ios_path") or body.get("ios") or ""),
                    android_path=str(body.get("android_path") or body.get("apk") or body.get("path") or ""),
                    repo=str(body.get("repo") or ""),
                )
            if cmd == "ship":
                return ship(str(body.get("confirm") or body.get("submit") or ""))
            if cmd == "shutdown":
                return shutdown(udid)
            if cmd == "install":
                return install(str(body.get("path") or ""), udid)
            if cmd == "launch":
                return launch(bundle, udid)
            if cmd == "terminate":
                return terminate(bundle, udid)
            if cmd == "openurl":
                return openurl(str(body.get("url") or ""), udid)
            if cmd == "appearance":
                return appearance(str(body.get("value") or body.get("appearance") or ""), udid)
            if cmd == "content-size" or cmd == "contentsize":
                return content_size(str(body.get("value") or body.get("size") or ""), udid)
            if cmd == "contrast":
                val = body.get("enabled")
                if isinstance(val, str):
                    val = val.lower() in {"1", "true", "enabled", "on"}
                return contrast(val if isinstance(val, bool) else None, udid)
            if cmd == "privacy":
                return privacy(str(body.get("action") or "grant"), str(body.get("service") or "camera"), bundle, udid)
            if cmd == "apps":
                return {"ok": True, "apps": listapps(udid)}
            if cmd == "hierarchy" or cmd == "a11y":
                return hierarchy(udid)
            if cmd == "audit":
                return audit(udid)
            if cmd == "sweep":
                return sweep(udid, body.get("appearances"), body.get("sizes"))
            if cmd == "tap":
                return tap(
                    label=str(body.get("label") or body.get("text") or ""),
                    x=body.get("x"),
                    y=body.get("y"),
                    nx=body.get("nx"),
                    ny=body.get("ny"),
                    bundle=bundle,
                    udid=udid,
                )
            if cmd == "type":
                return type_text(str(body.get("text") or ""), bundle=bundle, udid=udid)
            if cmd == "swipe":
                return swipe(str(body.get("direction") or body.get("dir") or "up"), bundle=bundle, udid=udid)
            raise RuntimeError(f"unknown cmd {cmd}")
        finally:
            _mark(cmd, False)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print("lab", fmt % args, flush=True)

    def _send(self, status: int, ctype: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj: dict, status: int = 200) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self._send(status, "application/json; charset=utf-8", raw)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode() or "{}")
        except Exception:
            return {}

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path in {"/", "/health"}:
                self._json(health())
                return
            if path == "/devices":
                self._json({"ok": True, "devices": devices(), "driver": driver_name()})
                return
            if path == "/shot":
                png = screenshot()
                self._send(200, "image/png", png)
                return
            if path == "/audit":
                self._json(audit())
                return
            if path in {"/a11y", "/hierarchy"}:
                self._json(hierarchy())
                return
            self._json({"ok": False, "error": "not found"}, 404)
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, 400)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].strip("/")
        body = self._body()
        cmd = path.split("/")[-1] if path else ""
        try:
            self._json(dispatch(cmd, body))
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, 400)


def main() -> None:
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    _state["driver"] = driver_name()
    print(f"→ device lab on :{PORT}  driver={_state['driver']}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

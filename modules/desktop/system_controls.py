"""
modules/desktop/system_controls.py

Windows controls for hands-free use:

    lock the PC                       (immediately)
    sleep / restart / shut down        (always asks; restart and shutdown wait 60 s
                                        so "cancel the shutdown" can stop them)
    mute / unmute my mic
    set Discord to 30% / mute the game  (per-app volume, Windows' volume mixer)
    set the volume to 40%              (whole system)
    switch audio to my headphones      (default output device)
    brightness 60%                     (laptop screens only; most desktop monitors refuse)
    do not disturb                     (Windows has no API: opens its settings)
    take a screenshot                  (saved to your Pictures\\Screenshots folder)

Audio uses pycaw (Windows Core Audio through comtypes).
"""

import ctypes
import difflib
import logging
import os
import re
import subprocess
import time
from typing import Dict, List, Optional

from modules.automation.tools import P, PermissionLevel, Tool, ToolError

log = logging.getLogger("saint.desktop")
_NO_WINDOW = 0x08000000


def _com():
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass


def _pycaw():
    try:
        _com()
        from pycaw.pycaw import AudioUtilities
        return AudioUtilities
    except ImportError:
        raise ToolError("Audio control needs the pycaw package (pip install pycaw).", "UNSUPPORTED")


# ---------------------------------------------------------------------- #
# Power
# ---------------------------------------------------------------------- #
def lock():
    if not ctypes.windll.user32.LockWorkStation():
        raise ToolError("Windows didn't lock.", "FAILED")
    return {"locked": True}


def power(action: str):
    action = (action or "").lower()
    if action == "sleep":
        # Returns when the PC wakes up again.
        ctypes.windll.powrprof.SetSuspendState(False, True, False)
        return {"done": "sleep"}
    if action == "hibernate":
        ctypes.windll.powrprof.SetSuspendState(True, True, False)
        return {"done": "hibernate"}
    flag = {"restart": "/r", "shutdown": "/s", "sign_out": "/l"}.get(action)
    if not flag:
        raise ToolError("I can sleep, hibernate, restart, shut down or sign out.", "INVALID")
    args = ["shutdown", flag] + (["/t", "60"] if action != "sign_out" else [])
    subprocess.run(args, creationflags=_NO_WINDOW, check=False)
    return {"done": action, "delay": 60 if action != "sign_out" else 0}


def cancel_shutdown():
    r = subprocess.run(["shutdown", "/a"], creationflags=_NO_WINDOW, capture_output=True, text=True)
    if r.returncode != 0:
        return {"cancelled": False}
    return {"cancelled": True}


# ---------------------------------------------------------------------- #
# Audio
# ---------------------------------------------------------------------- #
def _endpoint(kind: str):
    AU = _pycaw()
    if kind == "mic":
        dev = AU.GetMicrophone()
        if dev is None:
            raise ToolError("I can't find a microphone.", "NOT_FOUND")
        return AU.CreateDevice(dev).EndpointVolume
    return AU.GetSpeakers().EndpointVolume


def mic_mute(state: str = "toggle"):
    from modules.desktop.voicemeeter import voicemeeter
    if voicemeeter.available():
        # With Voicemeeter in between, the mic is its input strip, not the Windows device.
        return voicemeeter.mute_mic(state)
    ep = _endpoint("mic")
    cur = bool(ep.GetMute())
    new = (not cur) if state == "toggle" else state in ("on", "mute", "true")
    ep.SetMute(int(new), None)
    return {"muted": new}


def system_volume(percent: Optional[int] = None, mute: str = ""):
    ep = _endpoint("speakers")
    if mute:
        cur = bool(ep.GetMute())
        new = (not cur) if mute == "toggle" else mute == "on"
        ep.SetMute(int(new), None)
        return {"muted": new}
    if percent is None:
        return {"percent": round(ep.GetMasterVolumeLevelScalar() * 100)}
    ep.SetMasterVolumeLevelScalar(max(0, min(100, int(percent))) / 100.0, None)
    return {"percent": int(percent)}


_APP_ALIASES = {"discord": ["discord"], "spotify": ["spotify"], "chrome": ["chrome"], "opera": ["opera"],
                "browser": ["opera", "chrome", "msedge", "firefox", "brave"], "youtube": ["opera", "chrome", "msedge",
                                                                                  "firefox", "brave"],
                "steam": ["steam", "steamwebhelper"], "game": [], "the game": []}


def _sessions():
    AU = _pycaw()
    out = []
    for s in AU.GetAllSessions():
        try:
            name = (s.Process.name() if s.Process else "") or ""
        except Exception:
            name = ""
        out.append((name.lower().replace(".exe", ""), (s.DisplayName or "").lower(), s))
    return out


def _find_sessions(app: str) -> List:
    app = (app or "").strip().lower()
    sessions = [x for x in _sessions() if x[0]]
    if not sessions:
        return []
    if app in ("game", "the game", "my game"):
        # The app in front, if it's playing audio (full-screen games are).
        try:
            from modules.desktop.controller import desktop
            w = desktop.target_window()
            stem = (w.process or "").lower().replace(".exe", "") if w else ""
            return [s for n, _d, s in sessions if n == stem]
        except Exception:
            return []
    names = _APP_ALIASES.get(app, [app])
    hits = [s for n, d, s in sessions if any(n.startswith(x) or x in d for x in names)]
    if hits:
        return hits
    close = difflib.get_close_matches(app, [n for n, _d, _s in sessions], n=1, cutoff=0.75)
    return [s for n, _d, s in sessions if close and n == close[0]]


def app_volume(app: str, percent: Optional[int] = None, mute: str = "", step: int = 0):
    hits = _find_sessions(app)
    if not hits:
        playing = sorted({n for n, _d, _s in _sessions() if n})
        extra = f" Apps with sound right now: {', '.join(playing[:6])}." if playing else ""
        raise ToolError(f"{app} isn't playing any sound right now.{extra}", "NOT_FOUND")
    result = None
    for s in hits:
        vol = s.SimpleAudioVolume
        if mute:
            cur = bool(vol.GetMute())
            new = (not cur) if mute == "toggle" else mute == "on"
            vol.SetMute(int(new), None)
            result = {"app": app, "muted": new}
        else:
            level = vol.GetMasterVolume()
            if percent is not None:
                level = max(0, min(100, int(percent))) / 100.0
            elif step:
                level = max(0.0, min(1.0, level + step / 100.0))
            vol.SetMasterVolume(level, None)
            result = {"app": app, "percent": round(level * 100)}
    return result


# What people call a kind of device -> words in Windows' device names, best first.
# (A wireless headset often shows up as "Speakers (HyperX ...)"; "Headphones (Oculus
# Virtual Audio Device)" is a VR driver, not your headphones.)
_KINDS = {
    "headset": [r"hyperx|cloud|arctis|steelseries|razer|corsair|astro|logitech g|headset|airpods|buds|wh-1000|"
                r"jabra|sennheiser|beyerdynamic", r"headphones?(?!.*(?:oculus|virtual))", r"wireless"],
    "monitor": [r"nvidia high definition|display audio|amd high definition|intel.*display|hdmi|displayport"],
    "speakers": [r"^speakers \((?!.*(?:hyperx|cloud|arctis|headset|wireless))", r"realtek", r"speakers"],
}
_KIND_WORDS = {"headphones": "headset", "headphone": "headset", "headset": "headset", "earbuds": "headset",
               "buds": "headset", "monitor": "monitor", "screen": "monitor", "tv": "monitor", "display": "monitor",
               "speakers": "speakers", "speaker": "speakers", "pc speakers": "speakers"}


def match_output(spoken: str, names: List[str]) -> List[str]:
    """Device names that fit what was said, best first."""
    q = re.sub(r"^(?:my|the)\s+", "", (spoken or "").lower().strip())
    q = re.sub(r"\s+(?:please|instead)$", "", q)
    kind = _KIND_WORDS.get(q)
    if kind:
        for pattern in _KINDS[kind]:
            hits = [n for n in names if re.search(pattern, n.lower()) and "voicemeeter" not in n.lower()]
            if hits:
                return hits
    words = [w for w in re.findall(r"[a-z0-9]+", q)] or [q]
    hits = [n for n in names if all(w in n.lower() for w in words)]
    return hits or [n for n in names if any(w in n.lower() for w in words if len(w) > 2)]


def output_device(name: str):
    from modules.desktop.voicemeeter import voicemeeter
    if voicemeeter.available():
        # Windows plays into Voicemeeter; the real devices are its A-buses.
        return voicemeeter.route_to(name)
    AU = _pycaw()
    # data_flow 0 = playback devices, device_state 1 = active (plugged in and enabled)
    render = [d for d in AU.GetAllDevices(data_flow=0, device_state=1) if d.FriendlyName]
    hits = match_output(name, [d.FriendlyName for d in render])
    hits = [d for h in hits for d in render if d.FriendlyName == h]
    if not hits:
        raise ToolError(f"I couldn't find an audio device called {name}. I can see: "
                        + ", ".join(d.FriendlyName.split(" (")[0] for d in render[:6]) + ".", "NOT_FOUND")
    dev = hits[0]
    try:
        from pycaw.constants import ERole
        AU.SetDefaultDevice(dev.id, roles=[ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])
    except Exception as e:
        log.info("audio.output_switch_failed %s", e)
        os.startfile("ms-settings:sound")
        raise ToolError(f"Windows didn't let me switch to {dev.FriendlyName}; I opened Sound settings.", "FAILED")
    return {"device": dev.FriendlyName}


# ---------------------------------------------------------------------- #
# Display, focus, screenshots
# ---------------------------------------------------------------------- #
def brightness(percent: Optional[int] = None, step: int = 0):
    ps = ("$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction Stop | "
          "Select-Object -First 1; $m.CurrentBrightness")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                       creationflags=_NO_WINDOW, timeout=15)
    if r.returncode != 0 or not r.stdout.strip().isdigit():
        raise ToolError("Your screen doesn't let Windows change its brightness — that works on laptop screens; "
                        "for a desktop monitor use its own buttons.", "UNSUPPORTED")
    cur = int(r.stdout.strip())
    target = percent if percent is not None else max(0, min(100, cur + step))
    set_ps = ("Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | "
              f"Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{{Timeout=1; Brightness={int(target)}}}")
    subprocess.run(["powershell", "-NoProfile", "-Command", set_ps], capture_output=True, creationflags=_NO_WINDOW,
                   timeout=15)
    return {"percent": int(target)}


def do_not_disturb():
    os.startfile("ms-settings:notifications")
    return {"opened": "notification settings"}


def screenshot(target: str = ""):
    """All screens, one screen ("my left screen") or one window ("Claude")."""
    from core.game_mode import game_mode
    refused = game_mode.refuse_capture()      # no screen grabs while a game runs
    if refused:
        raise ToolError(refused, "GAME_MODE")
    from PIL import ImageGrab
    from modules.files.paths import known_folder
    from modules.desktop.controller import desktop
    folder = os.path.join(known_folder("pictures") or os.path.expanduser("~\\Pictures"), "Screenshots")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, time.strftime("SAINT %Y-%m-%d %H%M%S.png"))
    target = (target or "").strip()
    what, bbox = "all screens", None
    m = re.match(r"^(?:my|the)?\s*(\w+)\s+(?:screen|monitor|display)$", target, re.I)
    if m:
        mon = desktop.resolve_monitor(m.group(1), 1)
        bbox, what = (mon.left, mon.top, mon.right, mon.bottom), desktop.monitor_label(mon)
    elif target and target.lower() not in ("all", "everything", "both screens", "all screens"):
        w = desktop.find_window(target)
        if w.minimized:
            raise ToolError(f"{target} is minimized, so there's nothing to capture.", "NOT_VISIBLE")
        desktop._activate(w)
        time.sleep(0.3)
        w = desktop._info(w.hwnd)
        from modules.vision.screen import app_label
        bbox, what = (w.left, w.top, w.left + w.width, w.top + w.height), app_label(
            {"title": w.title, "process": w.process})
    img = ImageGrab.grab(bbox=bbox, all_screens=True)
    img.save(path)
    return {"path": path, "what": what}


def register_system_tools(registry):
    tools = [
        Tool("system.lock", "Lock the PC", {}, PermissionLevel.MEDIUM, lock, parameters={},
             llm_exposed=True, category="system"),
        # Always asks (ALWAYS_CONFIRM); never offered to the LLM.
        Tool("system.power", "Sleep, hibernate, restart, shut down or sign out", {"action": "string"},
             PermissionLevel.HIGH, power,
             parameters={"action": P("string", enum=["sleep", "hibernate", "restart", "shutdown", "sign_out"])},
             category="system"),
        Tool("system.cancel_shutdown", "Cancel a pending restart or shutdown", {}, PermissionLevel.LOW,
             cancel_shutdown, parameters={}, llm_exposed=True, category="system"),
        Tool("audio.mic_mute", "Mute or unmute the microphone", {"state": "string"}, PermissionLevel.MEDIUM,
             mic_mute, parameters={"state": P("string", enum=["on", "off", "toggle"], required=False,
                                              default="toggle")}, llm_exposed=True, category="audio"),
        Tool("audio.system_volume", "Set the whole PC's volume (0-100) or mute it", {"percent": "integer"},
             PermissionLevel.LOW, system_volume,
             parameters={"percent": P("integer", required=False, minimum=0, maximum=100),
                         "mute": P("string", required=False, default="", enum=["", "on", "off", "toggle"])},
             llm_exposed=True, category="audio"),
        Tool("audio.app_volume", "Set one app's volume in the Windows volume mixer (e.g. Discord 30%)",
             {"app": "string"}, PermissionLevel.LOW, app_volume,
             parameters={"app": P("string", "app name, or 'game' for the app in front"),
                         "percent": P("integer", required=False, minimum=0, maximum=100),
                         "mute": P("string", required=False, default="", enum=["", "on", "off", "toggle"]),
                         "step": P("integer", "relative change, e.g. -20", required=False, default=0)},
             llm_exposed=True, category="audio"),
        Tool("audio.output_device", "Switch the default audio output (speakers, headphones, a monitor...)",
             {"name": "string"}, PermissionLevel.MEDIUM, output_device, parameters={"name": P("string")},
             llm_exposed=True, category="audio"),
        Tool("display.brightness", "Set screen brightness (laptop screens)", {"percent": "integer"},
             PermissionLevel.LOW, brightness,
             parameters={"percent": P("integer", required=False, minimum=0, maximum=100),
                         "step": P("integer", required=False, default=0)}, llm_exposed=True, category="system"),
        Tool("system.do_not_disturb", "Open Windows' Do Not Disturb settings", {}, PermissionLevel.LOW,
             do_not_disturb, parameters={}, llm_exposed=True, category="system"),
        Tool("system.screenshot", "Save a screenshot (all screens, one screen, or one window) to "
             "Pictures\\Screenshots", {}, PermissionLevel.LOW, screenshot,
             parameters={"target": P("string", "a window ('Claude') or screen ('left screen'); empty = all",
                                     required=False, default="")}, llm_exposed=True, category="system"),
    ]
    for t in tools:
        registry.register(t)

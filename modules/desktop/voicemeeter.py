"""
modules/desktop/voicemeeter.py

Voicemeeter (VB-Audio) through its Remote API, for PCs where Voicemeeter sits
between Windows and the real devices. There, muting the Windows mic or
changing the default output does the wrong thing; the user means:

    "mute my mic"                  mute the input strip the microphone is on
                                   (Stereo Input 1 by default)
    "switch audio to my headphones"  route every strip that plays on the
    "use my speakers"                speakers' A-bus to the headphones' A-bus
                                     instead (A2 -> A1), and back

Which bus is which comes from the device each A-bus plays on ("Speakers
(HyperX Cloud ...)" is the headset). Settings: audio.voicemeeter.* —
"enabled" (auto/on/off), "mic_strip" (-1 = find the strip with a microphone).
"""

import atexit
import ctypes
import logging
import os
import re
import threading
import time
from typing import Dict, List, Optional

from core.config import config
from modules.automation.tools import ToolError

log = logging.getLogger("saint.desktop")

_DLL_DIRS = [r"C:\Program Files (x86)\VB\Voicemeeter", r"C:\Program Files\VB\Voicemeeter"]
# Voicemeeter type -> (strips, A-buses)       1 = Voicemeeter, 2 = Banana, 3 = Potato
_LAYOUT = {1: (3, 1), 2: (5, 3), 3: (8, 5)}
_HEADSET = re.compile(r"hyperx|cloud|arctis|headset|headphone|earbud|buds|airpods|blackshark|kraken|g pro|astro", re.I)


class Voicemeeter:
    def __init__(self):
        self._lock = threading.Lock()
        self._dll = None
        self._logged_in = False

    # ------------------------------------------------------------------ #
    def running(self) -> bool:
        try:
            import psutil
            return any((p.info.get("name") or "").lower().startswith("voicemeeter")
                       for p in psutil.process_iter(["name"]))
        except Exception:
            return False

    def available(self) -> bool:
        mode = str(config.get("audio.voicemeeter.enabled", "auto")).lower()
        if mode in ("off", "false", "no") or os.name != "nt":
            return False
        return self._find_dll() is not None and (mode in ("on", "true", "yes") or self.running())

    @staticmethod
    def _find_dll() -> Optional[str]:
        name = "VoicemeeterRemote64.dll" if ctypes.sizeof(ctypes.c_void_p) == 8 else "VoicemeeterRemote.dll"
        for d in _DLL_DIRS:
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
        return None

    def _api(self):
        if self._dll is None:
            path = self._find_dll()
            if path is None:
                raise ToolError("Voicemeeter's remote control isn't installed.", "UNSUPPORTED")
            self._dll = ctypes.WinDLL(path)
        if not self._logged_in:
            rc = self._dll.VBVMR_Login()
            if rc < 0:
                raise ToolError("I couldn't connect to Voicemeeter.", "FAILED")
            self._logged_in = True
            atexit.register(self._logout)
            time.sleep(0.1)
        self._dll.VBVMR_IsParametersDirty()          # refresh before reading
        return self._dll

    def _logout(self):
        try:
            if self._dll is not None and self._logged_in:
                self._dll.VBVMR_Logout()
        except Exception:
            pass
        self._logged_in = False

    def get(self, param: str) -> float:
        v = ctypes.c_float()
        if self._api().VBVMR_GetParameterFloat(param.encode(), ctypes.byref(v)) != 0:
            raise ToolError(f"Voicemeeter doesn't have {param}.", "FAILED")
        return float(v.value)

    def get_str(self, param: str) -> str:
        buf = ctypes.create_unicode_buffer(512)
        if self._api().VBVMR_GetParameterStringW(param.encode(), buf) != 0:
            return ""
        return buf.value

    def set(self, param: str, value: float):
        if self._api().VBVMR_SetParameterFloat(param.encode(), ctypes.c_float(value)) != 0:
            raise ToolError(f"Voicemeeter didn't accept {param}.", "FAILED")

    def layout(self):
        t = ctypes.c_long()
        self._api().VBVMR_GetVoicemeeterType(ctypes.byref(t))
        return _LAYOUT.get(int(t.value), (3, 1))

    # ------------------------------------------------------------------ #
    def _mic_strip(self) -> int:
        configured = int(config.get("audio.voicemeeter.mic_strip", -1))
        if configured >= 0:
            return configured
        strips, _ = self.layout()
        for i in range(min(strips, 3)):                       # hardware inputs come first
            if re.search(r"mic|microphone|headset|usb audio", self.get_str(f"Strip[{i}].device.name"), re.I):
                return i
        return 0

    def mute_mic(self, state: str = "toggle") -> Dict:
        with self._lock:
            i = self._mic_strip()
            cur = self.get(f"Strip[{i}].Mute") >= 0.5
            new = (not cur) if state == "toggle" else state in ("on", "mute", "true")
            self.set(f"Strip[{i}].Mute", 1.0 if new else 0.0)
            label = self.get_str(f"Strip[{i}].Label") or f"Stereo Input {i + 1}"
        log.info("voicemeeter.mic strip=%d muted=%s", i, new)
        return {"muted": new, "via": "Voicemeeter", "strip": label}

    def buses(self) -> List[Dict]:
        _, a = self.layout()
        return [{"bus": f"A{i + 1}", "index": i, "device": self.get_str(f"Bus[{i}].device.name")} for i in range(a)]

    def pick_bus(self, spoken: str) -> Optional[Dict]:
        """The A-bus for 'headphones' / 'speakers' / 'A1' / a device name."""
        q = re.sub(r"^(?:my|the)\s+", "", (spoken or "").lower()).strip()
        buses = [b for b in self.buses() if b["device"]]
        m = re.fullmatch(r"a\s?([1-5])", q)
        if m:
            return next((b for b in buses if b["index"] == int(m.group(1)) - 1), None)
        if re.search(r"head|ear|buds|headset", q):
            return next((b for b in buses if _HEADSET.search(b["device"])), None)
        if re.search(r"speaker|monitor|screen|tv|display", q):
            return next((b for b in buses if not _HEADSET.search(b["device"])), None)
        words = [w for w in re.findall(r"[a-z0-9]+", q) if len(w) > 2]
        return next((b for b in buses if words and all(w in b["device"].lower() for w in words)), None)

    def route_to(self, spoken: str) -> Dict:
        """Move everything that plays on the other A-bus(es) to the one named."""
        with self._lock:
            target = self.pick_bus(spoken)
            if target is None:
                names = ", ".join(f"{b['bus']} ({b['device'].split(' (')[0]})" for b in self.buses() if b["device"])
                raise ToolError(f"I couldn't tell which Voicemeeter output is {spoken}. I see {names}.", "NOT_FOUND")
            strips, a = self.layout()
            tb = target["bus"]
            moved = 0
            for s in range(strips):
                others = [f"A{i + 1}" for i in range(a) if f"A{i + 1}" != tb and self.get(f"Strip[{s}].A{i + 1}") >= 0.5]
                if not others:
                    continue
                self.set(f"Strip[{s}].{tb}", 1.0)
                for o in others:
                    self.set(f"Strip[{s}].{o}", 0.0)
                moved += 1
        device = target["device"].split(" (")[-1].rstrip(")") if "(" in target["device"] else target["device"]
        log.info("voicemeeter.route to=%s strips=%d", tb, moved)
        return {"device": f"{device} ({tb})", "moved": moved, "via": "Voicemeeter"}


voicemeeter = Voicemeeter()

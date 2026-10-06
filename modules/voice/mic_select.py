"""
modules/voice/mic_select.py

Which microphone SAINT opens.

The setting is the device's *name* (``voice.mic_device``). It used to be a
PortAudio index, and indexes move whenever Windows adds a device: connect
AirPods and "#2" became their hands-free microphone, which forces every
Bluetooth headset into call mode — mono, telephone-quality sound for
everything, music included (2026-10-06, "when my AirPods are connected the
audio sucks"). So:

* a name is looked up each time the mic opens (an old number is converted
  to the name it has *now* when that name is safe);
* a Bluetooth headset's microphone is never opened unless the user picked
  it by name or allowed it (``voice.allow_bluetooth_mic``) — another mic is
  used instead, and the reason is reported once.
"""

import re
from typing import Iterable, List, Optional, Tuple, Union

# Windows names a Bluetooth headset's hands-free endpoint "Headset (<name> Hands-Free…)" or
# "<name> Hands-Free AG Audio"; MME cuts names at 31 characters, so the brand is checked too.
_BT = re.compile(r"hands[- ]?free|\bairpods\b|bluetooth|bthhfenum|\bhf audio\b|^headset \(", re.I)
_NOT_A_MIC = re.compile(r"sound mapper|primary sound capture|stereo mix|what u hear|wave out mix", re.I)


def is_bluetooth_headset(name: str) -> bool:
    return bool(_BT.search(name or ""))


def _inputs(devices: Iterable[dict], hostapi: Optional[int]) -> List[Tuple[int, str]]:
    out = []
    for i, d in enumerate(devices):
        if int(d.get("max_input_channels") or 0) <= 0:
            continue
        if hostapi is not None and int(d.get("hostapi", 0)) != hostapi:
            continue
        out.append((i, str(d.get("name") or "")))
    return out


def _fallback(devices: List[dict], default: Optional[int], allow_bluetooth: bool) -> Tuple[Optional[int], str]:
    """The default input when it's usable, else the first real, non-Bluetooth microphone."""
    if default is not None and 0 <= default < len(devices):
        name = str(devices[default].get("name") or "")
        if int(devices[default].get("max_input_channels") or 0) > 0 and (allow_bluetooth or not is_bluetooth_headset(name)):
            return default, name
    hostapi = int(devices[default].get("hostapi", 0)) if default is not None and 0 <= default < len(devices) else 0
    for i, name in _inputs(devices, hostapi):
        if not _NOT_A_MIC.search(name) and (allow_bluetooth or not is_bluetooth_headset(name)):
            return i, name
    return (default, str(devices[default].get("name") or "")) if default is not None and 0 <= default < len(devices) \
        else (None, "")


def pick_input(setting: Union[str, int, None], devices: List[dict], default: Optional[int] = None,
               allow_bluetooth: bool = False) -> Tuple[Optional[int], str, str]:
    """(device index for sounddevice — None means PortAudio's default —, its name, a note for the
    user when SAINT had to use a different mic than the setting asked for, else "")."""
    devices = list(devices)
    if isinstance(setting, str) and setting.strip():
        want = setting.strip()
        named = [(i, n) for i, n in _inputs(devices, None) if n == want] or \
                [(i, n) for i, n in _inputs(devices, None) if n.lower().startswith(want.lower()[:28])]
        if named:
            i, n = named[0]
            return i, n, ""                       # picked by name: the user's choice, Bluetooth or not
        i, n = _fallback(devices, default, allow_bluetooth)
        return i, n, f"Your microphone “{want}” isn't connected, so I'm listening on {n or 'the default mic'}."
    if isinstance(setting, int) and not isinstance(setting, bool) and 0 <= setting < len(devices) \
            and int(devices[setting].get("max_input_channels") or 0) > 0:
        name = str(devices[setting].get("name") or "")
        if allow_bluetooth or not is_bluetooth_headset(name):
            return setting, name, ""
        i, n = _fallback(devices, default, allow_bluetooth)
        return i, n, (f"I didn't use {name} (your AirPods / Bluetooth headset microphone): it puts them in call "
                      f"mode and makes all audio sound bad. Listening on {n or 'the default mic'} — pick a mic in "
                      f"Settings › Voice.")
    i, n = _fallback(devices, default, allow_bluetooth)
    if default is not None and i != default and 0 <= default < len(devices):
        bad = str(devices[default].get("name") or "")
        return i, n, (f"Windows' default microphone is {bad} (a Bluetooth headset): using it would drop your "
                      f"headphones to call quality, so I'm listening on {n} instead.")
    return (None if i == default else i), n, ""

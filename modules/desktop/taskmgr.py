"""
modules/desktop/taskmgr.py

Task Manager by voice, without opening Task Manager or touching the window
the user is in:

    "what's running" / "what's using my CPU"     top programs by CPU / memory
    "end task on Discord" / "end the chrome task"  end every process of that app
                                                   (always asks first)
    "how's my PC doing" / "device health"          CPU, memory, disks, GPU, uptime,
                                                   battery and the biggest hogs

Processes are grouped by program (Chrome's 30 processes are one "Chrome").
Windows' own processes can't be ended from here.
"""

import difflib
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Dict, List, Optional

from modules.automation.tools import Tool, ToolError, PermissionLevel, P

log = logging.getLogger("saint.desktop")

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

# Never ended by voice: Windows needs them (or it's SAINT).
PROTECTED = {"system", "system idle process", "registry", "smss", "csrss", "wininit", "winlogon", "services",
             "lsass", "svchost", "dwm", "explorer", "fontdrvhost", "sihost", "ctfmon", "memory compression",
             "audiodg", "spoolsv", "searchhost", "startmenuexperiencehost", "shellexperiencehost",
             "runtimebroker", "taskhostw", "securityhealthservice", "msmpeng", "wudfhost", "conhost",
             "voicemeeter", "voicemeeterpro", "voicemeeter8", "voicemeeter8x64", "voicemeeterpro_x64",
             "audiodg", "nvcontainer", "nvdisplay.container", "taskmgr"}
# Windows bookkeeping, not programs anyone would call "running".
_HIDDEN = {"system idle process", "system", "memcompression", "memory compression", "registry", "secure system"}
_FRIENDLY = {"msedge": "Edge", "chrome": "Chrome", "opera": "Opera", "firefox": "Firefox", "code": "VS Code",
             "steamwebhelper": "Steam", "discord": "Discord", "spotify": "Spotify", "pythonw": "Python",
             "python": "Python", "obs64": "OBS", "robloxplayerbeta": "Roblox", "javaw": "Java"}


def _need():
    if psutil is None:
        raise ToolError("Seeing what's running needs psutil.", "UNSUPPORTED")


def _stem(name: str) -> str:
    return re.sub(r"\.exe$", "", (name or "").lower())


def _label(stem: str) -> str:
    return _FRIENDLY.get(stem, stem.replace("_", " ").title() if stem.islower() else stem)


def _grouped(sample_s: float = 0.4) -> List[Dict]:
    """Programs with their summed CPU% (of the whole machine) and memory."""
    _need()
    procs = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            p.cpu_percent(None)
            procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(sample_s)
    ncpu = psutil.cpu_count() or 1
    groups: Dict[str, Dict] = {}
    me = os.getpid()
    for p in procs:
        try:
            stem = _stem(p.info["name"])
            if not stem or stem in _HIDDEN:
                continue
            cpu = p.cpu_percent(None) / ncpu
            mem = p.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        g = groups.setdefault(stem, {"name": _label(stem), "process": stem, "cpu": 0.0, "memory_mb": 0.0,
                                     "count": 0, "pids": [], "saint": False})
        g["cpu"] += cpu
        g["memory_mb"] += mem / 2 ** 20
        g["count"] += 1
        g["pids"].append(p.pid)
        g["saint"] = g["saint"] or p.pid == me
    for g in groups.values():
        g["cpu"] = round(g["cpu"], 1)
        g["memory_mb"] = round(g["memory_mb"])
    return list(groups.values())


def processes(sort: str = "cpu", limit: int = 8) -> Dict:
    groups = _grouped()
    key = "memory_mb" if sort in ("memory", "ram", "mem") else "cpu"
    top = sorted(groups, key=lambda g: g[key], reverse=True)[:max(1, min(25, int(limit or 8)))]
    return {"sort": key, "processes": [{k: g[k] for k in ("name", "process", "cpu", "memory_mb", "count")}
                                       for g in top],
            "total_programs": len(groups)}


def _find_group(name: str, groups: List[Dict]) -> Optional[Dict]:
    q = re.sub(r"^(?:the|my)\s+", "", (name or "").lower()).strip()
    q = re.sub(r"\s+(?:app|process|program|task)$", "", q)
    if not q:
        return None
    try:
        from modules.desktop.apps import app_catalog, process_names_for
        procs = {p.lower().replace(".exe", "") for p in process_names_for(q, app_catalog.resolve(q))}
    except Exception:
        procs = set()
    squashed = q.replace(" ", "")
    for g in groups:
        if g["process"] in procs or g["process"] == squashed or g["name"].lower() == q:
            return g
    starts = [g for g in groups if g["process"].startswith(squashed) or squashed.startswith(g["process"])
              and len(g["process"]) >= 4]
    if len(starts) == 1:
        return starts[0]
    close = difflib.get_close_matches(squashed, [g["process"] for g in groups], n=1, cutoff=0.8)
    return next((g for g in groups if close and g["process"] == close[0]), None)


def end_task(name: str) -> Dict:
    """End every process of one program (Task Manager's "End task")."""
    _need()
    from modules.desktop.controller import _require
    _require("allow_window_control", "Window control")
    groups = _grouped(0.05)
    g = _find_group(name, groups)
    if g is None:
        busiest = ", ".join(x["name"] for x in sorted(groups, key=lambda x: x["memory_mb"], reverse=True)[:6])
        raise ToolError(f"Nothing called {name} is running. The biggest programs right now: {busiest}.", "NOT_FOUND")
    if g["saint"] or g["process"] in PROTECTED or g["process"].startswith("voicemeeter"):
        raise ToolError(f"I won't end {g['name']} — " + ("that's me." if g["saint"] else "Windows needs it."),
                        "INVALID")
    ended, denied = 0, 0
    live = []
    for pid in g["pids"]:
        try:
            p = psutil.Process(pid)
            p.terminate()
            live.append(p)
            ended += 1
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            denied += 1
    _gone, alive = psutil.wait_procs(live, timeout=4)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass
    log.info("taskmgr.end_task %s ended=%d denied=%d", g["process"], ended, denied)
    if ended == 0 and denied:
        raise ToolError(f"Windows won't let me end {g['name']} (it runs as administrator).", "ACCESS_DENIED")
    return {"name": g["name"], "process": g["process"], "ended": ended, "denied": denied}


def _gpu() -> Optional[Dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=4,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
        name, util, used, total, temp = [x.strip() for x in out.splitlines()[0].split(",")]
        return {"name": name, "percent": int(float(util)), "memory_used_mb": int(float(used)),
                "memory_total_mb": int(float(total)), "temp_c": int(float(temp))}
    except Exception as e:
        log.debug("taskmgr.gpu_failed %s", e)
        return None


def health() -> Dict:
    """A quick check-up: how busy the PC is and anything worth knowing."""
    _need()
    cpu = psutil.cpu_percent(interval=0.5)
    vm = psutil.virtual_memory()
    disks = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
        except Exception:
            continue
        disks.append({"drive": part.device.rstrip("\\"), "free_gb": round(u.free / 2 ** 30, 1),
                      "total_gb": round(u.total / 2 ** 30), "percent": u.percent})
    battery = None
    try:
        b = psutil.sensors_battery()
        if b is not None:
            battery = {"percent": int(b.percent), "plugged": bool(b.power_plugged)}
    except Exception:
        pass
    groups = _grouped(0.3)
    hogs_cpu = sorted(groups, key=lambda g: g["cpu"], reverse=True)[:3]
    hogs_mem = sorted(groups, key=lambda g: g["memory_mb"], reverse=True)[:3]
    warnings = []
    if cpu >= 85:
        warnings.append(f"the CPU is maxed out ({cpu:.0f}%)")
    if vm.percent >= 85:
        warnings.append(f"memory is nearly full ({vm.percent:.0f}%)")
    for d in disks:
        if d["percent"] >= 92 or d["free_gb"] < 10:
            warnings.append(f"{d['drive']} is almost full ({d['free_gb']} GB free)")
    gpu = _gpu()
    if gpu and gpu["temp_c"] >= 85:
        warnings.append(f"the GPU is hot ({gpu['temp_c']}°C)")
    return {"cpu_percent": round(cpu), "memory_percent": round(vm.percent),
            "memory_used_gb": round(vm.used / 2 ** 30, 1), "memory_total_gb": round(vm.total / 2 ** 30),
            "disks": disks, "gpu": gpu, "battery": battery,
            "uptime_h": round((time.time() - psutil.boot_time()) / 3600, 1),
            "top_cpu": [{"name": g["name"], "cpu": g["cpu"]} for g in hogs_cpu],
            "top_memory": [{"name": g["name"], "memory_mb": g["memory_mb"]} for g in hogs_mem],
            "warnings": warnings}


def startup_apps() -> Dict:
    """What starts with Windows (the Run keys and the Startup folders) — read only."""
    items = []
    try:
        import winreg
        for hive, path in ((winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
                           (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run")):
            try:
                with winreg.OpenKey(hive, path) as k:
                    i = 0
                    while True:
                        try:
                            name, _val, _t = winreg.EnumValue(k, i)
                        except OSError:
                            break
                        items.append(name)
                        i += 1
            except OSError:
                continue
    except ImportError:
        pass
    for folder in (os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"),
                   os.path.expandvars(r"%PROGRAMDATA%\Microsoft\Windows\Start Menu\Programs\StartUp")):
        try:
            items += [os.path.splitext(f)[0] for f in os.listdir(folder) if not f.lower().endswith(".ini")]
        except OSError:
            continue
    seen, out = set(), []
    for n in items:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return {"apps": out}


def register_taskmgr_tools(registry):
    tools = [
        Tool("system.processes", "List the programs using the most CPU or memory right now", {},
             PermissionLevel.LOW, processes,
             parameters={"sort": P("string", required=False, default="cpu", enum=["cpu", "memory"]),
                         "limit": P("integer", required=False, default=8, minimum=1, maximum=25)},
             llm_exposed=True, category="system"),
        # Always asks first (ALWAYS_CONFIRM).
        Tool("system.end_task", "End a running program and all its processes, like Task Manager's End task",
             {"name": "string"}, PermissionLevel.HIGH, end_task,
             parameters={"name": P("string", "the program, e.g. Discord or chrome")}, category="system"),
        Tool("system.health", "A check-up of the PC: CPU, memory, disks, GPU, uptime and what's using the most",
             {}, PermissionLevel.LOW, health, parameters={}, llm_exposed=True, category="system"),
        Tool("system.startup_apps", "List the programs that start with Windows", {}, PermissionLevel.LOW,
             startup_apps, parameters={}, llm_exposed=True, category="system"),
    ]
    for t in tools:
        registry.register(t)

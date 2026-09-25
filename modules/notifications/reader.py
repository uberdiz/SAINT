"""
modules/notifications/reader.py

Reads Windows' own notification list (the same toasts the Notification
Centre shows) from %LOCALAPPDATA%\\Microsoft\\Windows\\Notifications\\
wpndatabase.db — read-only, never written. Windows only lets packaged Store
apps use the official notification-listener API, so this is how a regular
app like SAINT can see them.

Importance (notifications.important_only): an app on the allow list, or text
matching one of notifications.keywords ("failed", "mentioned you", ...).
Apps on the deny list are never announced.
"""

import os
import re
import shutil
import sqlite3
import tempfile
import time
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

from core.config import config

_FILETIME_EPOCH = 11644473600          # seconds between 1601-01-01 and 1970-01-01


def db_path() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Notifications",
                        "wpndatabase.db")


def _connect(path: Optional[str] = None):
    path = path or db_path()
    if not os.path.exists(path):
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        con.execute("select 1 from Notification limit 1")
        return con
    except sqlite3.Error:
        # Locked by Windows: read a private copy instead (the original is never touched).
        tmp = os.path.join(tempfile.gettempdir(), "saint-wpn.db")
        try:
            shutil.copy2(path, tmp)
            for ext in ("-wal", "-shm"):
                if os.path.exists(path + ext):
                    shutil.copy2(path + ext, tmp + ext)
            return sqlite3.connect(tmp, timeout=2)
        except (OSError, sqlite3.Error):
            return None


def app_name(primary_id: str) -> str:
    """'Claude_pzs8sxrjxfjjc!Claude' -> 'Claude'; 'com.squirrel.Discord.Discord' -> 'Discord'."""
    pid = primary_id or ""
    if "!" in pid:
        name = pid.split("!")[-1]
    else:
        name = pid.split("\\")[-1].split(".")[-1]
    name = re.sub(r"(?i)\.exe$", "", name)
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name) if name.isalnum() and not name.isupper() else name
    return {"App": "an app", "Msedge": "Edge", "Chrome": "Chrome"}.get(name, name) or "an app"


def parse_payload(payload) -> List[str]:
    """The text lines of a toast (title first)."""
    if payload is None:
        return []
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return [t for t in re.findall(r"<text[^>]*>(.*?)</text>", payload, re.S) if t.strip()]
    return [" ".join((el.text or "").split()) for el in root.iter("text") if (el.text or "").strip()]


def read(since_order: int = 0, limit: int = 50, path: Optional[str] = None) -> List[Dict]:
    """Toasts newer than ``since_order`` (newest last)."""
    con = _connect(path)
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT n.\"Order\", n.Payload, n.ArrivalTime, h.PrimaryId FROM Notification n "
            "JOIN NotificationHandler h ON h.RecordId = n.HandlerId "
            "WHERE n.Type = 'toast' AND n.\"Order\" > ? ORDER BY n.\"Order\" DESC LIMIT ?",
            (int(since_order), int(limit))).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    out = []
    for order, payload, arrival, primary in reversed(rows):
        lines = parse_payload(payload)
        if not lines:
            continue
        try:
            at = int(arrival) / 1e7 - _FILETIME_EPOCH
        except (TypeError, ValueError):
            at = time.time()
        out.append({"order": int(order), "app": app_name(primary), "app_id": primary, "title": lines[0],
                    "body": " ".join(lines[1:])[:300], "at": at})
    return out


def latest_order(path: Optional[str] = None) -> int:
    con = _connect(path)
    if con is None:
        return 0
    try:
        row = con.execute("SELECT MAX(\"Order\") FROM Notification").fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0
    finally:
        con.close()


def is_important(n: Dict) -> bool:
    allow = [a.lower() for a in config.get("notifications.allow", []) or []]
    deny = [a.lower() for a in config.get("notifications.deny", []) or []]
    app = n.get("app", "").lower()
    if any(d in app for d in deny):
        return False
    if any(a in app for a in allow):
        return True
    if not config.get("notifications.important_only", True):
        return True
    text = f"{n.get('title', '')} {n.get('body', '')}".lower()
    return any(k.lower() in text for k in config.get("notifications.keywords", []) or [])


def spoken(n: Dict) -> str:
    body = f": {n['body']}" if n.get("body") else ""
    return f"{n['app']} — {n['title']}{body}"[:240]

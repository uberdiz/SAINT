"""
modules/desktop/media.py

What's playing on the PC — any app, not just Spotify. Windows keeps a list
of media sessions (the same one behind the volume flyout's media controls):
a YouTube video in a browser, VLC, Apple Music, Spotify... ``media`` polls it
on a background thread and emits MEDIA_CHANGED with the session that matters
(the one playing, else the one Windows calls current), and can play/pause or
skip it.

Needs the ``winrt-Windows.Media.Control`` packages; without them SAINT falls
back to Spotify-only now-playing and says so in the log once.
"""

import asyncio
import hashlib
import logging
import threading
import time
from typing import Dict, Optional

from core.events import event_bus, EventType

log = logging.getLogger("saint.desktop.media")

_PLAYING, _PAUSED = 4, 5

# AppUserModelIDs → a readable app name
_APPS = (("spotify", "Spotify"), ("opera", "Opera"), ("chrome", "Chrome"), ("msedge", "Edge"),
         ("firefox", "Firefox"), ("brave", "Brave"), ("vivaldi", "Vivaldi"), ("vlc", "VLC"),
         ("zune", "Media Player"), ("microsoft.media", "Media Player"), ("applemusic", "Apple Music"),
         ("itunes", "iTunes"), ("tidal", "TIDAL"), ("deezer", "Deezer"), ("discord", "Discord"),
         ("netflix", "Netflix"), ("twitch", "Twitch"), ("youtube", "YouTube"), ("plex", "Plex"))


def app_label(aumid: str) -> str:
    a = (aumid or "").lower()
    return next((name for key, name in _APPS if key in a), (aumid or "").split("!")[0].split(".")[0] or "Media")


def pick(sessions: list, current_id: str = "") -> Optional[dict]:
    """The session to show: one that's playing (Windows' current one first),
    else Windows' current session, else the first one."""
    if not sessions:
        return None
    playing = [s for s in sessions if s.get("is_playing")]
    if playing:
        return next((s for s in playing if s["app_id"] == current_id), playing[0])
    return next((s for s in sessions if s["app_id"] == current_id), sessions[0])


class MediaWatcher:
    def __init__(self, interval: float = 1.2):
        self.interval = interval
        self._lock = threading.Lock()
        self._state: Dict = {}
        self._thumbs: Dict[str, bytes] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._sessions = {}           # app_id -> winrt session (loop thread only)
        self.available: Optional[bool] = None

    # ------------------------------------------------------------------ #
    @property
    def state(self) -> Dict:
        with self._lock:
            return dict(self._state)

    def thumbnail(self, key: str) -> bytes:
        with self._lock:
            return self._thumbs.get(key, b"")

    def start(self):
        from core.config import config
        if not config.get("widgets.any_media", True) or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="media-watcher", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------ #
    def _run(self):
        try:
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M
        except Exception as e:
            self.available = False
            log.info("media.unavailable %s (pip install winrt-Windows.Media.Control)", e)
            return
        self.available = True
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._poll(M))
        except Exception:
            log.exception("media.watcher_crashed")
        finally:
            loop.close()
            self._loop = None

    async def _poll(self, M):
        mgr = await M.request_async()
        last_sig = None
        while not self._stop.is_set():
            try:
                cur = mgr.get_current_session()
                current_id = cur.source_app_user_model_id if cur is not None else ""
                sessions, handles = [], {}
                for s in mgr.get_sessions():
                    info = await self._describe(s)
                    if info and info.get("title"):
                        sessions.append(info)
                        handles[info["app_id"]] = s
                self._sessions = handles
                chosen = pick(sessions, current_id) or {}
                sig = (chosen.get("app_id"), chosen.get("title"), chosen.get("artist"), chosen.get("is_playing"),
                       chosen.get("thumb"), round((chosen.get("position_ms") or 0) / 4000))
                with self._lock:
                    self._state = dict(chosen, sessions=[{k: v for k, v in x.items() if k != "thumb_bytes"}
                                                          for x in sessions])
                if sig != last_sig:
                    last_sig = sig
                    payload = {k: v for k, v in chosen.items() if k != "thumb_bytes"}
                    payload["sessions"] = len(sessions)
                    event_bus.emit_event(EventType.MEDIA_CHANGED, payload)
            except Exception as e:
                log.debug("media.poll_failed %s", e)
            await asyncio.sleep(self.interval)

    async def _describe(self, s) -> Optional[Dict]:
        try:
            props = await s.try_get_media_properties_async()
        except Exception:
            return None
        pb = s.get_playback_info()
        tl = s.get_timeline_properties()
        app_id = s.source_app_user_model_id or ""
        title = (props.title or "").strip()
        artist = (props.artist or "").strip()
        thumb = ""
        if props.thumbnail is not None:
            thumb = f"media://{hashlib.md5((app_id + title + artist).encode('utf-8', 'ignore')).hexdigest()}"
            with self._lock:
                have = thumb in self._thumbs
            if not have:
                data = await self._read_thumb(props.thumbnail)
                if data:
                    with self._lock:
                        self._thumbs[thumb] = data
                        while len(self._thumbs) > 12:
                            self._thumbs.pop(next(iter(self._thumbs)))
                else:
                    thumb = ""

        def ms(td):
            try:
                return int(td.total_seconds() * 1000)
            except Exception:
                return 0
        try:
            # The position is as of Windows' last timeline update (browsers only
            # report on play / pause / seek) — progress interpolates from then.
            at = tl.last_updated_time.timestamp()
        except Exception:
            at = time.time()
        ctl = pb.controls
        return {
            "app_id": app_id, "app": app_label(app_id), "title": title, "artist": artist,
            "album": (props.album_title or "").strip(), "is_playing": pb.playback_status == _PLAYING,
            "status": int(pb.playback_status), "position_ms": ms(tl.position), "duration_ms": ms(tl.end_time),
            "at": min(at, time.time()), "thumb": thumb,
            "can_next": bool(ctl.is_next_enabled), "can_previous": bool(ctl.is_previous_enabled),
            "can_play_pause": bool(ctl.is_play_pause_toggle_enabled),
            "is_spotify": "spotify" in app_id.lower(),
        }

    @staticmethod
    async def _read_thumb(ref) -> bytes:
        try:
            from winrt.windows.storage.streams import Buffer, InputStreamOptions
            stream = await ref.open_read_async()
            size = int(stream.size)
            if not size or size > 8 * 1024 * 1024:
                return b""
            buf = Buffer(size)
            got = await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
            return bytes(got)
        except Exception as e:
            log.debug("media.thumb_failed %s", e)
            return b""

    # ------------------------------------------------------------------ #
    def control(self, action: str, app_id: str = "") -> bool:
        """play_pause / next / previous on the shown (or named) session."""
        loop = self._loop
        if loop is None:
            return False
        app_id = app_id or self.state.get("app_id", "")

        async def go():
            s = self._sessions.get(app_id)
            if s is None:
                return False
            fn = {"play_pause": s.try_toggle_play_pause_async, "next": s.try_skip_next_async,
                  "previous": s.try_skip_previous_async, "play": s.try_play_async,
                  "pause": s.try_pause_async}.get(action)
            return bool(await fn()) if fn else False
        try:
            ok = asyncio.run_coroutine_threadsafe(go(), loop).result(timeout=3)
        except Exception as e:
            log.info("media.control_failed %s %s", action, e)
            return False
        with self._lock:
            if ok and action in ("play_pause", "play", "pause") and self._state.get("app_id") == app_id:
                playing = {"play": True, "pause": False}.get(action, not self._state.get("is_playing"))
                self._state.update(is_playing=playing)
        return ok


media = MediaWatcher()

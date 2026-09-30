"""
modules/spotify/lyrics.py

Song lyrics for what's playing, from LRCLIB (lrclib.net: free, no key,
time-synced lines). Two users:

* the mini player's lyrics panel (current line, live);
* the voice gate: when music plays, a follow-up "command" that is really the
  song singing ("Open up your eyes." opened the dashboard on 2026-09-29) is
  recognised as a lyric and dropped.

Lookups run off the UI thread and are cached in memory and on disk
(data/cache/lyrics), so a song is fetched once. Only title / artist / album /
duration are sent.
"""

import difflib
import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger("saint.lyrics")

API = "https://lrclib.net/api"
_HEADERS = {"User-Agent": "SAINT desktop assistant (lyrics for the mini player)"}
_LRC_LINE = re.compile(r"^\s*((?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+)\s*(.*)$")
_STAMP = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")


@dataclass
class Lyrics:
    title: str
    artist: str
    synced: List[Tuple[int, str]] = field(default_factory=list)     # (ms, line), sorted
    plain: List[str] = field(default_factory=list)
    instrumental: bool = False

    @property
    def found(self) -> bool:
        return bool(self.synced or self.plain or self.instrumental)

    def lines(self) -> List[str]:
        return [t for _, t in self.synced] if self.synced else list(self.plain)

    def index_at(self, pos_ms: int, duration_ms: int = 0) -> int:
        """Index of the line being sung at ``pos_ms`` (-1 before the first).
        Unsynced lyrics are spread evenly over the song."""
        if self.synced:
            i = -1
            for n, (ms, _t) in enumerate(self.synced):
                if ms <= pos_ms:
                    i = n
                else:
                    break
            return i
        if self.plain and duration_ms:
            return min(len(self.plain) - 1, int(len(self.plain) * max(0, pos_ms) / duration_ms))
        return -1


def parse_lrc(text: str) -> List[Tuple[int, str]]:
    out = []
    for raw in (text or "").splitlines():
        m = _LRC_LINE.match(raw)
        if not m:
            continue
        line = m.group(2).strip()
        for mm, ss, frac in _STAMP.findall(m.group(1)):
            ms = (int(mm) * 60 + int(ss)) * 1000 + (int(frac.ljust(3, "0")[:3]) if frac else 0)
            out.append((ms, line))
    out.sort(key=lambda x: x[0])
    return out


def _fold(s: str) -> str:
    """Lower case, no accents or punctuation: "Tití Me Preguntó" -> "titi me pregunto"."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class LyricsService:
    def __init__(self):
        self._lock = threading.Lock()
        self._mem: Dict[str, Optional[Lyrics]] = {}
        self._inflight: Dict[str, List[Callable]] = {}
        self._current_key = ""
        self._current: Optional[Lyrics] = None
        self._current_pos: Tuple[int, float, bool] = (0, 0.0, False)     # (position_ms, at, playing)

    # ------------------------------------------------------------------ #
    @staticmethod
    def key(title: str, artist: str) -> str:
        return f"{_fold(artist).split(',')[0].strip()}|{_fold(title)}"

    def _disk(self, key: str):
        from core.paths import data_path
        return data_path("cache", "lyrics", hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + ".json")

    def cached(self, title: str, artist: str) -> Optional[Lyrics]:
        """Lyrics already known (memory or disk) — never touches the network."""
        k = self.key(title, artist)
        with self._lock:
            if k in self._mem:
                return self._mem[k]
        try:
            p = self._disk(k)
            if p.exists():
                d = json.loads(p.read_text(encoding="utf-8"))
                ly = None if d.get("missing") else Lyrics(title, artist, [tuple(x) for x in d.get("synced", [])],
                                                          d.get("plain", []), bool(d.get("instrumental")))
                if ly is None and time.time() - float(d.get("at", 0)) > 7 * 86400:
                    return None                          # retry a miss after a week
                with self._lock:
                    self._mem[k] = ly
                return ly
        except Exception as e:
            log.debug("lyrics.cache_read_failed %s", e)
        return None

    def get(self, title: str, artist: str, album: str = "", duration_ms: int = 0) -> Optional[Lyrics]:
        """Blocking lookup (call off the UI thread). None when LRCLIB has nothing."""
        if not title:
            return None
        k = self.key(title, artist)
        with self._lock:
            if k in self._mem:
                return self._mem[k]
        hit = self.cached(title, artist)
        if hit is not None:
            return hit
        ly = self._fetch(title, artist, album, duration_ms)
        with self._lock:
            self._mem[k] = ly
        try:
            payload = {"missing": True, "at": time.time()} if ly is None else {
                "synced": ly.synced, "plain": ly.plain, "instrumental": ly.instrumental, "at": time.time()}
            self._disk(k).write_text(json.dumps(payload), encoding="utf-8")
        except Exception as e:
            log.debug("lyrics.cache_write_failed %s", e)
        return ly

    def get_async(self, title: str, artist: str, album: str, duration_ms: int, done: Callable[[Optional[Lyrics]], None]):
        k = self.key(title, artist)
        with self._lock:
            if k in self._mem:
                ly = self._mem[k]
                threading.Thread(target=done, args=(ly,), daemon=True).start()
                return
            if k in self._inflight:
                self._inflight[k].append(done)
                return
            self._inflight[k] = [done]

        def run():
            try:
                ly = self.get(title, artist, album, duration_ms)
            except Exception:
                log.exception("lyrics.fetch_crashed")
                ly = None
            with self._lock:
                waiters = self._inflight.pop(k, [])
            for cb in waiters:
                try:
                    cb(ly)
                except Exception:
                    log.exception("lyrics.callback_failed")
        threading.Thread(target=run, daemon=True, name="lyrics").start()

    def _fetch(self, title: str, artist: str, album: str, duration_ms: int) -> Optional[Lyrics]:
        import requests
        main_artist = (artist or "").split(",")[0].strip()
        clean_title = re.sub(r"\s*[(\[](?:feat|with|ft)\.?[^)\]]*[)\]]|\s+-\s+.*remaster.*$", "", title, flags=re.I).strip()
        params = {"track_name": clean_title, "artist_name": main_artist}
        if album:
            params["album_name"] = album
        if duration_ms:
            params["duration"] = int(round(duration_ms / 1000))
        started = time.perf_counter()
        try:
            r = requests.get(f"{API}/get", params=params, headers=_HEADERS, timeout=8)
            data = r.json() if r.status_code == 200 else None
            if data is None:
                # Album or duration off by a little: search and take the closest match.
                r = requests.get(f"{API}/search", params={"track_name": clean_title, "artist_name": main_artist},
                                 headers=_HEADERS, timeout=8)
                results = r.json() if r.status_code == 200 else []
                data = self._best(results, clean_title, main_artist, duration_ms)
        except Exception as e:
            log.info("lyrics.fetch_failed %r by %r: %s", title, artist, e)
            return None
        log.info("lyrics.fetch %r by %r found=%s (%.0f ms)", title, main_artist, bool(data),
                 (time.perf_counter() - started) * 1000)
        if not data:
            return None
        ly = Lyrics(title, artist, parse_lrc(data.get("syncedLyrics") or ""),
                    [x.strip() for x in (data.get("plainLyrics") or "").splitlines() if x.strip()],
                    bool(data.get("instrumental")))
        return ly if ly.found else None

    @staticmethod
    def _best(results, title: str, artist: str, duration_ms: int):
        best, best_score = None, 0.0
        for d in results or []:
            if not isinstance(d, dict) or not (d.get("syncedLyrics") or d.get("plainLyrics") or d.get("instrumental")):
                continue
            s = difflib.SequenceMatcher(None, _fold(title), _fold(d.get("trackName", ""))).ratio() * 2
            s += difflib.SequenceMatcher(None, _fold(artist), _fold(d.get("artistName", ""))).ratio()
            if duration_ms and d.get("duration"):
                s -= min(1.0, abs(float(d["duration"]) - duration_ms / 1000) / 20)
            if d.get("syncedLyrics"):
                s += 0.2
            if s > best_score:
                best, best_score = d, s
        return best if best_score >= 2.2 else None

    # ------------------------------------------------------------------ #
    # What's playing (fed by the Spotify poller / media events)
    # ------------------------------------------------------------------ #
    def note_playing(self, title: str, artist: str, album: str = "", duration_ms: int = 0,
                     position_ms: int = 0, playing: bool = True, prefetch: bool = True):
        if not title:
            return
        k = self.key(title, artist)
        with self._lock:
            self._current_pos = (int(position_ms or 0), time.time(), bool(playing))
            changed = k != self._current_key
            if changed:
                self._current_key, self._current = k, self._mem.get(k)
        if changed and prefetch:
            def keep(ly, k=k):
                with self._lock:
                    if self._current_key == k:
                        self._current = ly
            self.get_async(title, artist, album, duration_ms, keep)

    def current_position(self) -> int:
        pos, at, playing = self._current_pos
        return pos + int((time.time() - at) * 1000) if playing else pos

    def matches_current(self, text: str, window_ms: int = 45000) -> bool:
        """Is ``text`` a line of the song playing now (near where it is)? Cache only."""
        with self._lock:
            ly = self._current
        if ly is None or not ly.lines():
            return False
        return is_lyric(text, ly, self.current_position() if ly.synced else None, window_ms)


def is_lyric(text: str, ly: Lyrics, pos_ms: Optional[int] = None, window_ms: int = 45000) -> bool:
    """Heard ``text`` matches a lyric line (or two consecutive lines).

    With synced lyrics and a position, only lines within ``window_ms`` count,
    so a real command that happens to share words with a verse elsewhere in
    the song still gets through."""
    said = _fold(text)
    words = said.split()
    if len(words) < 3:
        return False                  # "skip" / "pause" are too short to judge
    if ly.synced and pos_ms is not None:
        cand = [t for ms, t in ly.synced if abs(ms - pos_ms) <= window_ms]
    else:
        cand = ly.lines()
    cand = [_fold(c) for c in cand if c.strip()]
    spans = cand + [a + " " + b for a, b in zip(cand, cand[1:])]
    for span in spans:
        if not span:
            continue
        if difflib.SequenceMatcher(None, said, span).ratio() >= 0.78:
            return True
        # Whisper hears part of a line: most of what was heard is in it, in order.
        if len(words) >= 4 and said in span:
            return True
        sw = span.split()
        common = sum(1 for w in words if w in sw)
        if len(words) >= 4 and common / len(words) >= 0.85 and \
                difflib.SequenceMatcher(None, said, span).find_longest_match(0, len(said), 0, len(span)).size >= 12:
            return True
    return False


lyrics_service = LyricsService()

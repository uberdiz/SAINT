"""
modules/desktop/window_match.py

Finding the window the user means when the name they said doesn't match any
window title — trying harder before saying "I couldn't find it":

  * speech-to-text slips: "close to area" -> Terraria, "close the finers" ->
    THE FINALS (a letter-skeleton comparison catches what spelling doesn't);
  * games: a running Steam game is found through its install folder, even when
    its window has no title or hides from the normal window list.

Callers confirm with the *real* name ("Close Terraria?"), so a looser match
is safe here.
"""

import difflib
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("saint.desktop")


def _simple(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return " ".join(w for w in s.split() if w not in ("the", "a", "my", "app", "window", "game"))


def skeleton(s: str) -> str:
    """Consonant skeleton: 'to area' and 'Terraria' both -> 'tr'."""
    s = re.sub(r"[^a-z]", "", _simple(s))
    s = re.sub(r"(.)\1+", r"\1", s)
    if not s:
        return ""
    return re.sub(r"(.)\1+", r"\1", s[0] + re.sub(r"[aeiouhwy]", "", s[1:]))


_SOUND = str.maketrans({"c": "k", "q": "k", "g": "k", "b": "p", "d": "t", "v": "f", "z": "s", "x": "k"})


def sounds_like(a: str, b: str) -> float:
    """How alike two words sound to speech-to-text (0..1): 'Glod' ~ 'Claude',
    'clawed' ~ 'Claude', 'MO' ~ 'moe'. Voiced/unvoiced pairs count as one."""
    sa, sb = skeleton(a).translate(_SOUND), skeleton(b).translate(_SOUND)
    if not sa or not sb:
        return 0.0
    if sa == sb:
        return 1.0 if min(len(_simple(a)), len(_simple(b))) > 2 or sa[0] == sb[0] else 0.8
    return difflib.SequenceMatcher(None, sa, sb).ratio() * 0.9


def score(query: str, name: str) -> float:
    q, n = _simple(query), _simple(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if re.search(rf"\b{re.escape(q)}\b", n) or re.search(rf"\b{re.escape(n)}\b", q):
        return 0.9
    best = difflib.SequenceMatcher(None, q.replace(" ", ""), n.replace(" ", "")).ratio()
    sq, sn = skeleton(q), skeleton(n)
    if len(sq) >= 2 and sq == sn:
        best = max(best, 0.8)
    elif len(sq) >= 3 and len(sn) >= 3:
        best = max(best, 0.85 * difflib.SequenceMatcher(None, sq, sn).ratio())
    return best


def best_match(query: str, named: List[Tuple[str, object]], threshold: float = 0.62) -> Optional[object]:
    """The item whose name best matches ``query`` — only when it clearly beats the rest."""
    scored = sorted(((score(query, name), i) for i, (name, _obj) in enumerate(named)), reverse=True)
    if not scored or scored[0][0] < threshold:
        return None
    if len(scored) > 1 and scored[1][0] > scored[0][0] - 0.08 and named[scored[1][1]][1] is not named[scored[0][1]][1]:
        return None                         # two different windows fit about as well: don't guess
    return named[scored[0][1]][1]


def window_names(w) -> List[str]:
    from modules.vision.screen import app_label
    names = [app_label({"title": w.title, "process": w.process}), w.process.lower().replace(".exe", "")]
    names += [p for p in re.split(r"\s+[-–—|:]\s+", w.title or "") if p][:3]
    return [n for n in names if n]


def fuzzy_window(query: str, wins) -> Optional[object]:
    named = [(n, w) for w in wins for n in window_names(w)]
    return best_match(query, named)


# ---------------------------------------------------------------------- #
# Steam games
# ---------------------------------------------------------------------- #
def _running_under(paths: Dict[str, object]) -> Dict[int, object]:
    """pid -> game for processes whose executable lives in a game's folder."""
    out = {}
    try:
        import psutil
    except ImportError:
        return out
    norm = {os.path.normcase(os.path.normpath(p)) + os.sep: g for p, g in paths.items()}
    for proc in psutil.process_iter(["pid", "exe"]):
        exe = proc.info.get("exe") or ""
        if not exe:
            continue
        low = os.path.normcase(exe)
        for root, g in norm.items():
            if low.startswith(root):
                out[proc.info["pid"]] = g
                break
    return out


def running_games() -> Dict[int, object]:
    try:
        from modules.steam.library import steam_library
        games = steam_library.games()
    except Exception:
        return {}
    return _running_under({g.path: g for g in games if g.installdir})


def windows_of(pids) -> List[int]:
    """Visible top-level windows of these processes — titled or not."""
    import win32gui
    import win32process
    pids, out = set(pids), []

    def cb(hwnd, _):
        try:
            if win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] in pids:
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                if r - l > 50 and b - t > 50:
                    out.append(hwnd)
        except Exception:
            pass
        return True
    win32gui.EnumWindows(cb, None)
    return out


def game_window(query: str) -> Optional[Tuple[object, List[int], List[int]]]:
    """(game, window handles, pids) for a running Steam game the user named."""
    running = running_games()
    if not running:
        return None
    games = {id(g): g for g in running.values()}
    game = best_match(query, [(g.name, g) for g in games.values()] +
                      [(g.installdir, g) for g in games.values()])
    if game is None:
        return None
    pids = [pid for pid, g in running.items() if g is game]
    log.info("desktop.window_match game=%r pids=%s", game.name, pids)
    return game, windows_of(pids), pids

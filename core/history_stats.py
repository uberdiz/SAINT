"""
core/history_stats.py

Numbers for the History page, computed from the per-day summary
(core/history.py → History.daily()) and the recent log. No Qt, so the maths
is testable on its own.
"""

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from core.history import SOURCES


def _parse(key: str) -> date:
    return datetime.strptime(key, "%Y-%m-%d").date()


def calendar(daily: Dict[str, dict], weeks: int = 53, today: Optional[date] = None) -> List[Tuple[date, int]]:
    """Every day of the last ``weeks`` calendar weeks (Sunday first, ending
    with the week that contains today), oldest first, with its request count.
    Days after today are left out."""
    today = today or date.today()
    start = today - timedelta(days=(today.weekday() + 1) % 7) - timedelta(weeks=weeks - 1)
    out = []
    d = start
    while d <= today:
        out.append((d, int((daily.get(d.isoformat()) or {}).get("n", 0))))
        d += timedelta(days=1)
    return out


def level_thresholds(counts: List[int]) -> List[float]:
    """Upper bounds for levels 1-3 from the quartiles of active days (level 4
    is anything above), like GitHub's contribution graph."""
    active = sorted(c for c in counts if c > 0)
    if not active:
        return [1, 2, 3]
    def q(p):
        return active[min(len(active) - 1, int(p * (len(active) - 1)))]
    t = [q(0.25), q(0.5), q(0.75)]
    # Strictly increasing so every level can appear.
    for i in range(1, 3):
        if t[i] <= t[i - 1]:
            t[i] = t[i - 1] + 0.5
    return t


def level(count: int, thresholds: List[float]) -> int:
    if count <= 0:
        return 0
    for i, t in enumerate(thresholds):
        if count <= t:
            return i + 1
    return 4


def streaks(daily: Dict[str, dict], today: Optional[date] = None) -> Tuple[int, int]:
    """(current, longest) runs of consecutive days with at least one request.
    The current streak still counts if today has no requests yet."""
    today = today or date.today()
    active = sorted(_parse(k) for k, v in daily.items() if (v or {}).get("n", 0) > 0)
    if not active:
        return 0, 0
    longest = run = 1
    for a, b in zip(active, active[1:]):
        run = run + 1 if (b - a).days == 1 else 1
        longest = max(longest, run)
    days = set(active)
    cur, d = 0, today if today in days else today - timedelta(days=1)
    while d in days:
        cur += 1
        d -= timedelta(days=1)
    return cur, longest


def busiest_day(daily: Dict[str, dict]) -> Optional[Tuple[date, int]]:
    best = max(((k, (v or {}).get("n", 0)) for k, v in daily.items()), key=lambda kv: kv[1], default=None)
    return (_parse(best[0]), best[1]) if best and best[1] else None


def totals(daily: Dict[str, dict]) -> dict:
    """Lifetime totals: requests, per source, success rate, average latency, by hour, by weekday."""
    n = ok = fail = ms_sum = ms_n = 0
    by_source = {s: 0 for s in SOURCES}
    hours = [0] * 24
    weekdays = [0] * 7
    for k, v in daily.items():
        v = v or {}
        n += v.get("n", 0)
        ok += v.get("ok", 0)
        fail += v.get("fail", 0)
        ms_sum += v.get("ms_sum", 0)
        ms_n += v.get("ms_n", 0)
        for s in SOURCES:
            by_source[s] += v.get(s, 0)
        for h, c in enumerate(v.get("hours") or []):
            if h < 24:
                hours[h] += c
        try:
            weekdays[_parse(k).weekday()] += v.get("n", 0)
        except ValueError:
            pass
    return {"n": n, "ok": ok, "fail": fail, "success": (ok / (ok + fail)) if ok + fail else None,
            "avg_ms": (ms_sum / ms_n) if ms_n else None, "by_source": by_source, "hours": hours,
            "weekdays": weekdays}


def percentile(values: List[float], p: float) -> Optional[float]:
    vals = sorted(v for v in values if v)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(round(p * (len(vals) - 1))))]

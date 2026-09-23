"""
modules/automation/timeparse.py

Natural-language schedule parsing for reminders and automations (local time).

Supported forms (case-insensitive):
    in 30 minutes / in an hour / in 2 hours and 15 minutes / in half an hour
    at 5 / at 5 pm / at 5:30pm / at 17:30 / at noon / at midnight
    tomorrow (at 9) / tomorrow morning / tonight (at 8) / this evening
    on monday (at 9) / next friday at 3pm
    every morning / every evening / every night (at 9:30)
    every day at 7 / daily at 7 / every weekday at 8 / every weekend at 10
    every monday (and thursday) at 9
    every 20 minutes / every 2 hours / every hour

``parse_schedule(text)`` returns ``(schedule, remainder)`` where schedule is
one of
    {"type": "once", "at": <epoch seconds>}
    {"type": "interval", "every_sec": N, "start": <epoch>}
    {"type": "daily", "time": "HH:MM", "days": [0..6] | None}   (0 = Monday)
and remainder is the text with the time expression removed, or
``(None, text)`` when no time expression was found.
"""

import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

_NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
    "thirty": 30, "forty": 40, "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90,
    "a couple": 2, "a couple of": 2, "a few": 3, "few": 3,
}
_UNITS = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400,
          "week": 604800}
_DAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
         "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
         "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
_PARTS_OF_DAY = {"morning": "08:00", "afternoon": "14:00", "evening": "18:00", "night": "21:00",
                 "tonight": "20:00", "noon": "12:00", "midday": "12:00", "midnight": "00:00"}

_NUM = r"(\d+(?:\.\d+)?|a couple of|a couple|a few|an|a|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty five|forty|fifty|sixty|ninety|few)"
_UNIT = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)"
_CLOCK = r"(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?"
_DAY_NAMES = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thurs?|thu|fri|sat|sun)"


def _num(s: str) -> float:
    s = s.strip().lower()
    return float(s) if re.match(r"^\d", s) else float(_NUM_WORDS.get(s, 1))


def _unit_sec(u: str) -> int:
    u = u.lower().rstrip("s")
    return _UNITS.get(u, _UNITS.get(u[:3], 60))


def _clock(h: str, m: Optional[str], ampm: Optional[str], default_pm: bool = True) -> Tuple[int, int]:
    hour, minute = int(h), int(m or 0)
    ap = (ampm or "").replace(".", "").lower()
    if ap == "pm" and hour < 12:
        hour += 12
    elif ap == "am" and hour == 12:
        hour = 0
    elif not ap and default_pm and 1 <= hour <= 7:
        hour += 12      # "at 5" almost always means 5 PM for reminders
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("invalid time")
    return hour, minute


def _default_morning() -> str:
    try:
        from core.config import config
        return config.get("automation.default_morning_time", "08:00")
    except Exception:
        return "08:00"


def _hhmm(h: int, m: int) -> str:
    return f"{h:02d}:{m:02d}"


def next_daily(time_str: str, days: Optional[List[int]], now: Optional[datetime] = None) -> datetime:
    """Next occurrence of HH:MM on one of ``days`` (None = every day), strictly after now."""
    now = now or datetime.now()
    hour, minute = map(int, time_str.split(":"))
    for offset in range(0, 8):
        cand = (now + timedelta(days=offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand > now and (days is None or cand.weekday() in days):
            return cand
    return (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)


def next_run(schedule: Dict, after: Optional[float] = None) -> Optional[float]:
    """Next fire time (epoch) for a schedule strictly after ``after``."""
    after = time.time() if after is None else after
    t = schedule.get("type")
    if t == "once":
        at = float(schedule["at"])
        return at if at > after else None
    if t == "interval":
        every = max(1, int(schedule["every_sec"]))
        start = float(schedule.get("start", after))
        if start > after:
            return start
        n = int((after - start) // every) + 1
        return start + n * every
    if t == "daily":
        return next_daily(schedule["time"], schedule.get("days"),
                          datetime.fromtimestamp(after)).timestamp()
    return None


def describe(schedule: Dict) -> str:
    t = schedule.get("type")
    if t == "once":
        dt = datetime.fromtimestamp(schedule["at"])
        now = datetime.now()
        day = ("today" if dt.date() == now.date() else
               "tomorrow" if dt.date() == (now + timedelta(days=1)).date() else dt.strftime("%A %b %d"))
        return f"{day} at {dt.strftime('%I:%M %p').lstrip('0')}"
    if t == "interval":
        s = int(schedule["every_sec"])
        if s % 3600 == 0:
            n = s // 3600
            return "every hour" if n == 1 else f"every {n} hours"
        if s % 60 == 0:
            n = s // 60
            return "every minute" if n == 1 else f"every {n} minutes"
        return f"every {s} seconds"
    if t == "daily":
        hh, mm = map(int, schedule["time"].split(":"))
        clock = datetime(2000, 1, 1, hh, mm).strftime("%I:%M %p").lstrip("0")
        days = schedule.get("days")
        names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        if not days or len(days) == 7:
            return f"every day at {clock}"
        if sorted(days) == [0, 1, 2, 3, 4]:
            return f"every weekday at {clock}"
        if sorted(days) == [5, 6]:
            return f"every weekend day at {clock}"
        return "every " + " and ".join(names[d] for d in sorted(days)) + f" at {clock}"
    return "unscheduled"


def _strip(text: str, m) -> str:
    out = (text[:m.start()] + " " + text[m.end():]).strip()
    return re.sub(r"\s{2,}", " ", out).strip(" ,.")


def parse_schedule(text: str, now: Optional[datetime] = None) -> Tuple[Optional[Dict], str]:
    now = now or datetime.now()
    s = text.strip()
    low = s.lower()

    # ---- recurring: every N units -------------------------------------
    m = re.search(rf"\bevery\s+(?:{_NUM}\s+)?{_UNIT}\b", low)
    if m and not re.search(r"\bevery\s+(day|week)\b", low[m.start():m.end()]):
        n = _num(m.group(1)) if m.group(1) else 1
        every = int(n * _unit_sec(m.group(2)))
        if every >= 60:
            return {"type": "interval", "every_sec": every, "start": now.timestamp() + every}, _strip(s, m)

    # ---- recurring: every <day part / day / weekday> [at time] ----------
    m = re.search(
        rf"\b(?:every|each|daily|on)\s*(morning|afternoon|evening|night|day|weekday|weekend|"
        rf"{_DAY_NAMES}(?:\s*(?:and|,)\s*{_DAY_NAMES})*s?)?\b(?:\s+(?:at|@)\s+{_CLOCK}|\s+(morning|afternoon|evening|night))?",
        low)
    if m and (m.group(0).startswith(("every", "each", "daily")) or
              (m.group(0).startswith("on") and "every" in low)):
        what = (m.group(1) or "day").strip()
        clock = None
        if m.group(4):
            hh, mm = _clock(m.group(4), m.group(5), m.group(6),
                            default_pm=what not in ("morning",))
            clock = _hhmm(hh, mm)
        days: Optional[List[int]] = None
        if what in _PARTS_OF_DAY:
            clock = clock or (_default_morning() if what == "morning" else _PARTS_OF_DAY[what])
        elif what == "weekday":
            days = [0, 1, 2, 3, 4]
        elif what == "weekend":
            days = [5, 6]
        elif what != "day":
            days = sorted({_DAYS[d.rstrip("s")] for d in re.findall(_DAY_NAMES + "s?", what)
                           if d.rstrip("s") in _DAYS})
        if m.group(7):
            clock = clock or _PARTS_OF_DAY[m.group(7)]
        if clock is None:
            tm = re.search(rf"\b(?:at|@)\s+{_CLOCK}", low)
            if tm:
                hh, mm = _clock(tm.group(1), tm.group(2), tm.group(3))
                clock = _hhmm(hh, mm)
                s2 = _strip(s, tm)
                m2 = re.search(re.escape(m.group(0)), s2.lower())
                rest = _strip(s2, m2) if m2 else s2
                return {"type": "daily", "time": clock, "days": days}, rest
            clock = _default_morning() if days is not None else "09:00"
        return {"type": "daily", "time": clock, "days": days}, _strip(s, m)

    # ---- one-shot: in N units -------------------------------------------
    m = re.search(rf"\b(?:in|after)\s+(half an hour|{_NUM}\s+{_UNIT}(?:\s+and\s+(?:a\s+)?(?:half|{_NUM}\s+{_UNIT}))?)\b", low)
    if m:
        expr = m.group(1)
        if expr == "half an hour":
            secs = 1800
        else:
            secs = 0
            for num, unit in re.findall(rf"{_NUM}\s+{_UNIT}", expr):
                secs += _num(num) * _unit_sec(unit)
            if re.search(r"and\s+(a\s+)?half", expr):
                first_unit = re.search(_UNIT, expr).group(1)
                secs += _unit_sec(first_unit) / 2
        if secs > 0:
            return {"type": "once", "at": (now + timedelta(seconds=secs)).timestamp()}, _strip(s, m)

    # ---- one-shot: [tomorrow|on <day>|tonight] [at time] ------------------
    day_offset = None
    day_match = None
    dm = re.search(r"\b(tomorrow|tonight|today|this (?:morning|afternoon|evening))\b", low)
    if dm:
        day_match = dm
        word = dm.group(1)
        day_offset = 1 if word == "tomorrow" else 0
    else:
        dm = re.search(rf"\b(?:on\s+|next\s+|this\s+)?{_DAY_NAMES}\b", low)
        if dm:
            day_match = dm
            target = _DAYS[dm.group(1)]
            day_offset = (target - now.weekday()) % 7
            if day_offset == 0 or "next" in dm.group(0):
                day_offset = day_offset or 7

    tm = re.search(rf"\b(?:at|@|by)\s+(?:{_CLOCK}|(noon|midday|midnight))", low)
    part = re.search(r"\b(morning|afternoon|evening|night)\b", low) if day_match else None
    if tm or day_match:
        hour = minute = None
        if tm:
            if tm.group(4):
                hour, minute = map(int, _PARTS_OF_DAY[tm.group(4)].split(":"))
            else:
                is_morning = bool(re.search(r"\bmorning\b", low))
                hour, minute = _clock(tm.group(1), tm.group(2), tm.group(3), default_pm=not is_morning)
        elif part or (day_match and day_match.group(0).startswith(("tonight", "this"))):
            key = (part.group(1) if part else day_match.group(0).split()[-1])
            key = "tonight" if key == "tonight" else key
            hh = _default_morning() if key == "morning" else _PARTS_OF_DAY.get(key, "09:00")
            hour, minute = map(int, hh.split(":"))
        else:
            hour, minute = map(int, _default_morning().split(":"))
        base = now + timedelta(days=day_offset or 0)
        at = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if day_offset is None and at <= now:
            at += timedelta(days=1)          # "at 5 pm" after 5 pm -> tomorrow
        elif at <= now:
            return None, text                # e.g. "today at 9" when it's already past
        rest = s
        for mm in sorted([x for x in (tm, day_match, part) if x], key=lambda x: -x.start()):
            rest = rest[:mm.start()] + " " + rest[mm.end():]
        rest = re.sub(r"\s{2,}", " ", rest).strip(" ,.")
        return {"type": "once", "at": at.timestamp()}, rest
    return None, text


# ---------------------------------------------------------------------- #
# Reminder phrasing
# ---------------------------------------------------------------------- #
def extract_reminder(text: str) -> Optional[Dict]:
    """'remind me at 5 PM to work on AIDE' -> {"schedule", "message"}.

    Returns None if the text isn't a reminder request. Returns a dict with
    schedule None if it is one but has no understandable time.
    """
    low = text.lower().strip()
    if not re.search(r"\b(remind me|reminder|timer|alarm|wake me)\b", low):
        return None
    if re.search(r"\b(cancel|delete|remove|stop|list|show|what|any)\b.*\b(reminders?|timers?|alarms?)\b", low):
        return None   # managing reminders, not creating one
    is_timer = bool(re.search(r"\b(timer|alarm)\b", low))
    work = text
    if is_timer:
        # "set a 5 minute timer" / "timer for 10 minutes" — the duration is a delay.
        work = re.sub(rf"\b{_NUM}[\s-]+{_UNIT}\s+(timer|alarm)\b", r"\3 in \1 \2", work, count=1, flags=re.I)
        work = re.sub(rf"\bfor\s+(?={_NUM}\s+{_UNIT})", "in ", work, count=1, flags=re.I)
        work = re.sub(r"\b(alarm|timer)\s+for\s+(?=\d)", r"\1 at ", work, count=1, flags=re.I)
    schedule, rest = parse_schedule(work)
    msg = rest
    msg = re.sub(r"^(?:hey\s+)?(?:saint[,\s]+)?(?:can you|could you|please|would you)?\s*", "", msg, flags=re.I)
    msg = re.sub(r"\b(?:remind me|set (?:a |an )?(?:reminder|timer|alarm)|create (?:a )?reminder|"
                 r"add (?:a )?reminder|wake me up|wake me|timer|alarm)\b", " ", msg, flags=re.I)
    msg = re.sub(r"^\s*(?:for|to|that|about|of)\b", " ", msg.strip(), flags=re.I)
    msg = re.sub(r"\b(?:for|to)\s*$", "", msg.strip(), flags=re.I)
    msg = re.sub(r"\s{2,}", " ", msg).strip(" ,.?!")
    msg = re.sub(r"^(to|that|about|a|an)\s+", "", msg, flags=re.I)
    msg = re.sub(r"^(a|an)$", "", msg, flags=re.I)
    if not msg:
        msg = ("Alarm" if "alarm" in low else "Timer finished") if is_timer else "Reminder"
    msg = re.sub(r"\bmy\b", "your", msg, flags=re.I)
    return {"schedule": schedule, "message": msg[0].upper() + msg[1:] if msg else msg, "timer": is_timer}

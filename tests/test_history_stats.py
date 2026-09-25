"""History page numbers: the per-day summary, streaks, heatmap levels, and
the heatmap widget itself."""

import json
import os
import time
from datetime import date, datetime, timedelta

import pytest

from core import history_stats as hs
from core.history import History, add_to_day


def _daily(counts_by_offset, today=date(2026, 9, 24)):
    return {(today - timedelta(days=o)).isoformat(): {"n": n} for o, n in counts_by_offset.items() if n}


def test_streaks():
    today = date(2026, 9, 24)
    assert hs.streaks({}, today) == (0, 0)
    # today, yesterday, the day before; then a gap; then 5 in a row
    d = _daily({0: 2, 1: 1, 2: 4, 5: 1, 6: 1, 7: 1, 8: 1, 9: 1}, today)
    assert hs.streaks(d, today) == (3, 5)
    # nothing yet today: yesterday's run still counts
    d = _daily({1: 1, 2: 1}, today)
    assert hs.streaks(d, today) == (2, 2)
    d = _daily({3: 1}, today)
    assert hs.streaks(d, today)[0] == 0


def test_calendar_is_whole_weeks_ending_today():
    today = date(2026, 9, 24)                      # a Thursday
    days = hs.calendar({"2026-09-24": {"n": 7}}, weeks=53, today=today)
    assert days[-1] == (today, 7)
    assert days[0][0].weekday() == 6               # starts on a Sunday
    assert len(days) == 52 * 7 + 5                 # Sunday..Thursday of this week


def test_levels_follow_quartiles():
    counts = [0, 1, 2, 3, 4, 5, 6, 7, 8, 40]
    t = hs.level_thresholds(counts)
    assert t == sorted(t) and len(set(t)) == 3
    assert hs.level(0, t) == 0 and hs.level(1, t) == 1 and hs.level(40, t) == 4
    assert hs.level_thresholds([0, 0]) == [1, 2, 3]
    assert hs.level_thresholds([5, 5, 5]) == [5, 5.5, 6.0]


def test_totals_and_busiest():
    d = {}
    base = datetime(2026, 9, 23, 21, 0).timestamp()
    for i, (src, ok) in enumerate([("voice", True), ("hotword", True), ("typed", False), ("voice", True)]):
        add_to_day(d, {"ts": base + i, "source": src, "ms": 1000, "tools": [{"tool": "x", "ok": ok}]})
    add_to_day(d, {"ts": base - 86400, "source": "scene", "tools": []})
    tot = hs.totals(d)
    assert tot["n"] == 5 and tot["by_source"]["voice"] == 2 and tot["by_source"]["scene"] == 1
    assert tot["success"] == 0.75 and tot["avg_ms"] == 1000
    assert tot["hours"][21] == 5                    # all five were at 9 PM
    assert hs.busiest_day(d) == (date(2026, 9, 23), 4)
    assert hs.percentile([100, 200, 300, 400, 5000], 0.5) == 300


def test_daily_summary_survives_trimming(tmp_path, monkeypatch):
    from core.config import config
    monkeypatch.setitem(config._data["history"], "max_entries", 10)
    h = History(tmp_path / "history.jsonl")
    old = time.time() - 40 * 86400
    for i in range(30):
        h.append({"ts": old + i, "source": "voice", "user": f"old {i}", "tools": []})
    for i in range(500):
        h.append({"ts": time.time() - i, "source": "typed", "user": "new", "tools": []})
    assert len(h.read()) < 530                      # the log was trimmed ...
    daily = h.daily()
    assert daily[time.strftime("%Y-%m-%d", time.localtime(old))]["n"] == 30   # ... the summary wasn't
    assert sum(v["n"] for v in daily.values()) == 530


def test_daily_summary_rebuilds_from_the_log(tmp_path):
    path = tmp_path / "history.jsonl"
    ts = datetime(2026, 9, 20, 10).timestamp()
    path.write_text("\n".join(json.dumps({"ts": ts + i, "source": "voice", "tools": []}) for i in range(3)) + "\n",
                    encoding="utf-8")
    h = History(path)
    assert h.daily()["2026-09-20"]["n"] == 3
    h.append({"ts": ts + 10, "source": "hotword", "tools": []})
    d = h.daily()["2026-09-20"]
    assert d["n"] == 4 and d["hotword"] == 1          # the rebuild didn't count the new line twice
    h.clear()
    assert h.daily() == {} and not os.path.exists(h.daily_path)


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_heatmap_widget_paints_and_explains_a_day(qapp):
    from PySide6.QtCore import QPointF
    from ui.widgets import Heatmap
    today = date(2026, 9, 24)
    days = hs.calendar(_daily({0: 5, 1: 2, 30: 9}, today), today=today)
    w = Heatmap()
    w.resize(900, 200)
    w.set_data(days, hs.level_thresholds([c for _, c in days]), today)
    assert not w.grab().isNull()
    # hover over today's square → tooltip names the day
    i = len(days) - 1
    col, row = w._pos(i)
    step = w._cell() + w.GAP
    x, y = w.LEFT + col * step + 2, w.TOP + row * step + 2
    assert w._index_at(x, y) == i
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import Qt, QEvent
    ev = QMouseEvent(QEvent.MouseMove, QPointF(x, y), QPointF(x, y), Qt.NoButton, Qt.NoButton, Qt.NoModifier)
    w.mouseMoveEvent(ev)
    assert w.toolTip().startswith("5 requests") and "Sep 24, 2026" in w.toolTip()

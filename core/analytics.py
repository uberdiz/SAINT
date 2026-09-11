"""
core/analytics.py

Internal analytics engine. Subscribes to the Event Bus and tallies
counters and latency metrics. Extended for Milestone 1 to track the full
voice pipeline: STT, TTS, model, overall latency, interruptions, etc.

Persists to data/analytics.json. Thread-safe via GIL (all mutations
happen in the Qt main thread via Signal dispatch).
"""

import json
import os
import time

from core.events import event_bus, EventType
from core.config import config

ANALYTICS_FILE = "data/analytics.json"

_MAX_SAMPLES = 200

_DEFAULT = {
    # --- legacy v0.1 --------------------------------------------------------
    "commands": 0,
    "llm_errors": 0,
    "module_crashes": 0,
    "events_total": 0,
    "response_times": [],           # rolling window, seconds

    # --- Milestone 1: latency buckets (ms) ----------------------------------
    "stt_latencies": [],
    "tts_latencies": [],
    "model_latencies": [],
    "overall_latencies": [],
    "wake_word_latencies": [],
    "intent_latencies": [],

    # --- Milestone 1: conversation counters ---------------------------------
    "interruptions": 0,
    "cancelled_tasks": 0,
    "false_wake_words": 0,
    "recognition_samples": [],      # list of {expected, got} or just confidence floats

    "session_start": time.time(),
}


def _avg(lst):
    return round(sum(lst) / len(lst), 1) if lst else 0.0


def _trim(lst, max_len=_MAX_SAMPLES):
    if len(lst) > max_len:
        del lst[: len(lst) - max_len]


class Analytics:
    def __init__(self):
        self._data = self._load()
        event_bus.event_occurred.connect(self._on_event)

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load(self):
        if os.path.exists(ANALYTICS_FILE):
            try:
                with open(ANALYTICS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                merged = dict(_DEFAULT)
                merged.update(data)
                merged["session_start"] = time.time()
                # ensure all list keys exist
                for k, v in _DEFAULT.items():
                    if isinstance(v, list) and k not in merged:
                        merged[k] = []
                return merged
            except (json.JSONDecodeError, OSError):
                pass
        return dict(_DEFAULT)

    def save(self):
        os.makedirs(os.path.dirname(ANALYTICS_FILE) or ".", exist_ok=True)
        with open(ANALYTICS_FILE, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    # ------------------------------------------------------------------ #
    # Event handler (runs on Qt main thread)
    # ------------------------------------------------------------------ #
    def _on_event(self, ev):
        if not config.get("analytics.enabled", True):
            return

        self._data["events_total"] += 1

        t = ev.type

        # --- legacy AI ---
        if t == EventType.AI_REQUEST:
            self._data["commands"] += 1
        elif t == EventType.AI_RESPONSE:
            elapsed = ev.payload.get("elapsed_seconds")
            if elapsed is not None:
                samples = self._data.setdefault("response_times", [])
                samples.append(elapsed)
                _trim(samples)
        elif t == EventType.AI_ERROR:
            self._data["llm_errors"] += 1
        elif t == EventType.MODULE_CRASH:
            self._data["module_crashes"] += 1

        # --- streaming AI ---
        elif t == EventType.AI_STREAM_DONE:
            elapsed = ev.payload.get("elapsed_seconds")
            if elapsed:
                samples = self._data.setdefault("model_latencies", [])
                samples.append(round(elapsed * 1000, 1))
                _trim(samples)
                self._data["commands"] += 1
        elif t == EventType.AI_CANCELLED:
            self._data["cancelled_tasks"] += 1

        # --- latency events ---
        elif t == EventType.LATENCY_STT:
            lst = self._data.setdefault("stt_latencies", [])
            lst.append(ev.payload.get("ms", 0))
            _trim(lst)
        elif t == EventType.LATENCY_TTS:
            lst = self._data.setdefault("tts_latencies", [])
            lst.append(ev.payload.get("ms", 0))
            _trim(lst)
        elif t == EventType.LATENCY_MODEL:
            lst = self._data.setdefault("model_latencies", [])
            lst.append(ev.payload.get("ms", 0))
            _trim(lst)
        elif t == EventType.LATENCY_OVERALL:
            lst = self._data.setdefault("overall_latencies", [])
            lst.append(ev.payload.get("ms", 0))
            _trim(lst)
        elif t == EventType.LATENCY_WAKE_WORD:
            lst = self._data.setdefault("wake_word_latencies", [])
            lst.append(ev.payload.get("ms", 0))
            _trim(lst)
        elif t == EventType.LATENCY_INTENT:
            lst = self._data.setdefault("intent_latencies", [])
            lst.append(ev.payload.get("ms", 0))
            _trim(lst)

        # --- voice events ---
        elif t == EventType.VOICE_INTERRUPT:
            self._data["interruptions"] += 1
        elif t == EventType.VOICE_FALSE_WAKE_WORD:
            self._data["false_wake_words"] += 1
        elif t == EventType.VOICE_STT_FINAL:
            conf = ev.payload.get("confidence")
            if conf is not None:
                samples = self._data.setdefault("recognition_samples", [])
                samples.append(conf)
                _trim(samples)

        self.save()

    # ------------------------------------------------------------------ #
    # Computed values
    # ------------------------------------------------------------------ #
    def average_response_time(self):
        """Legacy metric — seconds."""
        samples = self._data.get("response_times", [])
        if not samples:
            return 0.0
        return sum(samples) / len(samples)

    def runtime_formatted(self):
        secs = int(time.time() - self._data.get("session_start", time.time()))
        h, rem = divmod(secs, 3600)
        m, _s = divmod(rem, 60)
        return f"{h}h {m}m"

    def recognition_accuracy(self):
        """Average STT confidence as a 0-100 percentage."""
        samples = self._data.get("recognition_samples", [])
        if not samples:
            return 0.0
        return round(sum(samples) / len(samples) * 100, 1)

    def snapshot(self):
        d = self._data
        return {
            # legacy
            "runtime": self.runtime_formatted(),
            "commands": d.get("commands", 0),
            "average_response": round(self.average_response_time(), 2),
            "llm_errors": d.get("llm_errors", 0),
            "module_crashes": d.get("module_crashes", 0),
            "events_total": d.get("events_total", 0),

            # Milestone 1 latencies (ms)
            "avg_stt_ms": _avg(d.get("stt_latencies", [])),
            "avg_tts_ms": _avg(d.get("tts_latencies", [])),
            "avg_model_ms": _avg(d.get("model_latencies", [])),
            "avg_overall_ms": _avg(d.get("overall_latencies", [])),
            "avg_wake_word_ms": _avg(d.get("wake_word_latencies", [])),
            "avg_intent_ms": _avg(d.get("intent_latencies", [])),

            # Milestone 1 counters
            "interruptions": d.get("interruptions", 0),
            "cancelled_tasks": d.get("cancelled_tasks", 0),
            "false_wake_words": d.get("false_wake_words", 0),
            "recognition_accuracy": self.recognition_accuracy(),
        }


# Singleton
analytics = Analytics()

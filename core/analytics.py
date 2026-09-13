"""
core/analytics.py

Internal analytics engine. Subscribes to the Event Bus and tallies
counters and latency metrics. Extended to track:
- Sessions and interactions
- Tool usage and automation
- Memory operations
- Voice pipeline latencies
- Errors and warnings
"""

import json
import logging
import os
import time
from collections import defaultdict

from core.events import event_bus, EventType
from core.config import config

logger = logging.getLogger("saint.analytics")

ANALYTICS_FILE = "data/analytics.json"

_MAX_SAMPLES = 200
_MAX_TOOL_SAMPLES = 100

_DEFAULT = {
    # --- Sessions ---
    "sessions": [],                    # list of {start, end, duration, interactions}
    "current_session_start": time.time(),
    "total_sessions": 0,
    "total_duration": 0.0,

    # --- Interactions ---
    "interactions": 0,
    "successful_interactions": 0,
    "failed_interactions": 0,

    # --- Legacy ---
    "commands": 0,
    "llm_errors": 0,
    "module_crashes": 0,
    "events_total": 0,
    "response_times": [],

    # --- Latency buckets (ms) ---
    "stt_latencies": [],
    "tts_latencies": [],
    "tts_inference_latencies": [],
    "tts_playback_latencies": [],
    "model_latencies": [],
    "overall_latencies": [],
    "wake_word_latencies": [],
    "intent_latencies": [],
    "ai_first_token_latencies": [],

    # --- Voice/Conversation ---
    "interruptions": 0,
    "cancelled_tasks": 0,
    "false_wake_words": 0,
    "recognition_samples": [],

    # --- Tool Usage ---
    "tool_calls": defaultdict(int),          # tool_name -> count
    "tool_errors": defaultdict(int),         # tool_name -> error count
    "tool_latencies": defaultdict(list),     # tool_name -> [latency_ms]

    # --- Memory Operations ---
    "memory_writes": 0,
    "memory_reads": 0,
    "memory_searches": 0,
    "memory_errors": 0,

    # --- Automation ---
    "automation_commands": 0,
    "automation_errors": 0,

    # --- Token Statistics (if available) ---
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
}


def _avg(lst):
    return round(sum(lst) / len(lst), 1) if lst else 0.0


def _trim(lst, max_len=_MAX_SAMPLES):
    if len(lst) > max_len:
        del lst[: len(lst) - max_len]


def _trim_tool(lst, max_len=_MAX_TOOL_SAMPLES):
    if len(lst) > max_len:
        del lst[: len(lst) - max_len]


class Analytics:
    def __init__(self):
        self._data = self._load()
        event_bus.event_occurred.connect(self._on_event)
        self._last_save = 0
        self._save_interval = 0.5  # Save at most every 500ms
        # Events that fire too frequently to track individually
        self._high_freq_events = {
            EventType.VOICE_AUDIO_LEVEL,
            EventType.VOICE_STT_PARTIAL,
            EventType.AI_STREAM_TOKEN,
            EventType.TTS_SPEAK_CHUNK,
            EventType.TTS_INFERENCE_START,
            EventType.TTS_INFERENCE_END,
            EventType.TTS_AUDIO_READY,
            EventType.TTS_PLAYBACK_START,
            EventType.TTS_PLAYBACK_END,
            EventType.LATENCY_TTS_INFERENCE,
            EventType.LATENCY_TTS_PLAYBACK,
        }

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _load(self):
        if os.path.exists(ANALYTICS_FILE):
            try:
                with open(ANALYTICS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                merged = dict(_DEFAULT)
                # Convert defaultdicts back to dicts for JSON
                for k, v in data.items():
                    if isinstance(v, dict) and k in ("tool_calls", "tool_errors"):
                        merged[k] = defaultdict(int, v)
                    elif isinstance(v, dict) and k == "tool_latencies":
                        merged[k] = defaultdict(list, {k2: v2 for k2, v2 in v.items()})
                    else:
                        merged[k] = v
                merged["current_session_start"] = time.time()
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

        # Convert defaultdicts to dicts for JSON serialization
        save_data = dict(self._data)
        for k in ("tool_calls", "tool_errors"):
            if isinstance(save_data.get(k), defaultdict):
                save_data[k] = dict(save_data[k])
        if isinstance(save_data.get("tool_latencies"), defaultdict):
            save_data["tool_latencies"] = dict(save_data["tool_latencies"])

        # Direct write (simpler, avoids shutil.replace issue)
        try:
            with open(ANALYTICS_FILE, "w", encoding="utf-8") as f:
                json.dump(save_data, f, indent=2)
        except OSError as e:
            logger.warning(f"Failed to save analytics: {e}")

    # ------------------------------------------------------------------ #
    # Session Management
    # ------------------------------------------------------------------ #
    def start_session(self):
        """Start a new session."""
        self._end_current_session()
        self._data["current_session_start"] = time.time()

    def _end_current_session(self):
        if self._data.get("current_session_start"):
            duration = time.time() - self._data["current_session_start"]
            self._data["sessions"].append({
                "start": self._data["current_session_start"],
                "end": time.time(),
                "duration": duration,
                "interactions": self._data.get("interactions", 0),
            })
            self._data["total_sessions"] += 1
            self._data["total_duration"] += duration
            self._data["current_session_start"] = None

    # ------------------------------------------------------------------ #
    # Event handler (runs on Qt main thread)
    # ------------------------------------------------------------------ #
    def _on_event(self, ev):
        if not config.get("analytics.enabled", True):
            return

        self._data["events_total"] += 1

        t = ev.type

        # Skip high-frequency events (audio levels, tokens, etc.) to avoid disk thrashing
        if t in self._high_freq_events:
            self._maybe_save()
            return

        # --- Session events ---
        if t == EventType.APP_STARTED:
            self.start_session()

        # --- AI / Legacy ---
        elif t == EventType.AI_REQUEST:
            self._data["commands"] += 1
        elif t == EventType.AI_RESPONSE:
            elapsed = ev.payload.get("elapsed_seconds")
            if elapsed is not None:
                samples = self._data.setdefault("response_times", [])
                samples.append(elapsed)
                _trim(samples)
            self._data["interactions"] += 1
            self._data["successful_interactions"] += 1
        elif t == EventType.AI_ERROR:
            self._data["llm_errors"] += 1
            self._data["interactions"] += 1
            self._data["failed_interactions"] += 1
        elif t == EventType.MODULE_CRASH:
            self._data["module_crashes"] += 1

        # --- Streaming AI ---
        elif t == EventType.AI_STREAM_DONE:
            elapsed = ev.payload.get("elapsed_seconds")
            if elapsed:
                samples = self._data.setdefault("model_latencies", [])
                samples.append(round(elapsed * 1000, 1))
                _trim(samples)
                self._data["commands"] += 1
        elif t == EventType.LATENCY_AI_FIRST_TOKEN:
            ms = ev.payload.get("ms")
            if ms is not None:
                samples = self._data.setdefault("ai_first_token_latencies", [])
                samples.append(ms)
                _trim(samples)
        elif t == EventType.AI_CANCELLED:
            self._data["cancelled_tasks"] += 1
        elif t == EventType.LATENCY_OVERALL:
            ms = ev.payload.get("ms")
            if ms is not None:
                samples = self._data.setdefault("overall_latencies", [])
                samples.append(ms)
                _trim(samples)

        # --- Voice events ---
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
        elif t == EventType.LATENCY_TTS_INFERENCE:
            ms = ev.payload.get("ms")
            if ms is not None:
                samples = self._data.setdefault("tts_inference_latencies", [])
                samples.append(ms)
                _trim(samples)
        elif t == EventType.LATENCY_TTS_PLAYBACK:
            ms = ev.payload.get("ms")
            if ms is not None:
                samples = self._data.setdefault("tts_playback_latencies", [])
                samples.append(ms)
                _trim(samples)

        # --- Tool events ---
        elif t == "tool.called":
            tool_name = ev.payload.get("tool", "unknown")
            self._data["tool_calls"][tool_name] += 1
            latency = ev.payload.get("latency_ms")
            if latency is not None:
                lat_list = self._data["tool_latencies"][tool_name]
                lat_list.append(latency)
                _trim_tool(lat_list)
        elif t == "tool.error":
            tool_name = ev.payload.get("tool", "unknown")
            self._data["tool_errors"][tool_name] += 1

        # --- Memory events ---
        elif t == "memory.write":
            self._data["memory_writes"] += 1
        elif t == "memory.read":
            self._data["memory_reads"] += 1
        elif t == "memory.search":
            self._data["memory_searches"] += 1
        elif t == "memory.error":
            self._data["memory_errors"] += 1

        # --- Automation events ---
        elif t == "automation.command":
            self._data["automation_commands"] += 1
        elif t == "automation.error":
            self._data["automation_errors"] += 1

        # --- Token stats (if provided by provider) ---
        elif t == "token.stats":
            self._data["prompt_tokens"] += ev.payload.get("prompt_tokens", 0)
            self._data["completion_tokens"] += ev.payload.get("completion_tokens", 0)
            self._data["total_tokens"] += ev.payload.get("total_tokens", 0)

        self._maybe_save()

    def _maybe_save(self):
        """Save to disk at most every _save_interval seconds."""
        now = time.time()
        if now - self._last_save >= self._save_interval:
            self.save()
            self._last_save = now

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
        secs = int(time.time() - self._data.get("current_session_start", time.time()))
        h, rem = divmod(secs, 3600)
        m, _s = divmod(rem, 60)
        return f"{h}h {m}m"

    def recognition_accuracy(self):
        """Average STT confidence as a 0-100 percentage."""
        samples = self._data.get("recognition_samples", [])
        if not samples:
            return 0.0
        return round(sum(samples) / len(samples) * 100, 1)

    def session_stats(self):
        sessions = self._data.get("sessions", [])
        return {
            "total_sessions": self._data.get("total_sessions", 0),
            "total_duration": self._data.get("total_duration", 0),
            "avg_session_duration": self._data.get("total_duration", 0) / max(1, self._data.get("total_sessions", 1)),
            "current_session_duration": time.time() - self._data.get("current_session_start", time.time()) if self._data.get("current_session_start") else 0,
        }

    def tool_stats(self):
        return {
            "calls": dict(self._data.get("tool_calls", {})),
            "errors": dict(self._data.get("tool_errors", {})),
            "avg_latency_ms": {
                name: _avg(latencies) for name, latencies in self._data.get("tool_latencies", {}).items()
            },
        }

    def memory_stats(self):
        return {
            "writes": self._data.get("memory_writes", 0),
            "reads": self._data.get("memory_reads", 0),
            "searches": self._data.get("memory_searches", 0),
            "errors": self._data.get("memory_errors", 0),
        }

    def automation_stats(self):
        return {
            "commands": self._data.get("automation_commands", 0),
            "errors": self._data.get("automation_errors", 0),
        }

    def token_stats(self):
        return {
            "prompt": self._data.get("prompt_tokens", 0),
            "completion": self._data.get("completion_tokens", 0),
            "total": self._data.get("total_tokens", 0),
        }

    def snapshot(self):
        d = self._data
        return {
            # Sessions
            "runtime": self.runtime_formatted(),
            "total_sessions": d.get("total_sessions", 0),
            "total_duration": round(d.get("total_duration", 0) / 3600, 1),  # hours

            # Interactions
            "interactions": d.get("interactions", 0),
            "successful_interactions": d.get("successful_interactions", 0),
            "failed_interactions": d.get("failed_interactions", 0),

            # Legacy
            "commands": d.get("commands", 0),
            "average_response": round(self.average_response_time(), 2),
            "llm_errors": d.get("llm_errors", 0),
            "module_crashes": d.get("module_crashes", 0),
            "events_total": d.get("events_total", 0),

            # Latencies (ms)
            "avg_stt_ms": _avg(d.get("stt_latencies", [])),
            "avg_tts_ms": _avg(d.get("tts_latencies", [])),
            "avg_tts_inference_ms": _avg(d.get("tts_inference_latencies", [])),
            "avg_tts_playback_ms": _avg(d.get("tts_playback_latencies", [])),
            "avg_model_ms": _avg(d.get("model_latencies", [])),
            "avg_overall_ms": _avg(d.get("overall_latencies", [])),
            "avg_wake_word_ms": _avg(d.get("wake_word_latencies", [])),
            "avg_intent_ms": _avg(d.get("intent_latencies", [])),
            "avg_ai_first_token_ms": _avg(d.get("ai_first_token_latencies", [])),

            # Voice/Conversation
            "interruptions": d.get("interruptions", 0),
            "cancelled_tasks": d.get("cancelled_tasks", 0),
            "false_wake_words": d.get("false_wake_words", 0),
            "recognition_accuracy": self.recognition_accuracy(),

            # Tools
            "tool_calls": dict(d.get("tool_calls", {})),
            "tool_errors": dict(d.get("tool_errors", {})),

            # Memory
            "memory_writes": d.get("memory_writes", 0),
            "memory_reads": d.get("memory_reads", 0),
            "memory_searches": d.get("memory_searches", 0),

            # Automation
            "automation_commands": d.get("automation_commands", 0),
            "automation_errors": d.get("automation_errors", 0),

            # Tokens
            "prompt_tokens": d.get("prompt_tokens", 0),
            "completion_tokens": d.get("completion_tokens", 0),
            "total_tokens": d.get("total_tokens", 0),
        }


# Singleton
analytics = Analytics()
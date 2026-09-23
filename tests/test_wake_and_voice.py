"""Wake word (real ONNX model), echo gate, and the voice listening state machine."""

import time

import numpy as np
import pytest

from core.audio_echo import EchoGate, PlaybackMonitor
from core.config import config
from modules.voice.module import VoiceModule, ListenPhase
from modules.voice.wake_word import OnnxWakeWordDetector


# --------------------------------------------------------------------- wake model
def test_real_model_loads_and_is_quiet_on_silence():
    det = OnnxWakeWordDetector(threshold=0.5)
    assert det.ready, det.status.error
    silence = np.zeros(16000 * 3, dtype=np.int16)
    for i in range(0, len(silence), 480):
        assert det.feed(silence[i:i + 480]) is None
    assert det.last_score < 0.1
    assert det.avg_infer_ms < 40          # comfortably real-time on CPU


def test_noise_does_not_trigger():
    det = OnnxWakeWordDetector(threshold=0.5, trigger_frames=2)
    rng = np.random.default_rng(0)
    noise = (rng.standard_normal(16000 * 5) * 2000).astype(np.int16)
    fired = [det.feed(noise[i:i + 480]) for i in range(0, len(noise), 480)]
    assert not any(f is not None for f in fired)


def test_missing_model_reports_error(tmp_path):
    det = OnnxWakeWordDetector(model_path=str(tmp_path / "nope.onnx"))
    assert not det.ready and det.status.code == "MODEL_MISSING"
    assert "not found" in det.status.error
    assert det.feed(np.zeros(1280, dtype=np.int16)) is None


def test_trigger_frames_and_refractory():
    det = OnnxWakeWordDetector(threshold=0.5, trigger_frames=2, refractory_sec=2.0)
    scores = iter([0.9, 0.1, 0.9, 0.9, 0.9, 0.9])
    det._process_frame = lambda frame: next(scores)
    frame = np.zeros(1280, dtype=np.int16)
    out = [det.feed(frame) for _ in range(6)]
    # single spike ignored; two in a row fire once; cooldown suppresses repeats
    assert out[:3] == [None, None, None] and out[3] == 0.9 and out[4:] == [None, None]


# --------------------------------------------------------------------- echo gate
def test_echo_gate_ignores_own_voice_but_detects_user():
    gate = EchoGate(margin=2.5, min_ms=240, frame_ms=30, initial_coupling=0.3)
    # SAINT playing at 0.1 RMS, mic hears echo at ~0.03 -> never a barge-in
    assert not any(gate.update(0.03, 0.1) for _ in range(100))
    # User talks loudly over it for > 240 ms -> barge-in
    fired = [gate.update(0.25, 0.1) for _ in range(10)]
    assert any(fired)


def test_playback_monitor():
    m = PlaybackMonitor()
    assert not m.active()
    m.note_block(0.2, 0.3)
    assert m.active() and m.level() == pytest.approx(0.2)


# --------------------------------------------------------------------- phases
class FakeWake:
    ready = True
    threshold = 0.5

    def __init__(self):
        self.fire_on = None
        self.n = 0
        self.status = type("S", (), {"ready": True, "error": "", "code": "", "model_path": "x",
                                     "threshold": 0.5})()
        self.avg_infer_ms = 0.1

    def feed(self, chunk):
        self.n += 1
        return 0.9 if self.fire_on is not None and self.n == self.fire_on else None

    def take_peak(self):
        return 0.0


class FakeSTT:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def transcribe(self, audio, sample_rate=16000):
        self.calls += 1
        return type("R", (), {"text": self.text, "confidence": 0.9})()


def _voice(stt_text):
    from modules.voice.vad import RmsVAD
    v = VoiceModule()
    v._vad = RmsVAD(threshold=0.015, noise_suppression=False, start_threshold=0.015, end_threshold=0.0075)
    v._stt = FakeSTT(stt_text)
    v._wake = FakeWake()
    v._listening = v._voice_active = True
    v._actual_sr = 16000
    v._phase = ListenPhase.WAKE
    return v


def _run(v, frames):
    import threading
    stop = threading.Event()
    v._stop_event = stop
    for f in frames:
        v._audio_queue.put(f)
    t = threading.Thread(target=v._process_loop, daemon=True)
    t.start()
    deadline = time.time() + 5
    while not v._audio_queue.empty() and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.4)
    stop.set()
    t.join(2)


def _speech(n):   # n x 30 ms of loud tone
    t = np.arange(480) / 16000
    return [(np.sin(2 * np.pi * 220 * t) * 8000).astype(np.int16) for _ in range(n)]


def _silence(n):
    return [np.zeros(480, dtype=np.int16) for _ in range(n)]


def test_background_speech_without_wake_word_is_never_transcribed():
    config.set("voice.wake_word_chime", False, persist=False)
    v = _voice("some background talk")
    _run(v, _speech(40) + _silence(40))
    assert v._stt.calls == 0 and v.phase == ListenPhase.WAKE


def test_wake_then_command_is_transcribed_and_returns_to_wake():
    from core.events import event_bus, EventType
    finals = []
    handler = lambda ev: finals.append(ev.payload) if ev.type == EventType.VOICE_STT_FINAL else None
    event_bus.subscribe(handler)
    try:
        config.set("voice.wake_word_chime", False, persist=False)
        v = _voice("Hey Saint, play some jazz.")
        v._wake.fire_on = 20          # wake fires mid-utterance
        _run(v, _speech(40) + _silence(40))
        deadline = time.time() + 3
        while not finals and time.time() < deadline:
            time.sleep(0.02)
        assert v._stt.calls == 1
        assert finals and finals[-1]["text"] == "play some jazz." and finals[-1]["wake"] is True
        assert v.phase == ListenPhase.WAKE
    finally:
        event_bus.unsubscribe(handler)


def test_command_timeout_returns_to_wake():
    config.set("voice.wake_word_chime", False, persist=False)
    config.set("voice.wake_word_command_timeout_sec", 0.3, persist=False)
    try:
        v = _voice("")
        v._wake.fire_on = 3
        _run(v, _silence(10) + _silence(30))
        time.sleep(0.5)
        v._check_command_timeout(False)
        assert v.phase == ListenPhase.WAKE and v._stt.calls == 0
    finally:
        config.set("voice.wake_word_command_timeout_sec", 6.0, persist=False)


def test_strip_wake_prefix():
    v = VoiceModule.__new__(VoiceModule)
    assert v._strip_wake_prefix("Hey Saint, skip this song.") == "skip this song."
    assert v._strip_wake_prefix("Saint.") == ""
    assert v._strip_wake_prefix("St. Louis weather") == "St. Louis weather"
    assert v._strip_wake_prefix("paint the fence") == "paint the fence"

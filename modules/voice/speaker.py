"""
modules/voice/speaker.py

"Is that the user talking?" — a small voice profile built on this PC from the
user's own commands, used to tell them apart from song vocals, videos and
other people in the room. Pure numpy (no SciPy / model download):

    each utterance -> MFCCs (20) + deltas -> mean and spread  = a 76-number voiceprint
    profile        =  running average of voiceprints of utterances that began
                      with "Hey SAINT" (the user, by definition) or were
                      recorded in Settings > Voice > "Learn my voice"
    score          =  cosine similarity to the profile (1.0 = same voice)

It's a coarse signal, not security: it is only used to *drop* speech that
clearly isn't the user (open mic / talk-over / singing detection), and only
once the profile has enough samples. Stored in data/voice_profile.json
(numbers only — no audio is kept).
"""

import json
import logging
import os
import threading
from typing import Optional

import numpy as np

from core.config import config
from core.paths import data_path

log = logging.getLogger("saint.voice")

SR = 16000
_N_FFT = 512
_HOP = 160
_N_MELS = 40
_N_MFCC = 20
MIN_SAMPLES = 8


def _mel_filters() -> np.ndarray:
    def hz_to_mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel_to_hz(m):
        return 700.0 * (10 ** (m / 2595.0) - 1.0)
    mels = np.linspace(hz_to_mel(60.0), hz_to_mel(7600.0), _N_MELS + 2)
    bins = np.floor((_N_FFT + 1) * mel_to_hz(mels) / SR).astype(int)
    fb = np.zeros((_N_MELS, _N_FFT // 2 + 1), dtype=np.float32)
    for i in range(1, _N_MELS + 1):
        a, b, c = bins[i - 1], bins[i], bins[i + 1]
        for k in range(a, b):
            fb[i - 1, k] = (k - a) / max(1, b - a)
        for k in range(b, c):
            fb[i - 1, k] = (c - k) / max(1, c - b)
    return fb


def _dct_matrix() -> np.ndarray:
    n = np.arange(_N_MELS)
    k = np.arange(_N_MFCC)[:, None]
    return (np.cos(np.pi * k * (2 * n + 1) / (2 * _N_MELS)) * np.sqrt(2.0 / _N_MELS)).astype(np.float32)


_FB = _mel_filters()
_DCT = _dct_matrix()
_WIN = np.hanning(_N_FFT).astype(np.float32)


def voiceprint(audio: np.ndarray, sample_rate: int = SR) -> Optional[np.ndarray]:
    """76-number summary of how this voice sounds (None if too little speech)."""
    x = np.asarray(audio)
    if x.dtype == np.int16:
        x = x.astype(np.float32) / 32768.0
    x = x.astype(np.float32).ravel()
    if sample_rate != SR or len(x) < SR * 0.6:
        return None
    x = np.append(x[0], x[1:] - 0.97 * x[:-1])                 # pre-emphasis
    n = 1 + (len(x) - _N_FFT) // _HOP
    if n < 30:
        return None
    idx = np.arange(_N_FFT)[None, :] + _HOP * np.arange(n)[:, None]
    frames = x[idx] * _WIN
    energy = (frames ** 2).mean(axis=1)
    # Only voiced frames: silence and breaths say nothing about the voice.
    keep = energy > max(1e-7, np.percentile(energy, 40))
    if keep.sum() < 20:
        return None
    spec = np.abs(np.fft.rfft(frames[keep], n=_N_FFT)) ** 2
    mel = np.log(spec @ _FB.T + 1e-8)
    mfcc = mel @ _DCT.T
    mfcc -= mfcc.mean(axis=0)                                    # cepstral mean normalisation (mic/room)
    delta = np.gradient(mfcc, axis=0)
    # The mean is ~0 after normalisation; the shape of the spread and of the raw
    # (un-normalised) spectral tilt carry the voice.
    raw = (mel @ _DCT.T)[:, 1:]
    blocks = [raw.mean(axis=0), mfcc.std(axis=0)[1:], delta.std(axis=0)[1:],
              np.percentile(raw, 85, axis=0) - np.percentile(raw, 15, axis=0)]
    # Each block counts equally (their scales differ by 10x).
    vec = np.concatenate([b / (np.linalg.norm(b) or 1.0) for b in blocks]).astype(np.float32)
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else None


class SpeakerProfile:
    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.Lock()
        self._mean: Optional[np.ndarray] = None
        self._n = 0
        self._self_scores: list = []      # how well the user's own voice matched (sets the threshold)
        self._loaded = False

    @property
    def path(self) -> str:
        return self._path or data_path("voice_profile.json")

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self._mean = np.asarray(d["mean"], dtype=np.float32)
            self._n = int(d.get("n", 0))
            self._self_scores = [float(x) for x in d.get("self_scores", [])][-40:]
        except (OSError, ValueError, KeyError):
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"mean": [round(float(v), 5) for v in self._mean], "n": self._n,
                           "self_scores": [round(x, 4) for x in self._self_scores[-40:]]}, f)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("voice.profile.save_failed %s", e)

    def _unit_mean(self) -> Optional[np.ndarray]:
        if self._mean is None:
            return None
        return self._mean / (np.linalg.norm(self._mean) or 1.0)

    @property
    def samples(self) -> int:
        with self._lock:
            self._load()
            return self._n

    @property
    def ready(self) -> bool:
        return self.samples >= MIN_SAMPLES

    def threshold(self) -> float:
        """Below this it isn't the user: set from how their own voice scores on
        this mic (mean - 3 spreads), so a noisy headset isn't held to a studio bar."""
        fixed = config.get("voice.speaker_threshold", None)
        if fixed:
            return float(fixed)
        with self._lock:
            self._load()
            sc = list(self._self_scores)
        if len(sc) < MIN_SAMPLES:
            return 0.88
        m, sd = float(np.mean(sc)), float(np.std(sc))
        return max(0.7, min(0.95, m - 3.0 * max(sd, 0.01)))

    def learn(self, audio: np.ndarray, sample_rate: int = SR) -> bool:
        """Add an utterance known to be the user's."""
        vp = voiceprint(audio, sample_rate)
        if vp is None:
            return False
        with self._lock:
            self._load()
            mean = self._unit_mean()
            if mean is not None:
                sim = float(np.dot(vp, mean))
                # Something that doesn't sound like them at all (the TV said "Hey
                # Saint"?) isn't averaged in once the profile is established.
                if self._n >= MIN_SAMPLES and sim < 0.6:
                    return False
                if self._n >= 3:
                    self._self_scores = (self._self_scores + [sim])[-40:]
            w = 1.0 / min(self._n + 1, 50)                      # recent voice counts more after 50
            self._mean = vp if self._mean is None else (1 - w) * self._mean + w * vp
            self._n += 1
            self._save()
        return True

    def score(self, audio: np.ndarray, sample_rate: int = SR) -> Optional[float]:
        """Cosine similarity to the user's voice, or None when unknown."""
        with self._lock:
            self._load()
            if self._n < MIN_SAMPLES:
                return None
            mean = self._unit_mean()
        vp = voiceprint(audio, sample_rate)
        return None if vp is None or mean is None else float(np.dot(vp, mean))

    def is_user(self, audio: np.ndarray, sample_rate: int = SR) -> Optional[bool]:
        """True / False, or None when there's no profile yet or too little audio."""
        s = self.score(audio, sample_rate)
        if s is None:
            return None
        return s >= self.threshold()

    def reset(self):
        with self._lock:
            self._mean, self._n, self._self_scores, self._loaded = None, 0, [], True
            try:
                os.remove(self.path)
            except OSError:
                pass


speaker_profile = SpeakerProfile()

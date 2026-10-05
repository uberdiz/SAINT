"""
modules/voice/voices.py

The Kokoro voices SAINT offers (English ones: SAINT's pipeline speaks English),
and how a blend of two is written for Kokoro. Kokoro averages the voices
in a comma list, so "af_heart,af_heart,af_heart,am_michael" is 75% / 25%.
"""

KOKORO_VOICES = [
    ("af_heart", "Heart — US, warm (default)"),
    ("af_bella", "Bella — US, bright"),
    ("af_nicole", "Nicole — US, soft/whispery"),
    ("af_sarah", "Sarah — US, clear"),
    ("af_sky", "Sky — US, light"),
    ("af_nova", "Nova — US, crisp"),
    ("af_alloy", "Alloy — US, even"),
    ("af_aoede", "Aoede — US, calm"),
    ("af_jessica", "Jessica — US, friendly"),
    ("af_kore", "Kore — US, steady"),
    ("af_river", "River — US, relaxed"),
    ("am_michael", "Michael — US, deep"),
    ("am_adam", "Adam — US, casual"),
    ("am_eric", "Eric — US, confident"),
    ("am_liam", "Liam — US, young"),
    ("am_onyx", "Onyx — US, low"),
    ("am_echo", "Echo — US, smooth"),
    ("am_fenrir", "Fenrir — US, rough"),
    ("am_puck", "Puck — US, playful"),
    ("bf_emma", "Emma — UK, polished"),
    ("bf_isabella", "Isabella — UK, warm"),
    ("bf_alice", "Alice — UK, crisp"),
    ("bf_lily", "Lily — UK, gentle"),
    ("bm_george", "George — UK, classic"),
    ("bm_lewis", "Lewis — UK, deep"),
    ("bm_daniel", "Daniel — UK, calm"),
    ("bm_fable", "Fable — UK, storyteller"),
]
VOICE_IDS = {v for v, _ in KOKORO_VOICES}


def kokoro_voice(primary: str, blend: str = "", blend_pct: int = 50) -> str:
    """The voice string Kokoro takes: one voice, or a weighted mix of two."""
    primary = (primary or "af_heart").strip()
    blend = (blend or "").strip()
    if not blend or blend == primary or "," in primary:
        return primary
    parts = max(1, min(3, round(int(blend_pct or 50) / 25)))      # quarters of the mix
    return ",".join([primary] * (4 - parts) + [blend] * parts)

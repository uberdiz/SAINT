import numpy as np
from core.events import event_bus, EventType

class AudioEchoCanceller:
    def __init__(self):
        self.tts_reference = None
        event_bus.event_occurred.connect(self._on_tts_audio)

    def _on_tts_audio(self, event):
        if event.type == EventType.TTS_AUDIO_READY:
            self.tts_reference = event.payload.get('samples')

    def is_echo(self, mic_audio):
        if self.tts_reference is None:
            return False
        # Simple correlation-based echo detection
        corr = np.correlate(mic_audio, self.tts_reference, mode='valid')
        return np.max(corr) > 0.7 * len(mic_audio)

# Initialize in app.py
echo_canceller = AudioEchoCanceller()

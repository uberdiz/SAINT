"""
ui/workers.py

QThread wrappers for background operations so the Qt UI thread never blocks.

AIWorker        — legacy synchronous AI call (used by Dashboard quick-test)
StreamingAIWorker — streaming AI call with per-token signals
"""

from PySide6.QtCore import QThread, Signal


class AIWorker(QThread):
    finished_ok = Signal(str)
    finished_error = Signal(str)

    def __init__(self, ai_module, prompt, parent=None):
        super().__init__(parent)
        self.ai_module = ai_module
        self.prompt = prompt

    def run(self):
        try:
            response = self.ai_module.send_prompt(self.prompt)
            self.finished_ok.emit(response)
        except Exception as e:  # noqa: BLE001
            self.finished_error.emit(str(e))


class StreamingAIWorker(QThread):
    """
    Runs ai_module.stream_prompt() in a thread.
    Emits token_received per token so the UI can update live.
    Emits finished_ok(full_text) when done.
    Emits finished_error(error_str) on failure.
    Emits cancelled() if the cancel flag was set.
    """
    token_received = Signal(str)
    finished_ok = Signal(str)
    finished_error = Signal(str)
    cancelled = Signal()

    def __init__(self, ai_module, prompt, is_interruption=False, parent=None):
        super().__init__(parent)
        self.ai_module = ai_module
        self.prompt = prompt
        self.is_interruption = is_interruption
        self._full_text = []

    def run(self):
        try:
            def on_token(tok):
                self._full_text.append(tok)
                self.token_received.emit(tok)

            def on_done(full):
                pass  # full_text already accumulated

            self.ai_module.stream_prompt(
                prompt=self.prompt,
                on_token=on_token,
                on_done=on_done,
                is_interruption=self.is_interruption,
            )
            full = "".join(self._full_text)
            if self.ai_module._cancel_flag.is_set():
                self.cancelled.emit()
            else:
                self.finished_ok.emit(full)
        except Exception as e:  # noqa: BLE001
            self.finished_error.emit(str(e))

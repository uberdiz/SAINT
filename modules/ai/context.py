"""
modules/ai/context.py

Conversation context manager for Milestone 1.

Maintains a rolling window of turns (user + assistant) and handles
interruptions gracefully. When the user interrupts, we record it as a
special turn so the model understands it should pivot without apologising.
"""

import threading
from typing import List, Dict


class ConversationContext:
    """Thread-safe rolling-window conversation history."""

    def __init__(self, max_turns: int = 6, system_prompt: str = ""):
        self._lock = threading.Lock()
        self._system_prompt = system_prompt
        self._max_turns = max_turns   # each turn = 1 user + 1 assistant message

        # Internal history: list of {"role": "user"|"assistant", "content": str}
        self._history: List[Dict[str, str]] = []

        # Metadata for analytics / acceptance tests
        self.current_topic: str = ""
        self.current_question: str = ""
        self.previous_answer: str = ""
        self.previous_interruption: str = ""

    # ------------------------------------------------------------------ #
    # Mutations
    # ------------------------------------------------------------------ #
    def add_user_turn(self, text: str):
        with self._lock:
            self.current_question = text
            self._history.append({"role": "user", "content": text})
            self._trim()

    def add_assistant_turn(self, text: str):
        with self._lock:
            self.previous_answer = text
            self._history.append({"role": "assistant", "content": text})
            self._trim()

    def handle_interruption(self, new_user_text: str):
        """
        Called when the user interrupts SAINT mid-sentence.

        Inserts a special marker so the LLM understands the context:
        the previous assistant response was cut off, and the user is
        redirecting. The model should pivot naturally, not apologise.
        """
        with self._lock:
            self.previous_interruption = new_user_text

            # Mark the incomplete assistant turn (if any trailing assistant entry)
            if self._history and self._history[-1]["role"] == "assistant":
                partial = self._history[-1]["content"]
                self._history[-1]["content"] = partial + " [interrupted]"
            elif self._history and self._history[-1]["role"] == "user":
                # No assistant turn yet — just add the new user text below
                pass

            self._history.append({
                "role": "user",
                "content": new_user_text,
            })
            self.current_question = new_user_text
            self._trim()

    def clear(self):
        with self._lock:
            self._history.clear()
            self.current_topic = ""
            self.current_question = ""
            self.previous_answer = ""
            self.previous_interruption = ""

    # ------------------------------------------------------------------ #
    # Formatting for the LLM
    # ------------------------------------------------------------------ #
    def format_messages(self) -> List[Dict[str, str]]:
        """Returns the message list suitable for an OpenAI-style API."""
        with self._lock:
            messages = []
            if self._system_prompt:
                messages.append({"role": "system", "content": self._system_prompt})
            messages.extend(list(self._history))
            return messages

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #
    def _trim(self):
        """Keep at most max_turns * 2 messages (pairs of user + assistant)."""
        max_msgs = self._max_turns * 2
        if len(self._history) > max_msgs:
            self._history = self._history[-max_msgs:]

    def __len__(self):
        with self._lock:
            return len(self._history)

"""
modules/ai/module.py

AI module — extended for Milestone 1.

New capabilities:
  - stream_prompt(): streams tokens via callback, fires AI_STREAM_TOKEN events
  - cancel(): sets the cancel flag so an in-flight stream exits cleanly
  - Conversation context tracked via ConversationContext
"""

import threading
import time
from typing import Callable, Optional

from modules.base import BaseModule
from modules.ai.providers import get_provider, ProviderError
from modules.ai.context import ConversationContext
from core.events import event_bus, EventType
from core.config import config


class AIModule(BaseModule):
    name = "AI"
    description = "Sends prompts to a configured LLM provider and returns responses."

    def __init__(self):
        super().__init__()
        self.subtasks = {
            "Module Loads": True,
            "Sends Prompt": True,
            "Receives Response": True,
            "Logs Response": True,
            "Error Handling": True,
            "Configurable Model": True,
            "Streaming": True,
            "Context Window": True,
            "Token Stats": False,
        }
        self._cancel_flag = threading.Event()
        self._context: Optional[ConversationContext] = None
        self._rebuild_context()

    # ------------------------------------------------------------------ #
    # Context
    # ------------------------------------------------------------------ #
    def _rebuild_context(self):
        max_turns = config.get("voice.max_context_turns", 6)
        system_prompt = config.get(
            "voice.system_prompt",
            "You are SAINT, a helpful AI assistant. Be concise.",
        )
        self._context = ConversationContext(max_turns=max_turns, system_prompt=system_prompt)

    @property
    def context(self) -> ConversationContext:
        return self._context

    # ------------------------------------------------------------------ #
    # Legacy synchronous API (kept for dashboard quick-test panel)
    # ------------------------------------------------------------------ #
    def send_prompt(self, prompt: str) -> str:
        """Synchronous call — intended to be run from a worker thread."""
        event_bus.emit_event(EventType.AI_REQUEST, {"prompt_length": len(prompt)})

        provider_name = config.get("ai.provider", "mock")
        model = config.get("ai.model", "llama3")
        api_key = config.get("ai.api_key", "")
        base_url = config.get("ai.base_url", "http://localhost:11434")
        temperature = config.get("ai.temperature", 0.7)
        timeout = config.get("ai.timeout_seconds", 30)

        provider = get_provider(provider_name)

        # Build messages from context
        self._context.add_user_turn(prompt)
        messages = self._context.format_messages()

        start = time.time()
        try:
            response_text = provider.send(
                messages=messages,
                model=model,
                api_key=api_key,
                base_url=base_url,
                temperature=temperature,
                timeout=timeout,
            )
        except ProviderError as e:
            event_bus.emit_event(EventType.AI_ERROR, {"error": str(e)})
            raise
        except Exception as e:
            event_bus.emit_event(EventType.AI_ERROR, {"error": f"Unexpected error: {e}"})
            raise

        elapsed = time.time() - start
        self._context.add_assistant_turn(response_text)
        event_bus.emit_event(
            EventType.AI_RESPONSE,
            {"elapsed_seconds": round(elapsed, 3), "response_length": len(response_text)},
        )
        return response_text

    # ------------------------------------------------------------------ #
    # Streaming API
    # ------------------------------------------------------------------ #
    def stream_prompt(
        self,
        prompt: str,
        on_token: Callable[[str], None],
        on_done: Optional[Callable[[str], None]] = None,
        is_interruption: bool = False,
        interrupted_text: str = "",
    ) -> None:
        """
        Stream response tokens via on_token(). Meant to be called from a
        worker thread. When done, calls on_done(full_text).

        is_interruption=True means the user cut off SAINT mid-sentence;
        handle_interruption() is used instead of add_user_turn().
        """
        self._cancel_flag.clear()

        provider_name = config.get("ai.provider", "mock")
        model = config.get("ai.model", "llama3")
        api_key = config.get("ai.api_key", "")
        base_url = config.get("ai.base_url", "http://localhost:11434")
        temperature = config.get("ai.temperature", 0.7)
        timeout = config.get("ai.timeout_seconds", 60)

        provider = get_provider(provider_name)

        if is_interruption:
            self._context.handle_interruption(prompt)
        else:
            self._context.add_user_turn(prompt)

        messages = self._context.format_messages()

        event_bus.emit_event(EventType.AI_REQUEST, {"prompt_length": len(prompt)})
        start = time.time()

        try:
            full_text = provider.stream_send(
                messages=messages,
                model=model,
                api_key=api_key,
                base_url=base_url,
                temperature=temperature,
                timeout=timeout,
                on_token=lambda tok: (
                    on_token(tok),
                    event_bus.emit_event(EventType.AI_STREAM_TOKEN, {"token": tok}),
                ),
                cancel_flag=self._cancel_flag,
            )
        except ProviderError as e:
            event_bus.emit_event(EventType.AI_ERROR, {"error": str(e)})
            raise
        except Exception as e:
            event_bus.emit_event(EventType.AI_ERROR, {"error": f"Unexpected error: {e}"})
            raise

        elapsed = time.time() - start

        if self._cancel_flag.is_set():
            event_bus.emit_event(EventType.AI_CANCELLED, {"reason": "interrupted"})
        else:
            self._context.add_assistant_turn(full_text)
            event_bus.emit_event(EventType.AI_STREAM_DONE, {
                "elapsed_seconds": round(elapsed, 3),
                "response_length": len(full_text),
            })
            event_bus.emit_event(EventType.LATENCY_MODEL, {"ms": round(elapsed * 1000, 1)})

        if on_done:
            on_done(full_text)

    def cancel(self):
        """Signal the in-flight stream to stop at the next token boundary."""
        self._cancel_flag.set()
        event_bus.emit_event(EventType.AI_CANCELLED, {"reason": "user_cancel"})

    def reset_context(self):
        """Clear conversation history."""
        self._rebuild_context()

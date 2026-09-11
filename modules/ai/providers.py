"""
modules/ai/providers.py

Abstract AI provider interface. Extended for Milestone 1 with streaming
support: stream_send() calls on_token() for each token received, enabling
the TTS engine to start speaking before the full response arrives.

Backends:
  mock    — simulates token-by-token output, no network, no GPU
  openai  — OpenAI-compatible SSE streaming
  ollama  — Ollama NDJSON streaming (local GPU)
"""

import json
import time
from typing import Callable, List, Dict, Optional

import requests


class ProviderError(Exception):
    pass


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
class AIProvider:
    def send(self, messages, model, api_key, base_url, temperature, timeout) -> str:
        raise NotImplementedError

    def stream_send(
        self,
        messages: List[Dict],
        model: str,
        api_key: str,
        base_url: str,
        temperature: float,
        timeout: float,
        on_token: Callable[[str], None],
        cancel_flag,           # threading.Event — set to abort
    ) -> str:
        """Stream tokens via on_token(); return full accumulated text."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Mock
# ---------------------------------------------------------------------------
MOCK_RESPONSES = {
    "python": "Python is a high-level, interpreted programming language known for its clean syntax and vast ecosystem.",
    "lua": "Lua is a lightweight, embeddable scripting language popular in game development and embedded systems.",
    "chrome": "Opening Chrome.",
    "firefox": "Stopping... Opening Firefox.",
    "default": "I understand. Let me help you with that.",
}


def _pick_mock_response(messages: List[Dict]) -> str:
    last_user = ""
    for m in reversed(messages):
        if m["role"] == "user":
            last_user = m["content"].lower()
            break
    # Check for interruptions in history
    has_interruption = any("[interrupted]" in m.get("content", "") for m in messages)

    if "lua" in last_user:
        return "Sure, Lua is a lightweight scripting language widely used in game engines and embedded systems. It's fast, minimal, and easy to embed."
    if "python" in last_user:
        return "Python is a high-level programming language celebrated for its readability and massive ecosystem of libraries."
    if "firefox" in last_user:
        prefix = "Stopping... " if has_interruption else ""
        return prefix + "Opening Firefox."
    if "chrome" in last_user:
        return "Opening Chrome."
    return MOCK_RESPONSES["default"]


class MockProvider(AIProvider):
    """Simulates streaming by emitting words with small delays. No network required."""

    WORD_DELAY = 0.04   # seconds between words (simulates ~25 tokens/s)

    def send(self, messages, model, api_key, base_url, temperature, timeout) -> str:
        time.sleep(0.3)
        return _pick_mock_response(messages)

    def stream_send(self, messages, model, api_key, base_url, temperature, timeout,
                    on_token, cancel_flag) -> str:
        text = _pick_mock_response(messages)
        accumulated = []
        words = text.split()
        for i, word in enumerate(words):
            if cancel_flag and cancel_flag.is_set():
                break
            token = (word + " ") if i < len(words) - 1 else word
            on_token(token)
            accumulated.append(token)
            time.sleep(self.WORD_DELAY)
        return "".join(accumulated)


# ---------------------------------------------------------------------------
# OpenAI-compatible (SSE streaming)
# ---------------------------------------------------------------------------
class OpenAICompatibleProvider(AIProvider):

    def send(self, messages, model, api_key, base_url, temperature, timeout) -> str:
        if not api_key:
            raise ProviderError("No API key configured for the OpenAI-compatible provider.")

        url = base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {"model": model, "messages": messages, "temperature": temperature}
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        except requests.RequestException as e:
            raise ProviderError(f"Network error: {e}") from e

        if resp.status_code != 200:
            raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise ProviderError(f"Unexpected response shape: {data}") from e

    def stream_send(self, messages, model, api_key, base_url, temperature, timeout,
                    on_token, cancel_flag) -> str:
        if not api_key:
            raise ProviderError("No API key configured.")

        url = base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        accumulated = []
        try:
            with requests.post(url, headers=headers, json=body,
                               timeout=timeout, stream=True) as resp:
                if resp.status_code != 200:
                    raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                for line in resp.iter_lines():
                    if cancel_flag and cancel_flag.is_set():
                        break
                    if not line:
                        continue
                    decoded = line.decode("utf-8")
                    if decoded.startswith("data: "):
                        decoded = decoded[6:]
                    if decoded == "[DONE]":
                        break
                    try:
                        chunk = json.loads(decoded)
                        delta = chunk["choices"][0]["delta"].get("content", "")
                        if delta:
                            on_token(delta)
                            accumulated.append(delta)
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
        except requests.RequestException as e:
            raise ProviderError(f"Network error: {e}") from e

        return "".join(accumulated)


# ---------------------------------------------------------------------------
# Ollama (NDJSON streaming — local GPU)
# ---------------------------------------------------------------------------
class OllamaProvider(AIProvider):

    def send(self, messages, model, api_key, base_url, temperature, timeout) -> str:
        # Use the chat endpoint which accepts messages array
        url = base_url.rstrip("/") + "/api/chat"
        body = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        try:
            resp = requests.post(url, json=body, timeout=timeout)
        except requests.RequestException as e:
            raise ProviderError(f"Network error contacting Ollama: {e}") from e

        if resp.status_code != 200:
            raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        try:
            return data["message"]["content"]
        except (KeyError, IndexError) as e:
            raise ProviderError(f"Unexpected response shape: {data}") from e

    def stream_send(self, messages, model, api_key, base_url, temperature, timeout,
                    on_token, cancel_flag) -> str:
        url = base_url.rstrip("/") + "/api/chat"
        body = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature},
        }
        accumulated = []
        try:
            with requests.post(url, json=body, timeout=timeout, stream=True) as resp:
                if resp.status_code != 200:
                    raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                for line in resp.iter_lines():
                    if cancel_flag and cancel_flag.is_set():
                        break
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line.decode("utf-8"))
                        token = chunk.get("message", {}).get("content", "")
                        if token:
                            on_token(token)
                            accumulated.append(token)
                        if chunk.get("done", False):
                            break
                    except (json.JSONDecodeError, KeyError):
                        continue
        except requests.RequestException as e:
            raise ProviderError(f"Network error: {e}") from e

        return "".join(accumulated)


def get_provider(name: str) -> AIProvider:
    return {
        "mock": MockProvider,
        "openai": OpenAICompatibleProvider,
        "ollama": OllamaProvider,
    }.get(name, MockProvider)()

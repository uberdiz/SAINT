"""
modules/ai/providers.py

Abstract AI provider interface. Extended with Ollama model discovery,
connection testing, and robust error handling.
"""

import json
import time
import threading
from typing import Callable, List, Dict, Optional, Any

import requests


class ProviderError(Exception):
    pass


class ModelNotFoundError(ProviderError):
    pass


class ConnectionError(ProviderError):
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

    def test_connection(self, base_url: str, timeout: float = 5.0) -> Dict[str, Any]:
        """Test connection to the provider. Override in subclasses."""
        raise NotImplementedError

    def list_models(self, base_url: str, timeout: float = 10.0) -> List[Dict[str, Any]]:
        """List available models. Override in subclasses."""
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

    WORD_DELAY = 0.04

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

    def test_connection(self, base_url: str, timeout: float = 5.0) -> Dict[str, Any]:
        return {"connected": True, "provider": "mock", "latency_ms": 0}

    def list_models(self, base_url: str, timeout: float = 10.0, force_refresh: bool = False) -> List[Dict[str, Any]]:
        return [
            {"name": "mock-model", "size": 0, "modified_at": "", "digest": ""}
        ]


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

    def test_connection(self, base_url: str, timeout: float = 5.0) -> Dict[str, Any]:
        return {"connected": True, "provider": "mock", "latency_ms": 0}

    def list_models(self, base_url: str, timeout: float = 10.0) -> List[Dict[str, Any]]:
        try:
            url = base_url.rstrip("/") + "/models"
            headers = {"Authorization": "Bearer test"}
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("data", [])
            return []
        except requests.RequestException:
            return []


# ---------------------------------------------------------------------------
# Ollama (NDJSON streaming — local GPU)
# ---------------------------------------------------------------------------
class OllamaProvider(AIProvider):

    _models_cache: List[Dict[str, Any]] = []
    _cache_time: float = 0
    _cache_lock = threading.Lock()
    _CACHE_TTL = 30.0  # seconds

    def _request(self, method: str, endpoint: str, base_url: str, timeout: float, **kwargs) -> requests.Response:
        base_url = base_url.replace("localhost", "127.0.0.1")
        url = base_url.rstrip("/") + endpoint
        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
            return resp
        except requests.RequestException as e:
            raise ConnectionError(f"Network error contacting Ollama: {e}") from e

    def send(self, messages, model, api_key, base_url, temperature, timeout) -> str:
        resp = self._request("POST", "/api/chat", base_url, timeout,
                            json={"model": model, "messages": messages, "stream": False, "keep_alive": -1, "options": {"temperature": temperature, "num_gpu": -1}})
        if resp.status_code != 200:
            if resp.status_code == 404:
                raise ModelNotFoundError(f"Model '{model}' not found. Run 'ollama pull {model}' first.")
            raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()["message"]["content"]
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise ProviderError(f"Unexpected response shape: {resp.text[:300]}") from e

    def stream_send(self, messages, model, api_key, base_url, temperature, timeout,
                    on_token, cancel_flag) -> str:
        resp = self._request("POST", "/api/chat", base_url, timeout,
                            json={"model": model, "messages": messages, "stream": True, "keep_alive": -1, "options": {"temperature": temperature, "num_gpu": -1}},
                            stream=True)
        if resp.status_code != 200:
            if resp.status_code == 404:
                raise ModelNotFoundError(f"Model '{model}' not found. Run 'ollama pull {model}' first.")
            raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        accumulated = []
        try:
            with resp:
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

    def test_connection(self, base_url: str, timeout: float = 5.0) -> Dict[str, Any]:
        """Test Ollama connection and return version info."""
        try:
            resp = self._request("GET", "/api/version", base_url, timeout)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "connected": True,
                    "provider": "ollama",
                    "version": data.get("version", "unknown"),
                }
            return {"connected": False, "error": f"HTTP {resp.status_code}"}
        except ConnectionError as e:
            return {"connected": False, "error": str(e)}
        except requests.RequestException as e:
            return {"connected": False, "error": str(e)}

    def list_models(self, base_url: str, timeout: float = 10.0, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """List available Ollama models with caching."""
        now = time.time()
        with self._cache_lock:
            if not force_refresh and self._models_cache and (now - self._cache_time) < self._CACHE_TTL:
                return self._models_cache
            
            try:
                resp = self._request("GET", "/api/tags", base_url, timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    models = data.get("models", [])
                    self._models_cache = models
                    self._cache_time = now
                    return models
                return []
            except requests.RequestException:
                return []

    def pull_model(self, base_url: str, model: str, timeout: float = 300.0) -> Dict[str, Any]:
        """Pull a model from Ollama registry."""
        try:
            resp = self._request("POST", "/api/pull", base_url, timeout,
                                json={"name": model, "stream": False})
            if resp.status_code == 200:
                return {"success": True, "status": resp.json().get("status", "done")}
            return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
        except requests.RequestException as e:
            return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Provider Factory
# ---------------------------------------------------------------------------
_PROVIDER_CACHE: Dict[str, AIProvider] = {}
_PROVIDER_CACHE_LOCK = threading.Lock()


def get_provider(name: str) -> AIProvider:
    """Get provider instance with singleton caching."""
    with _PROVIDER_CACHE_LOCK:
        if name not in _PROVIDER_CACHE:
            _PROVIDER_CACHE[name] = {
                "mock": MockProvider,
                "openai": OpenAICompatibleProvider,
                "ollama": OllamaProvider,
            }.get(name, MockProvider)()
        return _PROVIDER_CACHE[name]


def clear_provider_cache():
    """Clear the provider cache (useful for testing)."""
    with _PROVIDER_CACHE_LOCK:
        _PROVIDER_CACHE.clear()
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
from typing import Callable, Optional, Dict, List, Any

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
        self._active_turn_id: int = -1
        self._active_stream_id: str = ""
        self._stream_id_lock = threading.Lock()
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
        import uuid
        request_id = uuid.uuid4().hex[:12]

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

        event_bus.emit_event(EventType.AI_REQUEST, {
            "request_id": request_id,
            "user_text": prompt,
            "prompt_length": len(prompt),
            "model": model,
            "provider": provider_name,
        })
        start = time.perf_counter()
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
            event_bus.emit_event(EventType.AI_ERROR, {"error": str(e), "request_id": request_id})
            raise
        except Exception as e:
            event_bus.emit_event(EventType.AI_ERROR, {"error": f"Unexpected error: {e}", "request_id": request_id})
            raise

        elapsed = time.perf_counter() - start
        self._context.add_assistant_turn(response_text)
        event_bus.emit_event(
            EventType.AI_RESPONSE,
            {"elapsed_seconds": round(elapsed, 3), "response_length": len(response_text), "request_id": request_id},
        )
        event_bus.emit_event(EventType.LATENCY_MODEL, {"ms": round(elapsed * 1000, 1), "request_id": request_id})
        return response_text

    def _try_integration_command(self, prompt: str):
        """Execute deterministic commands for enabled integrations.

        This is intentionally used for action-oriented commands where the
        LLM should not be allowed to hallucinate that an action happened.
        Spotify is currently the first integration with direct routing.
        Returns a user-facing response string, or None when the prompt is
        not a supported integration command.
        """
        import re
        from core.module_manager import module_manager
        from modules.automation.tools import get_tool_registry

        text = prompt.strip()
        lower = text.lower()
        if "spotify" not in lower and not re.match(
            r"^(play|pause|resume|skip|next|previous|volume|what(?:'s| is) playing)\b",
            lower,
        ):
            return None

        spotify = module_manager.get("spotify")
        if not spotify or not spotify.enabled:
            return "Spotify is not enabled. Enable the Spotify integration in Settings → Integrations."

        if not spotify.is_connected():
            return "Spotify is enabled, but your Spotify account is not connected."

        registry = get_tool_registry()

        def run(name, **kwargs):
            result = registry.execute(name, **kwargs)
            if not result.success:
                return None, result.error or f"{name} failed."
            return result.result, None

        try:
            # Playback controls
            if re.search(r"\b(pause|stop)\b.*\bspotify\b|\bspotify\b.*\b(pause|stop)\b", lower):
                _, err = run("spotify.pause")
                return "Paused Spotify." if not err else f"I couldn't pause Spotify: {err}"

            if re.search(r"\b(resume|continue)\b.*\bspotify\b|\bspotify\b.*\b(resume|continue)\b", lower):
                _, err = run("spotify.play")
                return "Resumed Spotify." if not err else f"I couldn't resume Spotify: {err}"

            if re.search(r"\b(next|skip)\b", lower) and "spotify" in lower:
                _, err = run("spotify.next")
                return "Skipped to the next track." if not err else f"I couldn't skip the track: {err}"

            if re.search(r"\b(previous|back)\b", lower) and "spotify" in lower:
                _, err = run("spotify.previous")
                return "Went back to the previous track." if not err else f"I couldn't go back: {err}"

            vol = re.search(r"\bvolume\s+(?:to\s+)?(\d{1,3})\s*(?:%|percent)?", lower)
            if vol and "spotify" in lower:
                percent = max(0, min(100, int(vol.group(1))))
                _, err = run("spotify.volume", percent=percent)
                return f"Set Spotify volume to {percent}%." if not err else f"I couldn't change the volume: {err}"

            if re.search(r"(what(?:'s| is) playing|current(?:ly)? playing|current track)", lower):
                data, err = run("spotify.current")
                if err:
                    return f"I couldn't check Spotify playback: {err}"
                item = (data or {}).get("item") if isinstance(data, dict) else None
                if not item:
                    return "Spotify isn't currently playing anything."
                artists = ", ".join(a.get("name", "") for a in item.get("artists", []))
                return f"Spotify is playing {item.get('name', 'an unknown track')} by {artists}."

            # Playlist commands: use the user's actual playlists instead of
            # pretending that a playlist was played.
            if re.search(r"\bplaylist\b", lower) and re.search(r"\b(play|open|start)\b", lower):
                data, err = run("spotify.playlists")
                if err:
                    return f"I couldn't read your Spotify playlists: {err}"
                items = (data or {}).get("items", []) if isinstance(data, dict) else []
                if not items:
                    return "I couldn't find any Spotify playlists on your account."

                target = lower
                target = re.sub(r"\b(play|open|start)\b", "", target)
                target = target.replace("spotify", "").replace("playlist", "")
                target = re.sub(r"\b(my|the|on|please)\b", " ", target)
                target = re.sub(r"\s+", " ", target).strip()

                chosen = None
                if target:
                    exact = [p for p in items if p.get("name", "").lower() == target]
                    partial = [p for p in items if target in p.get("name", "").lower()]
                    chosen = (exact or partial or [None])[0]
                if chosen is None and "top" in target:
                    chosen = next((p for p in items if "top" in p.get("name", "").lower()), None)
                if chosen is None and not target:
                    chosen = items[0]
                if chosen is None:
                    names = ", ".join(p.get("name", "Unnamed") for p in items[:5])
                    return f"I couldn't find that playlist. Your playlists include: {names}."

                _, err = run("spotify.play", uri=chosen.get("uri"))
                return f"Playing your playlist {chosen.get('name', 'playlist')}." if not err else f"I found {chosen.get('name', 'that playlist')}, but couldn't start it: {err}"

            # Generic track/artist play request.
            if re.match(r"^(please\s+)?play\b", lower):
                query = re.sub(r"^(please\s+)?play\s+", "", text, flags=re.I)
                query = re.sub(r"\bon\s+spotify\s*$", "", query, flags=re.I).strip()
                if query and query.lower() not in {"spotify", "my spotify"}:
                    data, err = run("spotify.search", query=query, types="track")
                    if err:
                        return f"I couldn't search Spotify: {err}"
                    tracks = ((data or {}).get("tracks") or {}).get("items", [])
                    if not tracks:
                        return f"I couldn't find {query} on Spotify."
                    track = tracks[0]
                    _, err = run("spotify.play", uri=track.get("uri"))
                    artists = ", ".join(a.get("name", "") for a in track.get("artists", []))
                    return f"Playing {track.get('name', query)} by {artists}." if not err else f"I found the track, but couldn't start it: {err}"

        except Exception as exc:
            return f"I couldn't complete that Spotify action: {exc}"

        return None

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
        turn_id: int = 0,
        request_id: str = "",
    ) -> None:
        """
        Stream response tokens via on_token(). Meant to be called from a
        worker thread. When done, calls on_done(full_text).

        is_interruption=True means the user cut off SAINT mid-sentence;
        handle_interruption() is used instead of add_user_turn().
        turn_id is used to filter stale tokens from cancelled generations.
        """
        self._cancel_flag.clear()
        self._turn_id = turn_id
        self._active_turn_id = turn_id

        import uuid
        if not request_id:
            request_id = uuid.uuid4().hex[:12]
        stream_id = f"stream_{turn_id}_{uuid.uuid4().hex[:8]}"
        with self._stream_id_lock:
            self._active_stream_id = stream_id

        provider_name = config.get("ai.provider", "mock")
        model = config.get("ai.model", "llama3")
        api_key = config.get("ai.api_key", "")
        base_url = config.get("ai.base_url", "http://localhost:11434")
        temperature = config.get("ai.temperature", 0.7)
        timeout = config.get("ai.timeout_seconds", 60)

        provider = get_provider(provider_name)

        # Test provider connection on first use
        if not hasattr(self, '_provider_tested') or not self._provider_tested:
            self._provider_tested = True
            try:
                test_result = provider.test_connection(base_url)
                event_bus.emit_event(EventType.AI_PROVIDER_TEST, {
                    "provider": provider_name,
                    "model": model,
                    "connected": test_result.get("connected", False),
                    "details": test_result,
                })
                if not test_result.get("connected", False):
                    event_bus.emit_event(EventType.WARNING, {
                        "message": f"AI provider '{provider_name}' not connected: {test_result.get('error', 'unknown')}. Falling back to mock may occur."
                    })
            except Exception as e:
                event_bus.emit_event(EventType.WARNING, {
                    "message": f"AI provider test failed: {e}"
                })

        if is_interruption:
            self._context.handle_interruption(prompt)
        else:
            self._context.add_user_turn(prompt)

        messages = self._context.format_messages()

        # Detailed request logging for debugging
        event_bus.emit_event(EventType.AI_REQUEST, {
            "turn_id": turn_id,
            "request_id": request_id,
            "stream_id": stream_id,
            "user_text": prompt,
            "prompt_length": len(prompt),
            "model": model,
            "provider": provider_name,
            "conversation_history_length": len(messages),
            "system_prompt": config.get("voice.system_prompt", "")[:100],
        })
        start = time.perf_counter()

        # Execute real integration actions before asking the LLM to answer.
        # This prevents hallucinated actions such as "*Spotify plays*" when
        # the connected service was never actually called.
        integration_response = self._try_integration_command(prompt)
        if integration_response is not None:
            if self._cancel_flag.is_set():
                if on_done:
                    on_done("")
                return

            on_token(integration_response)
            elapsed = time.perf_counter() - start
            self._context.add_assistant_turn(integration_response)
            event_bus.emit_event(EventType.AI_STREAM_DONE, {
                "turn_id": turn_id,
                "request_id": request_id,
                "stream_id": stream_id,
                "elapsed_seconds": round(elapsed, 3),
                "response_length": len(integration_response),
                "tool_routed": True,
            })
            event_bus.emit_event(EventType.LATENCY_MODEL, {
                "ms": round(elapsed * 1000, 1),
                "turn_id": turn_id,
                "request_id": request_id,
                "tool_routed": True,
            })
            if on_done:
                on_done(integration_response)
            return

        _current_turn_id = turn_id
        _current_stream_id = stream_id
        _current_request_id = request_id
        first_token_emitted = [False]

        def _safe_on_token(tok: str):
            if _current_turn_id != self._turn_id:
                return
            if not first_token_emitted[0]:
                first_token_emitted[0] = True
            on_token(tok)
            event_bus.emit_event(EventType.AI_STREAM_TOKEN, {
                "token": tok,
                "turn_id": _current_turn_id,
                "stream_id": _current_stream_id,
                "request_id": _current_request_id,
            })

        try:
            full_text = provider.stream_send(
                messages=messages,
                model=model,
                api_key=api_key,
                base_url=base_url,
                temperature=temperature,
                timeout=timeout,
                on_token=_safe_on_token,
                cancel_flag=self._cancel_flag,
            )
        except ProviderError as e:
            event_bus.emit_event(EventType.AI_ERROR, {"error": str(e), "turn_id": turn_id, "request_id": request_id})
            # Fallback to mock if configured provider fails
            if provider_name != "mock":
                event_bus.emit_event(EventType.WARNING, {
                    "message": f"Provider '{provider_name}' failed, falling back to mock: {e}"
                })
                try:
                    mock_provider = get_provider("mock")
                    full_text = mock_provider.stream_send(
                        messages=messages,
                        model="mock",
                        api_key="",
                        base_url="",
                        temperature=temperature,
                        timeout=timeout,
                        on_token=_safe_on_token,
                        cancel_flag=self._cancel_flag,
                    )
                except Exception as e2:
                    event_bus.emit_event(EventType.AI_ERROR, {"error": f"Mock fallback also failed: {e2}", "turn_id": turn_id, "request_id": request_id})
                    raise
            else:
                raise
        except Exception as e:
            event_bus.emit_event(EventType.AI_ERROR, {"error": f"Unexpected error: {e}", "turn_id": turn_id, "request_id": request_id})
            raise

        elapsed = time.perf_counter() - start

        if self._cancel_flag.is_set():
            event_bus.emit_event(EventType.AI_CANCELLED, {"reason": "interrupted", "turn_id": turn_id, "request_id": request_id})
        else:
            self._context.add_assistant_turn(full_text)
            event_bus.emit_event(EventType.AI_STREAM_DONE, {
                "turn_id": turn_id,
                "request_id": request_id,
                "stream_id": stream_id,
                "elapsed_seconds": round(elapsed, 3),
                "response_length": len(full_text),
            })
            event_bus.emit_event(EventType.LATENCY_MODEL, {"ms": round(elapsed * 1000, 1), "turn_id": turn_id, "request_id": request_id})

        if on_done:
            on_done(full_text)

    def cancel(self):
        """Signal the in-flight stream to stop at the next token boundary."""
        self._cancel_flag.set()
        event_bus.emit_event(EventType.AI_CANCELLED, {"reason": "user_cancel"})

    def reset_context(self):
        """Clear conversation history."""
        self._rebuild_context()

    # ------------------------------------------------------------------ #
    # Ollama/Provider Management
    # ------------------------------------------------------------------ #
    def test_connection(self) -> Dict[str, Any]:
        """Test connection to the configured AI provider."""
        provider_name = config.get("ai.provider", "mock")
        base_url = config.get("ai.base_url", "http://localhost:11434")
        provider = get_provider(provider_name)
        
        if hasattr(provider, 'test_connection'):
            return provider.test_connection(base_url)
        return {"connected": False, "error": "Provider does not support connection testing"}

    def list_models(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """List available models from the configured provider (primarily for Ollama)."""
        provider_name = config.get("ai.provider", "mock")
        base_url = config.get("ai.base_url", "http://localhost:11434")
        provider = get_provider(provider_name)
        
        if hasattr(provider, 'list_models'):
            return provider.list_models(base_url, force_refresh=force_refresh)
        return []

    def pull_model(self, model_name: str) -> Dict[str, Any]:
        """Pull a model (Ollama only)."""
        provider_name = config.get("ai.provider", "mock")
        base_url = config.get("ai.base_url", "http://localhost:11434")
        provider = get_provider(provider_name)
        
        if hasattr(provider, 'pull_model'):
            return provider.pull_model(base_url, model_name)
        return {"success": False, "error": "Provider does not support model pulling"}

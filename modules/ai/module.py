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
    # Model resolution
    # ------------------------------------------------------------------ #
    def _resolve_model(self, provider_name: str, base_url: str, configured: str):
        """Resolve the model to actually use.

        The user's configured model is authoritative (Settings). But if it is
        an Ollama model that isn't installed (e.g. the old default 'llama3'
        when only 'llama3.1:latest' is present), we transparently select the
        closest *available* model and log a clear warning — instead of letting
        the request 404 and silently fall back to a mock responder.

        Returns (model_to_use, info_or_None). info is a dict describing the
        substitution when one happened.
        """
        if provider_name != "ollama":
            return configured, None
        try:
            provider = get_provider(provider_name)
            models = provider.list_models(base_url)
            names = [m.get("name") for m in models if m.get("name")]
        except Exception:
            names = []
        if not names:
            # Can't verify (Ollama unreachable). Keep configured so the request
            # surfaces a clear connection/model error rather than guessing.
            return configured, None
        if configured in names:
            return configured, None
        # Configured model isn't installed — pick the closest available.
        base = (configured or "").split(":")[0].lower()

        def score(name: str) -> int:
            nl = name.lower()
            s = 0
            if base and nl.split(":")[0] == base:
                s += 100
            if base and base in nl:
                s += 50
            return s

        best = max(names, key=score)
        return best, {"configured": configured, "resolved": best, "available": names}

# ------------------------------------------------------------------ #
    # Legacy synchronous API (kept for dashboard quick-test panel)
    # ------------------------------------------------------------------ #
    def send_prompt(self, prompt: str) -> str:
        """Synchronous call — intended to be run from a worker thread."""
        import uuid
        request_id = uuid.uuid4().hex[:12]

        provider_name = config.get("ai.provider", "mock")
        api_key = config.get("ai.api_key", "")
        base_url = config.get("ai.base_url", "http://localhost:11434")
        temperature = config.get("ai.temperature", 0.7)
        timeout = config.get("ai.timeout_seconds", 30)

        provider = get_provider(provider_name)
        model, _ = self._resolve_model(provider_name, base_url, config.get("ai.model", ""))

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

    def _spotify_intent(self, text: str):
        """Deterministically map a music-control utterance to a Spotify tool.

        Returns (intent_kind, tool_name, kwargs) or None. Common commands
        (skip, next, previous, pause, resume, play, what's playing, volume,
        shuffle, repeat) are recognised WITHOUT requiring the literal word
        "spotify", so "skip the song" reliably routes to spotify.next instead
        of being handed to the LLM (which could hallucinate or, on failure,
        fall through to a mock response).
        """
        import re
        lower = text.lower().strip()
        wc = len(lower.split())

        def has(pattern):
            return re.search(pattern, lower) is not None

        # --- what's playing / current track (check before "play") ---------
        if has(r"what(?:'?s| is| am i)\b.*\b(playing|listening|song|track)") \
                or has(r"what song is (this|playing|that)") \
                or has(r"what(?:'?s| is) this (song|track)") \
                or has(r"\b(current|now playing|currently playing)\b.*\b(song|track)?") \
                or has(r"who(?:'?s| is) (this|singing|the artist)"):
            return ("current", "spotify.current", {})

        # --- previous / back (before generic play) ------------------------
        if has(r"\b(previous|last)\s+(song|track|one)\b") or has(r"\bgo back\b") \
                or has(r"\bplay (the )?(previous|last)\b") \
                or (has(r"\bprevious\b") and wc <= 5) or has(r"\bback a (song|track)\b"):
            return ("previous", "spotify.previous", {})

        # --- next / skip ---------------------------------------------------
        if has(r"\b(skip|next)\b") and (
            has(r"\b(song|track|this|it|ahead|one)\b") or has(r"\bspotify\b") or wc <= 5
        ):
            return ("next", "spotify.next", {})

        # --- pause / stop music -------------------------------------------
        if has(r"\bpause\b") or has(r"\bstop\b.*\b(music|song|spotify|playback|playing)\b") \
                or has(r"\b(music|song|spotify|playback)\b.*\bstop\b"):
            return ("pause", "spotify.pause", {})

        # --- volume set / up / down ---------------------------------------
        vol = re.search(r"\bvolume\b.*?(\d{1,3})", lower) or re.search(r"\bset (?:the )?volume (?:to )?(\d{1,3})", lower)
        if vol:
            pct = max(0, min(100, int(vol.group(1))))
            return ("volume_set", "spotify.volume", {"percent": pct})
        if has(r"\b(volume up|louder|turn (it )?up)\b"):
            return ("volume_up", "spotify.volume", {"_relative": +15})
        if has(r"\b(volume down|quieter|softer|turn (it )?down)\b"):
            return ("volume_down", "spotify.volume", {"_relative": -15})

        # --- shuffle -------------------------------------------------------
        if has(r"\bshuffle\b"):
            state = not has(r"\b(off|stop|disable|no)\b")
            return ("shuffle", "spotify.shuffle", {"state": state})

        # --- repeat --------------------------------------------------------
        if has(r"\brepeat\b"):
            if has(r"\b(off|stop|disable|no)\b"):
                mode = "off"
            elif has(r"\b(one|this|track|single)\b"):
                mode = "track"
            else:
                mode = "context"
            return ("repeat", "spotify.repeat", {"state": mode})

        # --- resume --------------------------------------------------------
        if has(r"\b(resume|unpause|continue)\b"):
            return ("resume", "spotify.play", {})

        # --- play (query or resume) ---------------------------------------
        m = re.match(r"^(?:please\s+)?play\b(.*)$", lower)
        if m:
            return ("play", "__play__", {"raw": text})

        return None

    def _try_integration_command(self, prompt: str):
        """Execute deterministic commands for enabled integrations.

        This is intentionally used for action-oriented commands where the
        LLM should not be allowed to hallucinate that an action happened.
        Returns a user-facing response string, or None when the prompt is
        not a supported integration command.
        """
        import re
        from core.module_manager import module_manager
        from modules.automation.tools import get_tool_registry

        text = prompt.strip()
        lower = text.lower()

        intent = self._spotify_intent(text)
        # If it's clearly not a music command, let the LLM handle it.
        if intent is None and "spotify" not in lower:
            return None

        spotify = module_manager.get("spotify")
        if not spotify or not spotify.enabled:
            if intent is None:
                return None
            return "Spotify isn't enabled. You can enable it in Settings, under Integrations."

        if not spotify.is_connected():
            if intent is None:
                return None
            return "Spotify is enabled, but your account isn't connected yet."

        registry = get_tool_registry()

        def run(name, **kwargs):
            result = registry.execute(name, **kwargs)
            if not result.success:
                return None, (result.error or f"{name} failed.")
            return result.result, None

        try:
            kind = intent[0] if intent else None

            if kind == "pause":
                _, err = run("spotify.pause")
                return "Okay, paused." if not err else err

            if kind == "resume":
                _, err = run("spotify.play")
                return "Okay, playing." if not err else err

            if kind == "next":
                _, err = run("spotify.next")
                return "Skipped to the next track." if not err else err

            if kind == "previous":
                _, err = run("spotify.previous")
                return "Going back a track." if not err else err

            if kind == "shuffle":
                _, err = run("spotify.shuffle", state=intent[2]["state"])
                if err:
                    return err
                return "Shuffle on." if intent[2]["state"] else "Shuffle off."

            if kind == "repeat":
                _, err = run("spotify.repeat", state=intent[2]["state"])
                if err:
                    return err
                labels = {"off": "Repeat off.", "track": "Repeating this track.", "context": "Repeat on."}
                return labels.get(intent[2]["state"], "Repeat updated.")

            if kind == "volume_set":
                _, err = run("spotify.volume", percent=intent[2]["percent"])
                return f"Volume set to {intent[2]['percent']}%." if not err else err

            if kind in ("volume_up", "volume_down"):
                data, err = run("spotify.current")
                if err:
                    return err
                cur = 50
                if isinstance(data, dict):
                    cur = (data.get("device") or {}).get("volume_percent", 50)
                new = max(0, min(100, int(cur) + intent[2]["_relative"]))
                _, err = run("spotify.volume", percent=new)
                return f"Volume {'up' if intent[2]['_relative'] > 0 else 'down'} to {new}%." if not err else err

            if kind == "current":
                data, err = run("spotify.current")
                if err:
                    return err
                item = (data or {}).get("item") if isinstance(data, dict) else None
                if not item:
                    return "Spotify isn't playing anything right now."
                artists = ", ".join(a.get("name", "") for a in item.get("artists", []))
                return f"This is {item.get('name', 'an unknown track')}" + (f" by {artists}." if artists else ".")

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
                if chosen is None and target in {"top", "top playlist", "favorite", "favourite"}:
                    # "my top playlist" means the first playlist in the user's
                    # Spotify playlist collection unless a playlist actually
                    # named "top" was matched above.
                    chosen = next(
                        (p for p in items if "top" in p.get("name", "").lower()),
                        None,
                    ) or items[0]
                if chosen is None and not target:
                    chosen = items[0]
                if chosen is None:
                    names = ", ".join(p.get("name", "Unnamed") for p in items[:5])
                    return f"I couldn't find that playlist. Your playlists include: {names}."

                _, err = run("spotify.play", uri=chosen.get("uri"))
                return f"Playing your playlist {chosen.get('name', 'playlist')}." if not err else f"I found {chosen.get('name', 'that playlist')}, but couldn't start it: {err}"

            # Generic track/artist play request (or a vague "play something").
            if re.match(r"^(please\s+)?play\b", lower):
                query = re.sub(r"^(please\s+)?play\s+", "", text, flags=re.I)
                query = re.sub(r"\bon\s+spotify\s*$", "", query, flags=re.I).strip()
                vague = {"", "spotify", "my spotify", "music", "some music",
                         "something", "a song", "the music", "some songs"}
                if query.lower() in vague:
                    # No specific track — just resume/continue playback.
                    _, err = run("spotify.play")
                    return "Okay, playing." if not err else err
                data, err = run("spotify.search", query=query, types="track")
                if err:
                    return err
                tracks = ((data or {}).get("tracks") or {}).get("items", [])
                if not tracks:
                    return f"I couldn't find {query} on Spotify."
                track = tracks[0]
                _, err = run("spotify.play", uri=track.get("uri"))
                artists = ", ".join(a.get("name", "") for a in track.get("artists", []))
                return f"Playing {track.get('name', query)} by {artists}." if not err else err

        except Exception as exc:
            # Never speak a raw exception; log the detail and say something clean.
            import logging
            logging.getLogger("saint.ai").exception("spotify.route.error")
            return "Sorry, I couldn't complete that Spotify action."

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
        configured_model = config.get("ai.model", "")
        api_key = config.get("ai.api_key", "")
        base_url = config.get("ai.base_url", "http://localhost:11434")
        temperature = config.get("ai.temperature", 0.7)
        timeout = config.get("ai.timeout_seconds", 60)

        provider = get_provider(provider_name)

        # Resolve the model against what's actually available (Ollama).
        model, model_info = self._resolve_model(provider_name, base_url, configured_model)
        import logging
        _ai_log = logging.getLogger("saint.ai")
        if model_info:
            _ai_log.warning(
                "Configured model '%s' is not installed; using available model "
                "'%s'. Installed: %s. Set your model in Settings.",
                model_info["configured"], model_info["resolved"], model_info["available"],
            )
            event_bus.emit_event(EventType.WARNING, {
                "message": (
                    f"Configured AI model '{model_info['configured']}' not found. "
                    f"Using '{model_info['resolved']}'. Pull the model or change it "
                    f"in Settings."
                ),
            })
        _ai_log.info("AI request: provider=%s model=%s (configured=%s)",
                     provider_name, model, configured_model)

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
            # A real provider failure must NEVER silently become a fabricated
            # mock response (that produced "I understand." for unrelated input).
            # Report a clear, honest error. The mock provider is only used when
            # explicitly configured (provider == "mock") for dev/testing.
            _ai_log.error("AI provider '%s' (model=%s) failed: %s", provider_name, model, e)
            event_bus.emit_event(EventType.AI_ERROR, {
                "error": str(e), "turn_id": turn_id, "request_id": request_id,
                "provider": provider_name, "model": model,
            })

            from modules.ai.providers import ModelNotFoundError, ConnectionError as _ConnErr
            if isinstance(e, ModelNotFoundError):
                spoken = (f"I can't answer right now — the language model "
                          f"\"{model}\" isn't installed. Please pull it or pick "
                          f"another model in settings.")
            elif isinstance(e, _ConnErr):
                spoken = ("I can't reach my language model right now. Please "
                          "make sure Ollama is running.")
            else:
                spoken = ("I ran into a problem reaching my language model, so "
                          "I couldn't answer that.")

            # Speak the truthful error, but do NOT add it to conversation
            # history (avoid polluting future context with error text).
            if not self._cancel_flag.is_set():
                on_token(spoken)
            event_bus.emit_event(EventType.AI_STREAM_DONE, {
                "turn_id": turn_id, "request_id": request_id, "stream_id": stream_id,
                "response_length": len(spoken), "error": True,
            })
            if on_done:
                on_done(spoken)
            return
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

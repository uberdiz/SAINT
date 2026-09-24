"""
modules/ai/module.py

AI module: every user turn goes through here.

  - stream_prompt(): agent first (deterministic intents -> real tools), then the
    LLM with relevant memories and optional tool calling; streams tokens
  - cancel(): sets the cancel flag so an in-flight stream exits cleanly
  - Short-term conversation context tracked via ConversationContext
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
        self.expects_reply = False   # last answer asked the user a question
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
        """Compatibility shim: (kind, tool, kwargs) for a music utterance, or None.

        The real parser lives in modules/agent/router.py (spotify_intent)."""
        from modules.agent.router import spotify_intent
        si = spotify_intent(text)
        return (si.kind, si.tool, si.kwargs) if si else None

    # ------------------------------------------------------------------ #
    # Memory context
    # ------------------------------------------------------------------ #
    def _memory_messages(self, prompt: str) -> List[Dict[str, str]]:
        if not (config.get("memory.enabled", True) and config.get("memory.inject_context", True)
                and config.get("modules.memory", True)):
            return []
        try:
            from modules.memory.service import memory_service
            items = memory_service.context_for(prompt, limit=int(config.get("memory.max_context_items", 6)))
        except Exception:
            return []
        if not items:
            return []
        lines = "\n".join(f"- {i}" for i in items)
        return [{"role": "system", "content": (
            "Facts the user previously asked you to remember (use them only if relevant; do not "
            "invent other personal facts):\n" + lines)}]

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
        """Handle one user turn. Called from a worker thread.

        1. The agent (deterministic intents + real tools) answers if it can.
        2. Otherwise the configured LLM answers, with relevant memories and —
           for action-like requests on a tool-capable model — function calling.
        Tokens stream through on_token(); on_done(full_text) fires at the end.
        """
        import logging
        import uuid
        _ai_log = logging.getLogger("saint.ai")

        self._cancel_flag.clear()
        self._turn_id = turn_id
        self._active_turn_id = turn_id
        self.expects_reply = False
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

        if is_interruption:
            self._context.handle_interruption(prompt)
        else:
            self._context.add_user_turn(prompt)

        event_bus.emit_event(EventType.AI_REQUEST, {
            "turn_id": turn_id, "request_id": request_id, "stream_id": stream_id,
            "user_text": prompt, "prompt_length": len(prompt), "provider": provider_name,
        })
        start = time.perf_counter()

        # ---- 1. Agent: deterministic intents executed through real tools ----
        from modules.agent.agent import agent
        try:
            result = agent.handle(prompt)
        except Exception:
            _ai_log.exception("agent.handle_failed")
            result = None
        if result is not None:
            if self._cancel_flag.is_set():
                if on_done:
                    on_done("")
                return
            self.expects_reply = result.expects_reply
            from modules.agent.output import clean_reply
            result.text = clean_reply(result.text, prompt) or result.text
            on_token(result.text)
            event_bus.emit_event(EventType.AI_STREAM_TOKEN, {
                "token": result.text, "turn_id": turn_id, "stream_id": stream_id, "request_id": request_id})
            elapsed = time.perf_counter() - start
            self._context.add_assistant_turn(result.text)
            event_bus.emit_event(EventType.AI_STREAM_DONE, {
                "turn_id": turn_id, "request_id": request_id, "stream_id": stream_id,
                "elapsed_seconds": round(elapsed, 3), "response_length": len(result.text),
                "tool_routed": True, "intent": result.intent, "ok": result.ok,
            })
            event_bus.emit_event(EventType.LATENCY_MODEL, {
                "ms": round(elapsed * 1000, 1), "turn_id": turn_id, "request_id": request_id,
                "tool_routed": True})
            if on_done:
                on_done(result.text)
            return

        # ---- 2. LLM ------------------------------------------------------------
        provider = get_provider(provider_name)
        model, model_info = self._resolve_model(provider_name, base_url, configured_model)
        if model_info:
            _ai_log.warning("Configured model '%s' is not installed; using '%s'. Installed: %s.",
                            model_info["configured"], model_info["resolved"], model_info["available"])
            event_bus.emit_event(EventType.WARNING, {"message": (
                f"Configured AI model '{model_info['configured']}' not found. Using "
                f"'{model_info['resolved']}'. Pull the model or change it in Settings.")})

        if not getattr(self, "_provider_tested", False):
            self._provider_tested = True
            try:
                test_result = provider.test_connection(base_url)
                event_bus.emit_event(EventType.AI_PROVIDER_TEST, {
                    "provider": provider_name, "model": model,
                    "connected": test_result.get("connected", False), "details": test_result})
            except Exception as e:
                _ai_log.warning("AI provider test failed: %s", e)

        messages = self._context.format_messages()
        # Ground the model: real clock + only the memories the user stored.
        from datetime import datetime
        grounding = [{"role": "system", "content": datetime.now().strftime(
            "Current local date and time: %A, %B %d, %Y, %I:%M %p. Never guess the time; use this.")}]
        # Compact capability summary + domain hint. Never authoritative — the
        # model still chooses. This replaces dumping the full tool schema list
        # into prompts for chat-like requests.
        try:
            from modules.agent.capabilities import capability_summary, top_domain, score_domains
            hint = top_domain(prompt)
            scores = score_domains(prompt)
            summary = capability_summary()
            grounding.append({"role": "system", "content": (
                summary + (f"\n\nThis request looks like it's about: {hint}." if hint != "general" else "")
                + (f" (signals: {', '.join(f'{k}={v}' for k, v in sorted(scores.items(), key=lambda kv: -kv[1]))})"
                   if scores else "")
            )})
        except Exception:
            pass
        grounding += self._memory_messages(prompt)
        memory_msgs = grounding[1:]
        insert_at = 1 if messages and messages[0]["role"] == "system" else 0
        messages[insert_at:insert_at] = grounding

        use_tools = (provider_name == "ollama" and config.get("ai.tool_calling", True)
                     and config.get("agent.enabled", True))
        if use_tools:
            from modules.agent.llm import might_need_tool, model_supports_tools
            use_tools = might_need_tool(prompt) and model_supports_tools(model, base_url)
        _ai_log.info("AI request: provider=%s model=%s tools=%s memories=%d",
                     provider_name, model, use_tools, len(memory_msgs))

        _current_turn_id = turn_id
        first_token = [True]

        def _safe_on_token(tok: str):
            if _current_turn_id != self._turn_id:
                return
            if first_token[0]:
                first_token[0] = False
            on_token(tok)
            event_bus.emit_event(EventType.AI_STREAM_TOKEN, {
                "token": tok, "turn_id": _current_turn_id, "stream_id": stream_id,
                "request_id": request_id})

        try:
            if use_tools:
                from modules.agent.llm import run_with_tools
                full_text, info = run_with_tools(messages, model, base_url, temperature, timeout,
                                                 _safe_on_token, self._cancel_flag)
                self.expects_reply = bool(info.get("expects_reply"))
            else:
                # Plain chat: stream prose, but hold back anything that looks
                # like a tool call / JSON / code so it never reaches chat or TTS.
                from modules.agent.output import ReplyGuard, clean_reply, extract_tool_calls, pseudo_answer
                shown: List[str] = []

                def _emit(tok):
                    shown.append(tok)
                    _safe_on_token(tok)

                guard = ReplyGuard(_emit)
                provider.stream_send(
                    messages=messages, model=model, api_key=api_key, base_url=base_url,
                    temperature=temperature, timeout=timeout, on_token=guard.feed,
                    cancel_flag=self._cancel_flag)
                held = guard.finish()
                if held.strip() and not self._cancel_flag.is_set():
                    calls = extract_tool_calls(held)
                    answer = next((a for a in (pseudo_answer(n, a) for n, a in calls) if a), None)
                    if calls and answer is None and provider_name == "ollama":
                        # It tried to use a tool on the plain path: redo the turn with tools.
                        from modules.agent.llm import run_with_tools, model_supports_tools
                        if model_supports_tools(model, base_url):
                            _ai_log.info("ai.retry_with_tools (model wrote a tool call as text)")
                            run_with_tools(messages, model, base_url, temperature, timeout,
                                           _emit, self._cancel_flag)
                            answer = ""
                    if answer is None:
                        from modules.agent.output import honest
                        # The plain path runs no tools: an action claim here is false
                        # ("Closed the browser." when nothing was closed).
                        answer = honest(clean_reply(held, prompt), False)
                    if answer:
                        _emit(answer)
                    if not "".join(shown).strip():
                        _emit("Sorry, I couldn't work that out.")
                full_text = "".join(shown)
        except ProviderError as e:
            # A real provider failure must never become a fabricated reply.
            _ai_log.error("AI provider '%s' (model=%s) failed: %s", provider_name, model, e)
            event_bus.emit_event(EventType.AI_ERROR, {
                "error": str(e), "turn_id": turn_id, "request_id": request_id,
                "provider": provider_name, "model": model})
            from modules.ai.providers import ModelNotFoundError, ConnectionError as _ConnErr
            if isinstance(e, ModelNotFoundError):
                spoken = (f"I can't answer right now — the language model \"{model}\" isn't installed. "
                          f"Please pull it or pick another model in settings.")
            elif isinstance(e, _ConnErr):
                spoken = "I can't reach my language model right now. Please make sure Ollama is running."
            else:
                spoken = "I ran into a problem reaching my language model, so I couldn't answer that."
            if not self._cancel_flag.is_set():
                on_token(spoken)
            event_bus.emit_event(EventType.AI_STREAM_DONE, {
                "turn_id": turn_id, "request_id": request_id, "stream_id": stream_id,
                "response_length": len(spoken), "error": True})
            if on_done:
                on_done(spoken)
            return
        except Exception as e:
            _ai_log.exception("ai.unexpected_error")
            event_bus.emit_event(EventType.AI_ERROR, {"error": f"Unexpected error: {e}",
                                                      "turn_id": turn_id, "request_id": request_id})
            raise

        elapsed = time.perf_counter() - start
        if self._cancel_flag.is_set():
            event_bus.emit_event(EventType.AI_CANCELLED, {"reason": "interrupted", "turn_id": turn_id,
                                                          "request_id": request_id})
        else:
            self._context.add_assistant_turn(full_text)
            event_bus.emit_event(EventType.AI_STREAM_DONE, {
                "turn_id": turn_id, "request_id": request_id, "stream_id": stream_id,
                "elapsed_seconds": round(elapsed, 3), "response_length": len(full_text)})
            event_bus.emit_event(EventType.LATENCY_MODEL, {"ms": round(elapsed * 1000, 1),
                                                           "turn_id": turn_id, "request_id": request_id})
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

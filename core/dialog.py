"""
core/dialog.py

What is SAINT waiting for from the user right now? One read-only answer
instead of three places to check (a pending confirmation, a pending
"which one?" choice, or the LLM having asked a question).

    IDLE        nothing expected: a bare "yes" / "no" / "okay" needs "SAINT" first
    confirm     "Do you want me to close Discord?"  -> yes / no / cancel
    choice      "Which browser window?"             -> "the second one", "the YouTube one"
    reply       the model asked something            -> the next sentence goes to it

The voice module uses this to listen for a *short* answer (a one-word reply
is ~150 ms of speech, and it is said right away), and to keep the reply
window open for as long as the question is still pending.
"""

import time
from dataclasses import dataclass
from typing import Optional

from core.config import config


@dataclass
class Expectation:
    kind: str               # "confirm" | "choice" | "reply"
    prompt: str             # what SAINT asked (for the UI)
    seconds_left: float     # until the question expires


def current() -> Optional[Expectation]:
    try:
        from modules.agent.confirm import confirmations, choices
    except Exception:
        return None
    timeout = float(config.get("agent.confirm_timeout_sec", 30))
    p = confirmations.pending
    if p is not None:
        return Expectation("confirm", f"{p.description}?", max(0.0, timeout - (time.time() - p.created)))
    c = choices.pending
    if c is not None:
        return Expectation("choice", c.question, max(0.0, timeout * 2 - (time.time() - c.created)))
    try:
        from core.module_manager import module_manager
        ai = module_manager.get("ai")
    except Exception:
        ai = None
    if ai is not None and getattr(ai, "expects_reply", False):
        return Expectation("reply", "", float(config.get("voice.reply_window_sec", 20.0)))
    return None


def expects_short_answer() -> bool:
    """A yes/no or pick-one answer is expected (the model's open questions can
    get long answers, so they don't count)."""
    e = current()
    return e is not None and e.kind in ("confirm", "choice")


def reply_window(default: float = 8.0) -> float:
    """How long to keep listening for the answer without the wake word: as
    long as the question is pending, within voice.reply_window_sec."""
    cap = float(config.get("voice.reply_window_sec", 20.0) or 20.0)
    e = current()
    if e is None:
        return default
    return max(default, min(cap, e.seconds_left))

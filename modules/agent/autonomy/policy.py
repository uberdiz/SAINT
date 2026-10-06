"""
modules/agent/autonomy/policy.py

How risky each plan step is, and what the permission mode (Settings › Permissions,
``automation.permission_mode``) lets SAINT do without asking.

    LOW     opening / switching / moving windows, reading files and the clipboard, Spotify
            playback and volume, navigating and reading websites, searching
    MEDIUM  typing text, editing or moving files, installing packages, sending ordinary
            messages, closing apps (unsaved work)
    HIGH    deleting, uninstalling, arbitrary shell commands, system / security settings,
            power actions, payments, anything sending passwords or card numbers

    mode         LOW    MEDIUM                      HIGH
    safe         run    not allowed                 not allowed
    confirm      run    one "go ahead?" per plan    asked at that step
    autonomous   run    run                         asked at that step (unless
                                                    automation.confirm_dangerous is off)

Tools keep their own permission checks underneath (modules/automation/tools.py and
core/permissions.py) — the agent can never do more than a spoken command could. When the user
has just approved a HIGH step, that step's tool doesn't ask a second time, except the tools that
always ask with their own specific question (moving files to the Recycle Bin, uninstalling,
power, force quit).
"""

import re
import threading
from typing import Optional

from core.config import config
from modules.agent.autonomy.model import Risk

RUN, CONFIRM_PLAN, CONFIRM_STEP, DENY = "run", "confirm_plan", "confirm_step", "deny"

_HIGH = re.compile(
    r"\b(?:delete|erase|wipe|destroy|empty (?:the )?recycl\w* bin|uninstall|format|shut ?down|restart (?:the |my )?"
    r"(?:pc|computer)|reboot|log ?off|sign out|sleep|hibernate|lock (?:the |my )?(?:pc|computer)|force (?:quit|close)|"
    r"end task|kill|run (?:the )?command|powershell|cmd\b|terminal command|shell|registry|regedit|firewall|"
    r"defender|antivirus|uac|admin(?:istrator)?|password|credit card|card number|social security|bank|"
    r"pay\b|payment|purchase|buy\b|checkout|transfer|wire|send money|venmo|paypal|crypto)\b", re.I)
_MEDIUM = re.compile(
    r"\b(?:type|write|enter|fill (?:in|out)|paste|edit|rename|move (?:the |my |this |that )?(?:file|folder)|"
    r"copy (?:the |my )?(?:file|folder)|install|pip|npm|update|upgrade|send|post|reply|email|e-mail|message|dm|"
    r"tweet|comment|download|close|quit|exit|save|submit|click (?:the )?(?:send|post|submit|save|confirm|ok)|"
    r"recycle|extract|unzip|compress|record)\b", re.I)

# Intents whose tool is known to be risky regardless of wording.
_INTENT_RISK = {"desktop.type_text": Risk.MEDIUM, "desktop.compose_type": Risk.MEDIUM,
                "desktop.press_keys": Risk.MEDIUM, "desktop.close_window": Risk.MEDIUM,
                "files.recycle": Risk.HIGH, "files.move": Risk.MEDIUM, "steam.uninstall": Risk.HIGH,
                "system.power": Risk.HIGH, "desktop.force_quit": Risk.HIGH, "system.end_task": Risk.HIGH}

ALWAYS_ASK = frozenset({"files.recycle", "files.move", "steam.uninstall", "system.power",
                        "desktop.force_quit", "system.end_task"})


def classify(action: str, intent_name: str = "") -> str:
    """LOW / MEDIUM / HIGH for one SAINT command."""
    if intent_name in _INTENT_RISK:
        return _INTENT_RISK[intent_name]
    if intent_name:
        try:
            from modules.automation.tools import get_tool_registry
            tool = get_tool_registry().get(intent_name)
            if tool is not None:
                return tool.permission.value
        except Exception:
            pass
    text = action or ""
    if _HIGH.search(text):
        return Risk.HIGH
    if _MEDIUM.search(text):
        return Risk.MEDIUM
    return Risk.LOW


def mode() -> str:
    m = str(config.get("automation.permission_mode", "confirm") or "confirm").lower()
    return m if m in ("safe", "confirm", "autonomous") else "confirm"


def decide(risk: str, permission_mode: Optional[str] = None) -> str:
    m = permission_mode or mode()
    if risk == Risk.LOW:
        return RUN
    if m == "safe":
        return DENY
    if risk == Risk.MEDIUM:
        return RUN if m == "autonomous" else CONFIRM_PLAN
    # HIGH
    if m == "autonomous" and not config.get("automation.confirm_dangerous", True):
        return RUN
    return CONFIRM_STEP


# ---- approvals the user gave for a specific step ------------------------------------------------
_approved = threading.local()


class approved_step:
    """``with approved_step():`` — the user said yes to this exact step: a tool asking for the
    same confirmation again runs without a second question (ALWAYS_ASK tools still ask)."""

    def __enter__(self):
        _approved.on = True
        return self

    def __exit__(self, *exc):
        _approved.on = False


def preapproved(tool: str) -> bool:
    return bool(getattr(_approved, "on", False)) and tool not in ALWAYS_ASK

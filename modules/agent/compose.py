"""
modules/agent/compose.py

Writing text the user asked for, instead of typing their words back.

    "type out a summary of what SAINT is"   -> a summary, written by the model, then typed
    "write a short paragraph about dogs"    -> a paragraph about dogs, typed where the cursor is
    "type hello world"                      -> "hello world" (literal: not a description of text)

The router decides deterministically whether what follows "type"/"write" is
the text itself or a *description* of text; only a description costs one model
call. Emails and messages are not here — they are multi-step jobs taught as
lessons (modules/learning/lesson.py), which use ``write`` below for their drafts.
"""

import re
from typing import Dict, Optional

# "a summary of ...", "a short paragraph about ...", "an explanation of ...", "a haiku", "3 sentences about ..."
_KINDS = (r"summary|summari[sz]ation|paragraph|description|explanation|essay|poem|story|bio|biography|intro|"
          r"introduction|overview|list|note|letter|haiku|limerick|joke|tweet|post|caption|review|outline|recap|"
          r"definition|sentence|sentences|blurb|pitch|tagline|slogan|thank you note|cover letter|response|reply|"
          r"answer|comment|abstract|synopsis|toast|speech|rhyme|riddle|fun fact|fact|facts|tl;?dr")
_DESCRIBES = re.compile(
    r"^(?:out\s+|up\s+|me\s+)?(?:(?:a|an|the|some|my|one|two|three|four|five|\d+)\s+)?"
    r"(?:(?:short|quick|brief|long|detailed|little|small|simple|nice|funny|formal|casual|friendly|professional|"
    r"good|polite|one[- ]line|one[- ]sentence|two[- ]sentence|few)\s+)*"
    rf"(?P<kind>{_KINDS})\b(?P<rest>.*)$", re.I)
_ABOUT = re.compile(r"^\s*(?:of|about|on|for|explaining|describing|that (?:says|explains|describes)|saying|"
                    r"regarding|covering|introducing|why|how|what|to)\b", re.I)
_STANDALONE = re.compile(r"^(?:haiku|limerick|joke|poem|riddle|fun fact|rhyme|tagline|slogan|pitch|toast)$", re.I)


def describes_text(spoken: str) -> bool:
    """Is ``spoken`` (what came after "type"/"write") a description of text to write,
    rather than the text itself?"""
    s = (spoken or "").strip().strip('"“”').rstrip(".!?")
    m = _DESCRIBES.match(s)
    if not m:
        return False
    rest = m.group("rest")
    return bool(_ABOUT.match(rest)) or (not rest.strip() and bool(_STANDALONE.match(m.group("kind"))))


def about_saint() -> str:
    """What SAINT is, for text the user asks to be written about SAINT itself."""
    lines = ["SAINT is the user's own AI desktop assistant for Windows, with a companion iPhone app. You talk to it "
             "by voice (\"SAINT, ...\" or \"Hey SAINT\") or by typing. It runs locally on the PC with a local "
             "language model (Ollama), and does real things through deterministic tools rather than guessing: it "
             "opens and arranges apps and windows across monitors, controls Spotify and media, manages files and "
             "cleans up drives, sets reminders and automations, remembers what you tell it, sees the screen when "
             "asked, and learns new tasks when you walk it through them once. Its devices connect privately over "
             "an encrypted link (SAINT Link) so the phone can use the PC's model and everything learned is shared."]
    try:
        from modules.agent.capabilities import capability_summary
        lines.append(capability_summary())
    except Exception:
        pass
    return "\n".join(lines)


def write(description: str, details: Optional[Dict[str, str]] = None, change: str = "", current: str = "",
          one_line: bool = False, timeout: float = 60.0) -> str:
    """The text ``description`` asks for ("a summary of what SAINT is"), as the user will send or type it.
    '' when the model didn't answer."""
    from modules.agent.llm import complete
    prompt = f"Write {description.strip().rstrip('.')}."
    if details:
        known = {k: v for k, v in details.items() if v}
        if known:
            prompt += "\nUse these details:\n" + "\n".join(f"- {k.replace('_', ' ')}: {v}" for k, v in known.items())
    if change and current:
        prompt += f"\n\nThe current version is:\n{current}\n\nChange it like this: {change}"
    system = ("You write text the user will type or send as their own. Output only that text — no preamble, no "
              "quotes around it, no notes, no markdown." + (" One short line." if one_line else
                                                             " Keep it natural and to the point."))
    blob = " ".join([description, *(details or {}).values()])
    if re.search(r"\bsaint\b", blob, re.I):
        system += "\n\nFacts about SAINT (use them; don't invent features):\n" + about_saint()
    out = complete(prompt, system=system, timeout=timeout, max_tokens=60 if one_line else 600) or ""
    out = out.strip().strip('"“”')
    out = re.sub(r"^(?:sure|okay|ok|here(?:'s| is)[^:\n]{0,60}):\s*", "", out, flags=re.I).strip()
    if one_line:
        out = re.sub(r"^(?:subject|title)\s*:\s*", "", out.splitlines()[0] if out else "", flags=re.I)
    return out.strip()

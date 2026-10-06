"""
modules/voice/speech_text.py

Text as it should be *said*. Kokoro spells out words written in capitals
("NO ME QUIERO CASAR" -> "N. O. M. E. ..."), so song titles, artist names and
shouted words are turned into ordinary case first, while real abbreviations
(PC, USB, GPU, SSD) keep their letters.
"""

import re

# Said letter by letter — keep as capitals.
_ACRONYMS = {
    "AI", "PC", "TV", "OK", "ID", "IP", "UI", "OS", "CD", "DJ", "EP", "LP", "MC", "VR", "AR", "HD", "UK", "US", "EU",
    "USB", "GPU", "CPU", "RAM", "SSD", "HDD", "API", "URL", "VPN", "DNS", "FPS", "RGB", "LED", "LCD", "PDF", "ZIP",
    "MP3", "MP4", "HTML", "CSS", "JSON", "SQL", "HTTP", "HTTPS", "HDMI", "NVME", "BIOS", "UEFI", "DPI", "FAQ",
    "CEO", "FBI", "CIA", "NBA", "NFL", "BBC", "CNN", "ESPN", "USA", "ASAP", "DIY", "BTW", "FYI", "IDK", "TBH",
    "LOL", "OMG", "PS", "PS5", "XP", "NPC", "RPG", "FPS", "MMO", "DLC", "AM", "PM", "GB", "MB", "KB", "TB", "GHZ",
    "MHZ", "KBPS", "MBPS", "WIFI", "SMS", "GPS", "NYC", "LA", "UFC", "WWE", "VIP", "BPM", "DM", "DMS", "RSVP",
    "IDE", "SDK", "EXE", "MCP", "NAS", "SSH", "VS",
}
# Short words that are words, not initials, when written in capitals.
_WORDS = {
    "A", "I", "THE", "AND", "BUT", "FOR", "NOR", "YET", "YOU", "ME", "MY", "WE", "US", "HE", "SHE", "HER", "HIM",
    "HIS", "IT", "ITS", "IS", "AM", "ARE", "WAS", "BE", "BY", "TO", "OF", "IN", "ON", "AT", "UP", "NO", "SO", "GO",
    "DO", "OH", "HI", "YO", "YA", "YES", "NOT", "ALL", "OUT", "NOW", "NEW", "BAD", "GOD", "WHY", "HOW", "WHO",
    "OFF", "GET", "GOT", "LET", "SEE", "SAY", "ONE", "TWO", "TEN", "BIG", "HOT", "RED", "SAD", "MAD", "BOY", "MAN",
    "DAY", "WAY", "OUR", "HEY", "WOW", "OUR", "CAN", "WON", "FUN", "RUN", "SUN", "LIE", "DIE", "CRY", "FLY", "SKY",
    "TOO", "TOP", "OLD", "ANY", "LA", "DE", "EL", "TU", "TE", "SE", "LO", "MI", "YO", "SI", "ES", "EN", "UN",
    "LOS", "LAS", "QUE", "CON", "SIN", "MAS", "DEL", "UNA", "OK",
}
_VOWEL = re.compile(r"[AEIOUY]")
_CAPS_WORD = re.compile(r"\b[A-Z][A-Z'’]*[A-Z]\b|\b[A-Z]\b")


def _fix(word: str, sentence_caps: bool) -> str:
    core = word.replace("’", "'")
    bare = core.replace("'", "")
    if bare in _ACRONYMS and not (sentence_caps and bare in _WORDS):
        return word
    if len(bare) == 1:
        return word if bare in ("A", "I") or not sentence_caps else word.lower()
    if bare in _WORDS or (len(bare) >= 4 and _VOWEL.search(bare)) or (sentence_caps and _VOWEL.search(bare)):
        return word.capitalize() if word[0].isalpha() else word
    return word                    # "NVMW", "XQZ": no way to say it but by letters


# A Windows path: folders may contain spaces; the last part ends at a file extension or a space.
_PATH = re.compile(r"\b[A-Za-z]:\\(?:[^\\\n\"“”<>|?*:]+\\)*"
                   r"(?:[^\\\n\"“”<>|?*:]*?\.[A-Za-z0-9]{1,5}\b|[^\s\\\"“”<>|?*:.,;!]*)")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_URL = re.compile(r"\bhttps?://(?:www\.)?([^/\s\"”'<>]+)[^\s\"”'<>]*", re.I)


def _say_path(m) -> str:
    raw = m.group(0)
    last = raw.rstrip("\\").rsplit("\\", 1)[-1]
    if not last or last.endswith(":"):
        return f"the {raw[0].upper()} drive"
    stem, dot, ext = last.rpartition(".")
    if dot and stem and ext.lower() in ("lnk", "url", "exe"):
        return stem                       # "Antigravity IDE.lnk" -> "Antigravity IDE"
    return last


def shorten_paths(text: str) -> str:
    """Paths and links as a person would say them: 'Made it in C:\\Users\\sam\\Downloads'
    -> 'Made it in Downloads'; 'https://mail.google.com/mail/u/0' -> 'mail.google.com'.
    Read out whole, they're long and Kokoro's phonemizer switches language on the
    fragments (a French accent on 'C:\\Users\\...', 2026-10-05)."""
    if text and "@" in text:
        text = _EMAIL.sub(lambda m: m.group(0).replace(".", " dot ").replace("@", " at "), text)
    if not text or (":\\" not in text and "://" not in text):
        return text
    text = _URL.sub(lambda m: m.group(1), text)
    return _PATH.sub(_say_path, text)


def for_speech(text: str) -> str:
    """'Playing NO ME QUIERO CASAR by Bad Bunny on your PC' ->
    'Playing No Me Quiero Casar by Bad Bunny on your PC'. Paths and links are shortened."""
    text = shorten_paths(text)
    if not text or not re.search(r"[A-Z]{2}", text):
        return text
    words = re.findall(r"[A-Za-z][A-Za-z'’]*", text)
    caps = [w for w in words if len(w) > 1 and w.isupper()]
    # Most of it in capitals (a shouted line, an all-caps title): even short
    # words like "ME" / "NO" are words, not initials.
    sentence_caps = len(caps) >= 2 and len(caps) >= 0.5 * len([w for w in words if len(w) > 1])
    text = _CAPS_WORD.sub(lambda m: _fix(m.group(0), sentence_caps), text)
    # A run of 2+ capitalised words inside a normal sentence is a title: fix its short words too.
    return re.sub(r"\b(?:[A-Z][A-Za-z'’]*\s+){1,}[A-Z][A-Za-z'’]*\b",
                  lambda m: " ".join(_fix(w, True) if w.isupper() else w for w in m.group(0).split()), text)

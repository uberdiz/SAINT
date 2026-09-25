"""
modules/steam/vdf.py

A small reader for Valve's text KeyValues format (libraryfolders.vdf,
appmanifest_*.acf):

    "AppState"
    {
        "appid"     "105600"
        "name"      "Terraria"
    }

Returns nested dicts of strings. Keys are case-preserved; ``get_ci`` looks
them up case-insensitively (Valve isn't consistent about case).
"""

from typing import Any, Dict, List, Tuple


def _tokens(text: str) -> List[Tuple[str, str]]:
    """[(kind, value)] where kind is 'str', '{' or '}'."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif c in "{}":
            out.append((c, c))
            i += 1
        elif c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    buf.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(nxt, "\\" + nxt))
                    i += 2
                    continue
                buf.append(text[i])
                i += 1
            out.append(("str", "".join(buf)))
            i += 1
        else:                                   # unquoted token (rare)
            j = i
            while j < n and text[j] not in ' \t\r\n{}"':
                j += 1
            out.append(("str", text[i:j]))
            i = j
    return out


def loads(text: str) -> Dict[str, Any]:
    toks = _tokens(text)
    pos = 0

    def parse_obj() -> Dict[str, Any]:
        nonlocal pos
        obj: Dict[str, Any] = {}
        while pos < len(toks):
            kind, val = toks[pos]
            if kind == "}":
                pos += 1
                return obj
            if kind != "str":
                pos += 1
                continue
            key = val
            pos += 1
            if pos >= len(toks):
                break
            kind2, val2 = toks[pos]
            if kind2 == "{":
                pos += 1
                obj[key] = parse_obj()
            elif kind2 == "str":
                obj[key] = val2
                pos += 1
            else:
                pos += 1
        return obj

    return parse_obj()


def load(path: str) -> Dict[str, Any]:
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")      # older manifests ("Call of Duty®")
    return loads(text)


def get_ci(d: Dict[str, Any], key: str, default=None):
    if not isinstance(d, dict):
        return default
    if key in d:
        return d[key]
    low = key.lower()
    for k, v in d.items():
        if k.lower() == low:
            return v
    return default

"""
modules/memory/passive.py

Facts people mention in passing — "I'm a nurse", "I hate horror movies",
"my sister's name is Mia", "I'm learning Japanese". SAINT keeps them as
*learned* memories (see modules/memory/service.py) without interrupting the
conversation. Deliberately conservative: a sentence has to be a plain,
first-person statement about a lasting trait; moods ("I'm tired"),
questions, requests and lyrics-like fragments are ignored.
"""

import re
from typing import Dict, List

_OCCUPATIONS = (
    "student|engineer|developer|programmer|coder|designer|teacher|professor|nurse|doctor|dentist|artist|"
    "musician|gamer|streamer|writer|author|manager|analyst|scientist|lawyer|chef|cook|consultant|freelancer|"
    "founder|intern|researcher|editor|photographer|producer|youtuber|mechanic|electrician|plumber|accountant|"
    "marketer|architect|pilot|driver|barista|cashier|firefighter|paramedic|pharmacist|therapist|"
    "salesperson|entrepreneur|carpenter|technician|trader|investor|contractor|soldier|officer|"
    "graphic designer|web developer|software engineer|data scientist|video editor|content creator|dj|"
    "high schooler|freshman|sophomore|junior|senior|college student|grad student|phd student|retiree"
)
_RELATIONS = ("wife|husband|partner|girlfriend|boyfriend|fianc[eé]e?|mom|mum|mother|dad|father|brother|sister|"
              "son|daughter|best friend|friend|boss|roommate|cousin|grandma|grandpa|grandmother|grandfather|"
              "uncle|aunt|niece|nephew|kid|baby|coworker|teammate")
_PETS = "dog|cat|puppy|kitten|hamster|rabbit|bunny|parrot|bird|fish|turtle|snake|lizard|horse|guinea pig|ferret"

# Things that make "I love X" / "I hate X" not a lasting preference.
_NOT_TOPIC = re.compile(
    r"^(?:it|that|this|you|them|him|her|when|how|what|the way|to (?:know|hear|see)|doing that|"
    r"waiting|this song|that song|this one|that one|your|my life)\b", re.I)
_TRANSIENT = re.compile(r"\b(?:today|tonight|right now|now|at the moment|this (?:morning|evening|week)|"
                        r"currently feeling|so tired|hungry|bored|sick)\b", re.I)


def _clean(text: str) -> str:
    t = re.sub(r"^(?:hey\s+)?saint[,.!\s]+", "", (text or "").strip(), flags=re.I)
    t = re.sub(r"^(?:well|so|oh|um+|uh+|yeah|actually|also|and|btw|by the way|honestly|you know)[,\s]+", "", t,
               flags=re.I)
    return t.strip().rstrip(".!")


def _topic(s: str, max_words: int = 6) -> str:
    s = re.sub(r"\s+(?:a lot|so much|very much|too|as well|lol|haha)$", "", s.strip(), flags=re.I)
    s = re.split(r"\s+(?:but|because|since|so|and then|although)\s+", s, 1, flags=re.I)[0]
    return s if 0 < len(s.split()) <= max_words else ""


def _original(t: str, low: str, topic: str, start: int) -> str:
    """``topic`` (found in the lower-cased text) with the user's own capitalisation."""
    i = low.find(topic, start)
    return t[i:i + len(topic)].strip() if i >= 0 else topic


def extract(text: str) -> List[Dict[str, str]]:
    """Plain first-person statements in ``text`` → [{key, value, category, content}]."""
    out: List[Dict[str, str]] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\s*;\s*", text or ""):
        t = _clean(sentence)
        if not t or t.endswith("?") or len(t.split()) > 18 or _TRANSIENT.search(t):
            continue
        low = t.lower()
        f = _one(t, low)
        if f:
            out.append(f)
    return out


def _one(t: str, low: str):
    m = re.match(rf"^i(?:'m| am) (?:an? |the )?((?:\w+ ){{0,2}}(?:{_OCCUPATIONS}))(?: (?:at|for|in) (.+))?$", low)
    if m:
        job = m.group(1).strip()
        where = f" at {t[m.start(2):].strip()}" if m.group(2) else ""
        return {"key": "occupation", "value": job + where, "category": "identity", "content": f"I'm a {job}{where}"}
    m = re.match(r"^i work as (?:an? )?(.{2,40})$", low)
    if m:
        return {"key": "occupation", "value": m.group(1), "category": "identity", "content": f"I work as a {m.group(1)}"}
    m = re.match(r"^i(?:'m| am) (\d{1,2})(?: years? old)?$", low) or re.match(r"^i(?:'m| am) (\d{1,2}) years? old\b", low)
    if m and 5 <= int(m.group(1)) <= 99:
        return {"key": "age", "value": m.group(1), "category": "identity", "content": f"I'm {m.group(1)} years old"}
    m = re.match(r"^i(?:'m| am) (?:originally )?from (.{2,40})$", low)
    if m:
        place = t[m.start(1):].strip()
        return {"key": "hometown", "value": place, "category": "identity", "content": f"I'm from {place}"}
    m = re.match(r"^i (?:study|major in|am studying|'m studying)\s+(.{2,40}?)(?:\s+at\s+(.{2,40}))?$", low)
    if m:
        subj = m.group(1)
        at = f" at {t[m.start(2):].strip()}" if m.group(2) else ""
        return {"key": "studies", "value": subj + at, "category": "identity", "content": f"I study {subj}{at}"}
    m = re.match(r"^i go to (.{2,50}?(?:school|university|college|academy|high))$", low)
    if m:
        school = t[m.start(1):].strip()
        return {"key": "school", "value": school, "category": "identity", "content": f"I go to {school}"}
    m = re.match(rf"^i have an? ({_PETS})(?: (?:named|called) (\w[\w' -]{{0,20}}))?$", low)
    if m:
        pet = m.group(1)
        name = t[m.start(2):].strip().title() if m.group(2) else ""
        return {"key": f"pet {pet}", "value": name or pet, "category": "person",
                "content": f"I have a {pet}" + (f" named {name}" if name else "")}
    m = re.match(rf"^my ({_PETS}|{_RELATIONS})(?:'s name| is named| is called|'s called| name) is ([\w' -]{{1,24}})$", low) \
        or re.match(rf"^my ({_PETS}|{_RELATIONS}) is (?:named|called) ([\w' -]{{1,24}})$", low)
    if m:
        name = t[m.start(2):m.end(2)].strip().title()
        return {"key": f"{m.group(1)}'s name", "value": name, "category": "person",
                "content": f"My {m.group(1)}'s name is {name}"}
    m = re.match(r"^i(?:'m| am) (?:learning|teaching myself|getting into) (.{2,40})$", low)
    if m and _topic(m.group(1)):
        what = t[m.start(1):].strip()
        return {"key": f"learning {what.lower()}", "value": what, "category": "goal", "content": f"I'm learning {what}"}
    m = re.match(r"^i(?:'m| am) (?:working on|building|making|developing|writing) (?:an? |my )?(.{3,50})$", low)
    if m and _topic(m.group(1), 8) and not re.search(r"\b(it|that|this)\b", m.group(1)):
        what = t[m.start(1):].strip()
        return {"key": f"project {what.lower()[:30]}", "value": what, "category": "project",
                "content": f"I'm working on {what}"}
    m = re.match(r"^i (?:want|plan|hope|am trying|'m trying) to (.{3,50})$", low)
    if m and re.search(r"\b(learn|become|get|build|start|finish|move|save|run|lose|gain|visit|travel|buy)\b",
                       m.group(1)) and _topic(m.group(1), 8):
        what = t[m.start(1):].strip()
        return {"key": f"goal {what.lower()[:30]}", "value": what, "category": "goal", "content": f"I want to {what}"}
    m = re.match(r"^i (?:really |absolutely |totally )?(love|like|enjoy|adore)\s+(.+)$", low) or \
        re.match(r"^i(?:'m| am) (?:really |so |totally )?(into|obsessed with|a (?:big |huge )?fan of)\s+(.+)$", low)
    if m:
        topic = _topic(m.group(2))
        if topic and not _NOT_TOPIC.match(topic):
            what = _original(t, low, topic, m.start(2))
            return {"key": f"likes {what.lower()}", "value": what, "category": "interest", "content": f"I like {what}"}
    m = re.match(r"^i (?:really |absolutely |totally )?(hate|dislike|can'?t stand|don'?t like|do not like)\s+(.+)$",
                 low) or re.match(r"^i(?:'m| am) (not a (?:big )?fan of|not into)\s+(.+)$", low)
    if m:
        topic = _topic(m.group(2))
        if topic and not _NOT_TOPIC.match(topic):
            what = _original(t, low, topic, m.start(2))
            return {"key": f"dislikes {what.lower()}", "value": what, "category": "dislike",
                    "content": f"I don't like {what}"}
    m = re.match(r"^i play (?:the )?(.{2,30})$", low)
    if m and _topic(m.group(1), 4) and not re.search(r"\b(it|that|this|music|song|some)\b", m.group(1)):
        what = t[m.start(1):].strip()
        return {"key": f"plays {what.lower()}", "value": what, "category": "interest", "content": f"I play {what}"}
    m = re.match(r"^i (?:usually|always|normally|often) (.{4,60})$", low)
    if m and not re.search(r"\b(forget|wonder|think|feel)\b", m.group(1)):
        what = t[m.start(1):].strip()
        return {"key": f"routine {what.lower()[:30]}", "value": what, "category": "routine",
                "content": f"I usually {what}"}
    m = re.match(r"^i (?:use|main|code in|program in|drive|drive an?) (?:an? )?([\w.+# -]{2,30})"
                 r"(?: for (?:work|coding|gaming|school|everything))?$", low)
    if m and not re.search(r"\b(it|that|this|the|my)\b", m.group(1)):
        what = t[m.start(1):m.end(1)].strip()
        return {"key": f"uses {what.lower()}", "value": what, "category": "interest", "content": f"I use {what}"}
    return None

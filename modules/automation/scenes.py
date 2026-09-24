"""
modules/automation/scenes.py

Scenes: a named list of spoken-style commands SAINT runs in order, e.g.

    Focus mode  →  "play my focus playlist", "set volume to 30", "open notion"

Triggered by voice ("focus mode", "run focus mode", "start focus mode scene"),
from the UI, or on a schedule (a scheduler "command" automation that says
"run <name>"). Steps go through the same agent router as voice commands, so a
scene can do anything a command can. Stored in data/scenes.json.
"""

import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional

from core.events import event_bus, EventType
from core.paths import data_path

log = logging.getLogger("saint.scenes")

_VERB = re.compile(r"^(?:please\s+)?(?:run|start|activate|begin|launch|do|enable|trigger)\s+(?:the\s+|my\s+)?")
_SUFFIX = re.compile(r"\s+(?:scene|routine)$")
_RUNNER_PREFIX = "scene-"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", (text or "").lower())).strip()


@dataclass
class Scene:
    name: str
    steps: List[str] = field(default_factory=list)
    phrase: str = ""                  # extra voice trigger, e.g. "movie time"
    schedule: str = ""                # natural language, e.g. "every weekday at 8"
    automation_id: str = ""           # scheduler entry backing ``schedule``
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    last_run: float = 0.0


class SceneStore:
    def __init__(self, path=None):
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self):
        return self._path or data_path("scenes.json")

    # ------------------------------------------------------------------ #
    def all(self) -> List[Scene]:
        with self._lock:
            return self._load()

    def _load(self) -> List[Scene]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return []
        known = Scene.__dataclass_fields__
        return [Scene(**{k: v for k, v in s.items() if k in known}) for s in raw if s.get("name")]

    def _write(self, scenes: List[Scene]):
        tmp = str(self.path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([asdict(s) for s in scenes], f, indent=2)
        os.replace(tmp, self.path)

    def get(self, sid: str) -> Optional[Scene]:
        return next((s for s in self.all() if s.id == sid), None)

    def save(self, scene: Scene) -> Scene:
        scene.name = scene.name.strip()
        scene.steps = [s.strip() for s in scene.steps if s and s.strip()]
        if not scene.name:
            raise ValueError("Give the scene a name.")
        if not scene.steps:
            raise ValueError("Add at least one step.")
        self._sync_schedule(scene)
        with self._lock:
            scenes = [s for s in self._load() if s.id != scene.id]
            clash = next((s for s in scenes if _norm(s.name) == _norm(scene.name)), None)
            if clash:
                raise ValueError(f"There's already a scene called “{clash.name}”.")
            scenes.append(scene)
            self._write(scenes)
        event_bus.emit_event(EventType.AUTOMATION_UPDATED, {"id": scene.id, "title": scene.name, "scene": True})
        return scene

    def delete(self, sid: str):
        scene = self.get(sid)
        if scene and scene.automation_id:
            self._drop_schedule(scene.automation_id)
        with self._lock:
            self._write([s for s in self._load() if s.id != sid])
        event_bus.emit_event(EventType.AUTOMATION_CANCELLED, {"id": sid, "scene": True})

    # ------------------------------------------------------------------ #
    # Schedule: a scheduler "command" automation that says "run <name>"
    # ------------------------------------------------------------------ #
    def _sync_schedule(self, scene: Scene):
        from modules.automation.scheduler import scheduler
        from modules.automation.timeparse import parse_schedule
        if scene.automation_id:
            self._drop_schedule(scene.automation_id)
            scene.automation_id = ""
        if not scene.schedule.strip():
            return
        sched, _ = parse_schedule(scene.schedule)
        if not sched:
            raise ValueError(f"I can't read the schedule “{scene.schedule}”.")
        a = scheduler.create("command", f"run {scene.name}", sched, title=f"Scene · {scene.name}")
        scene.automation_id = a.id

    @staticmethod
    def _drop_schedule(aid: str):
        from modules.automation.scheduler import scheduler
        try:
            scheduler.delete(aid)
        except Exception:
            log.exception("scenes.schedule_delete_failed id=%s", aid)

    # ------------------------------------------------------------------ #
    # Voice matching + running
    # ------------------------------------------------------------------ #
    def match(self, text: str) -> Optional[Scene]:
        # Steps of a running scene never trigger scenes (no loops).
        if threading.current_thread().name.startswith(_RUNNER_PREFIX):
            return None
        t = _norm(text)
        if not t:
            return None
        bare = _SUFFIX.sub("", _VERB.sub("", t))
        for s in self.all():
            names = {_norm(s.name), _SUFFIX.sub("", _norm(s.name)), _norm(s.phrase)} - {""}
            if t in names or bare in names:
                return s
        return None

    def run(self, scene: Scene, run_command: Callable[[str], str]) -> List[str]:
        log.info("scene.run name=%r steps=%d", scene.name, len(scene.steps))
        results = []
        for step in scene.steps:
            try:
                results.append(run_command(step) or "")
            except Exception as e:
                log.exception("scene.step_failed %r", step)
                results.append(f"Failed: {e}")
            time.sleep(0.4)            # let Spotify / windows settle between steps
        scene.last_run = time.time()
        with self._lock:
            scenes = self._load()
            for s in scenes:
                if s.id == scene.id:
                    s.last_run = scene.last_run
            self._write(scenes)
        event_bus.emit_event(EventType.AUTOMATION_TRIGGERED, {
            "id": scene.id, "title": scene.name, "kind": "scene", "result": " · ".join(r for r in results if r)})
        return results

    def run_in_background(self, scene: Scene, run_command: Optional[Callable[[str], str]] = None,
                          on_done: Optional[Callable[[List[str]], None]] = None):
        if run_command is None:
            from modules.agent.agent import agent
            run_command = agent.run_command

        def go():
            res = self.run(scene, run_command)
            if on_done:
                on_done(res)
        threading.Thread(target=go, daemon=True, name=f"{_RUNNER_PREFIX}{scene.id}").start()


scenes = SceneStore()


if __name__ == "__main__":        # quick self-check: python -m modules.automation.scenes
    import tempfile
    store = SceneStore(os.path.join(tempfile.mkdtemp(), "scenes.json"))
    store._sync_schedule = lambda s: None
    store.save(Scene("Focus mode", ["play lofi", "set volume to 30"], phrase="deep work"))
    for said in ("focus mode", "Run focus mode.", "start the focus mode scene", "deep work"):
        assert store.match(said) and store.match(said).name == "Focus mode", said
    assert store.match("play focus mode playlist") is None
    ran = store.run(store.all()[0], lambda step: f"did {step}")
    assert ran == ["did play lofi", "did set volume to 30"], ran
    print("scenes ok")

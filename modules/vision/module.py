"""
modules/vision/module.py

Screen awareness: capture, OS-level UI context, and (optional) visual
analysis through a vision-capable local model. See modules/vision/screen.py.
"""

from modules.automation.tools import Tool, PermissionLevel, P, get_tool_registry
from modules.base import BaseModule


class VisionModule(BaseModule):
    name = "Vision"
    description = "Screen capture, active-window UI understanding, optional local vision-model analysis."

    def __init__(self):
        super().__init__()
        self.subtasks = {
            "Screen Capture": True,
            "UI Element Context": True,
            "Window / Monitor Context": True,
            "Visual Analysis (vision model)": False,   # becomes True when a model is configured
            "OCR": False,
        }

    def enable(self):
        super().enable()
        from modules.vision.screen import visual_analyzer
        self.subtasks["Visual Analysis (vision model)"] = visual_analyzer.available()[0]
        self._register_tools()

    def _register_tools(self):
        from modules.vision import screen

        def capture(monitor=None):
            res = screen.capture(monitor)
            res.pop("image", None)
            return res

        def context(monitor=None):
            ctx = screen.screen_context(True, monitor=monitor)
            ctx["summary"] = screen.describe_context(ctx)
            return ctx

        def analyze(question="Describe what is on the screen.", monitor=None):
            shot = screen.capture(monitor)
            answer = screen.visual_analyzer.analyze(shot["image"], question)
            return {"answer": answer, "path": shot.get("path")}

        def read_screen():
            from modules.desktop import uia
            res = uia.read_text()
            if not res["text"] and screen.visual_analyzer.available()[0]:
                shot = screen.capture(save=False)
                answer = screen.visual_analyzer.analyze(
                    shot["image"], "Read out the main visible text on the screen, verbatim.")
                res["text"] = [answer] if answer else []
            return res

        def locate(name):
            from modules.desktop import uia
            return uia.locate(name)

        def analyzer_available():
            return screen.visual_analyzer.available()

        reg = get_tool_registry()
        reg.register(Tool("screen.capture", "Take a screenshot (all monitors or one) and save it",
                          {"monitor": "int (optional)"}, PermissionLevel.LOW, capture,
                          parameters={"monitor": P("integer", "1-based monitor", required=False, minimum=1)},
                          llm_exposed=True, category="vision"))
        reg.register(Tool("screen.context", "See what is on the screen(s): monitors, open windows and "
                          "where they are, the focused window and its buttons, links, fields and text. "
                          "Use monitor='second' etc. for one screen. Answer from 'summary'.",
                          {"monitor": "string (optional)"}, PermissionLevel.LOW, context,
                          parameters={"monitor": P("string", "e.g. 'second', 'main', 'left', '2'", required=False)},
                          llm_exposed=True, category="vision"))
        reg.register(Tool("screen.read", "Read the visible text of the window the user is looking at",
                          {}, PermissionLevel.LOW, read_screen, parameters={}, llm_exposed=True, category="vision"))
        reg.register(Tool("screen.locate", "Find where a button/field/link is on screen (e.g. 'search box')",
                          {"name": "string"}, PermissionLevel.LOW, locate,
                          parameters={"name": P("string", "element label")}, llm_exposed=True, category="vision"))
        reg.register(Tool("screen.analyze", "Look at the screen with the local vision model and answer a question",
                          {"question": "string"}, PermissionLevel.LOW, analyze,
                          parameters={"question": P("string", required=False, default="Describe what is on the screen."),
                                      "monitor": P("integer", required=False, minimum=1)},
                          llm_exposed=True, category="vision", availability=analyzer_available))

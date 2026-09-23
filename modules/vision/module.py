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

        def context():
            return screen.screen_context()

        def analyze(question="Describe what is on the screen.", monitor=None):
            shot = screen.capture(monitor)
            answer = screen.visual_analyzer.analyze(shot["image"], question)
            return {"answer": answer, "path": shot.get("path")}

        def analyzer_available():
            return screen.visual_analyzer.available()

        reg = get_tool_registry()
        reg.register(Tool("screen.capture", "Take a screenshot (all monitors or one) and save it",
                          {"monitor": "int (optional)"}, PermissionLevel.LOW, capture,
                          parameters={"monitor": P("integer", "1-based monitor", required=False, minimum=1)},
                          llm_exposed=True, category="vision"))
        reg.register(Tool("screen.context", "List what is on screen: active window, open windows, "
                          "and the active window's buttons/fields (from Windows UI Automation)",
                          {}, PermissionLevel.LOW, context, parameters={}, llm_exposed=True, category="vision"))
        reg.register(Tool("screen.analyze", "Look at the screen with the local vision model and answer a question",
                          {"question": "string"}, PermissionLevel.LOW, analyze,
                          parameters={"question": P("string", required=False, default="Describe what is on the screen."),
                                      "monitor": P("integer", required=False, minimum=1)},
                          llm_exposed=True, category="vision", availability=analyzer_available))

"""
modules/vision/module.py

Planned for v0.5. Disabled by default in v0.1.
"""

from modules.base import BaseModule


class VisionModule(BaseModule):
    name = "Vision"
    description = "Screen understanding, OCR, and object detection (planned v0.5)."

    def __init__(self):
        super().__init__()
        self.subtasks = {
            "OCR": False,
            "Screen Capture": False,
            "Object Detection": False,
        }

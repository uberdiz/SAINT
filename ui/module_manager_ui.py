"""
ui/module_manager_ui.py

Shows every installed module, whether it's on/off, and its completion
percentage. Clicking a module expands its subtasks below.
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QCheckBox,
    QProgressBar, QPushButton,
)
from PySide6.QtCore import Qt

from core.module_manager import module_manager


class SubtaskList(QFrame):
    def __init__(self, module):
        super().__init__()
        self.setObjectName("SubtaskPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 8, 20, 8)
        for label, done in module.subtasks.items():
            row = QLabel(("\u2713 " if done else "\u25a1 ") + label)
            row.setObjectName("SubtaskDone" if done else "SubtaskPending")
            layout.addWidget(row)


class ModuleRow(QFrame):
    def __init__(self, key, module, on_toggle):
        super().__init__()
        self.setObjectName("ModuleRow")
        self.module = module
        self.expanded = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 10, 16, 10)
        outer.setSpacing(6)

        top = QHBoxLayout()

        self.checkbox = QCheckBox()
        self.checkbox.setChecked(module.enabled)
        self.checkbox.setEnabled(key == "ai")  # only AI is functional in v0.1
        self.checkbox.stateChanged.connect(lambda state: on_toggle(key, bool(state)))
        top.addWidget(self.checkbox)

        name = QLabel(module.name)
        name.setObjectName("ModuleName")
        top.addWidget(name)

        status = QLabel("ACTIVE" if module.enabled else "OFF")
        status.setObjectName("ModuleStatusOn" if module.enabled else "ModuleStatusOff")
        top.addWidget(status)

        top.addStretch()

        self.progress = QProgressBar()
        self.progress.setFixedWidth(160)
        self.progress.setValue(module.completion_percentage())
        top.addWidget(self.progress)

        self.expand_btn = QPushButton("Details")
        self.expand_btn.setFixedWidth(80)
        self.expand_btn.clicked.connect(self.toggle_expand)
        top.addWidget(self.expand_btn)

        outer.addLayout(top)

        desc = QLabel(module.description)
        desc.setObjectName("ModuleDescription")
        desc.setWordWrap(True)
        outer.addWidget(desc)

        self.subtask_panel = SubtaskList(module)
        self.subtask_panel.setVisible(False)
        outer.addWidget(self.subtask_panel)

    def toggle_expand(self):
        self.expanded = not self.expanded
        self.subtask_panel.setVisible(self.expanded)
        self.expand_btn.setText("Hide" if self.expanded else "Details")


class ModuleManagerUI(QWidget):
    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        title = QLabel("Module Manager")
        title.setObjectName("PageTitle")
        root.addWidget(title)

        subtitle = QLabel("Only AI is functional in v0.1 -- everything else is a real, "
                           "inert module waiting for its version to land.")
        subtitle.setObjectName("Subtitle")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        for key, module in module_manager.modules.items():
            row = ModuleRow(key, module, self._on_toggle)
            root.addWidget(row)

        root.addStretch()

    def _on_toggle(self, key, enabled):
        module_manager.set_enabled(key, enabled)

"""
ui/theme.py

Minimal, clean styling. No fancy graphics -- just a legible dark
theme (default) and a light alternative, matching the "Dark" default
in Settings.
"""

DARK_QSS = """
QWidget {
    background-color: #14161a;
    color: #e6e6e6;
    font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
}

QLabel#PageTitle {
    font-size: 22px;
    font-weight: 600;
    color: #ffffff;
}

QLabel#SectionTitle {
    font-size: 15px;
    font-weight: 600;
    color: #ffffff;
    margin-top: 8px;
}

QLabel#Subtitle {
    color: #9aa0a6;
}

QFrame#StatCard {
    background-color: #1c1f24;
    border: 1px solid #2a2e35;
    border-radius: 8px;
}

QLabel#StatTitle {
    color: #9aa0a6;
    font-size: 12px;
}

QLabel#StatValue {
    color: #ffffff;
    font-size: 20px;
    font-weight: 600;
}

QLabel#StatValueWarn {
    color: #ff6b6b;
    font-size: 20px;
    font-weight: 600;
}

QLabel#StatValueGood {
    color: #3ddc84;
    font-size: 20px;
    font-weight: 600;
}

QLabel#TileGoal {
    color: #6b7076;
    font-size: 11px;
}

QLabel#TileGoalMet {
    color: #3ddc84;
    font-size: 11px;
}

QLabel#TileGoalMiss {
    color: #ff6b6b;
    font-size: 11px;
}

QFrame#ModuleRow {
    background-color: #1c1f24;
    border: 1px solid #2a2e35;
    border-radius: 8px;
}

QLabel#ModuleName {
    font-weight: 600;
    font-size: 14px;
    margin-left: 6px;
    margin-right: 10px;
}

QLabel#ModuleStatusOn {
    color: #3ddc84;
    font-weight: 600;
}

QLabel#ModuleStatusOff {
    color: #6b7076;
    font-weight: 600;
}

QLabel#ModuleDescription {
    color: #9aa0a6;
}

QFrame#SubtaskPanel {
    background-color: #16181c;
    border-radius: 6px;
}

QLabel#SubtaskDone {
    color: #3ddc84;
}

QLabel#SubtaskPending {
    color: #6b7076;
}

QPlainTextEdit#ConsoleOutput {
    background-color: #0e0f11;
    color: #c9d1d9;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 12px;
    border: 1px solid #2a2e35;
    border-radius: 6px;
}

QListWidget#Sidebar {
    background-color: #101215;
    border: none;
    outline: none;
    font-size: 14px;
    padding-top: 10px;
}

QListWidget#Sidebar::item {
    padding: 10px 18px;
    color: #c9d1d9;
}

QListWidget#Sidebar::item:selected {
    background-color: #2563eb;
    color: #ffffff;
    border-radius: 4px;
}

QPushButton {
    background-color: #2563eb;
    color: #ffffff;
    border: none;
    border-radius: 6px;
    padding: 6px 14px;
}

QPushButton:hover {
    background-color: #1d4ed8;
}

QPushButton:disabled {
    background-color: #3a3f47;
    color: #7a7f87;
}

QLineEdit, QTextEdit, QComboBox, QDoubleSpinBox {
    background-color: #1c1f24;
    border: 1px solid #2a2e35;
    border-radius: 6px;
    padding: 5px;
    color: #e6e6e6;
}

QProgressBar {
    background-color: #1c1f24;
    border: 1px solid #2a2e35;
    border-radius: 6px;
    text-align: center;
    color: #e6e6e6;
}

QProgressBar::chunk {
    background-color: #2563eb;
    border-radius: 6px;
}

/* --- Voice page ---------------------------------------------------- */

QFrame#WaveformMeter {
    background-color: #0e0f11;
    border: 1px solid #2a2e35;
    border-radius: 8px;
}

QTextEdit#TranscriptBox {
    background-color: #0e0f11;
    border: 1px solid #2a2e35;
    border-radius: 6px;
    color: #c9d1d9;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 13px;
    padding: 8px;
}
"""

LIGHT_QSS = """
QWidget {
    background-color: #f5f6f8;
    color: #1a1a1a;
    font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
}

QLabel#PageTitle {
    font-size: 22px;
    font-weight: 600;
    color: #0d0d0d;
}

QLabel#SectionTitle {
    font-size: 15px;
    font-weight: 600;
    color: #0d0d0d;
}

QLabel#Subtitle {
    color: #55595e;
}

QFrame#StatCard, QFrame#ModuleRow {
    background-color: #ffffff;
    border: 1px solid #dcdfe3;
    border-radius: 8px;
}

QLabel#StatTitle {
    color: #55595e;
    font-size: 12px;
}

QLabel#StatValue {
    color: #0d0d0d;
    font-size: 20px;
    font-weight: 600;
}

QLabel#StatValueWarn {
    color: #d92626;
    font-size: 20px;
    font-weight: 600;
}

QLabel#StatValueGood {
    color: #1a9e56;
    font-size: 20px;
    font-weight: 600;
}

QLabel#TileGoal {
    color: #8a8f96;
    font-size: 11px;
}

QLabel#TileGoalMet {
    color: #1a9e56;
    font-size: 11px;
}

QLabel#TileGoalMiss {
    color: #d92626;
    font-size: 11px;
}

QLabel#ModuleName {
    font-weight: 600;
    font-size: 14px;
}

QLabel#ModuleStatusOn {
    color: #1a9e56;
    font-weight: 600;
}

QLabel#ModuleStatusOff {
    color: #8a8f96;
    font-weight: 600;
}

QLabel#ModuleDescription {
    color: #55595e;
}

QFrame#SubtaskPanel {
    background-color: #f0f1f3;
    border-radius: 6px;
}

QLabel#SubtaskDone {
    color: #1a9e56;
}

QLabel#SubtaskPending {
    color: #8a8f96;
}

QPlainTextEdit#ConsoleOutput {
    background-color: #ffffff;
    color: #1a1a1a;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 12px;
    border: 1px solid #dcdfe3;
    border-radius: 6px;
}

QListWidget#Sidebar {
    background-color: #eceef1;
    border: none;
    outline: none;
    font-size: 14px;
    padding-top: 10px;
}

QListWidget#Sidebar::item {
    padding: 10px 18px;
    color: #1a1a1a;
}

QListWidget#Sidebar::item:selected {
    background-color: #2563eb;
    color: #ffffff;
    border-radius: 4px;
}

QPushButton {
    background-color: #2563eb;
    color: #ffffff;
    border: none;
    border-radius: 6px;
    padding: 6px 14px;
}

QPushButton:hover {
    background-color: #1d4ed8;
}

QLineEdit, QTextEdit, QComboBox, QDoubleSpinBox {
    background-color: #ffffff;
    border: 1px solid #dcdfe3;
    border-radius: 6px;
    padding: 5px;
}

QProgressBar {
    background-color: #ffffff;
    border: 1px solid #dcdfe3;
    border-radius: 6px;
    text-align: center;
}

QProgressBar::chunk {
    background-color: #2563eb;
    border-radius: 6px;
}

/* --- Voice page ---------------------------------------------------- */

QFrame#WaveformMeter {
    background-color: #f8f9fa;
    border: 1px solid #dcdfe3;
    border-radius: 8px;
}

QTextEdit#TranscriptBox {
    background-color: #ffffff;
    border: 1px solid #dcdfe3;
    border-radius: 6px;
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 13px;
    padding: 8px;
}
"""


def stylesheet_for(theme_name: str) -> str:
    return LIGHT_QSS if theme_name == "Light" else DARK_QSS

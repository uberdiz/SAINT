"""
ui/theme.py

Token-based theming. SAINT's visual identity (deep charcoal surfaces, blue
accent, Segoe UI) is the default; the palette, accent, font and density are
configurable in Settings > Appearance and applied live.
"""

from dataclasses import dataclass

from PySide6.QtGui import QColor


@dataclass
class Palette:
    bg: str
    sidebar: str
    surface: str
    surface2: str
    border: str
    text: str
    muted: str
    faint: str
    accent: str
    accent_hover: str
    accent_soft: str
    on_accent: str
    success: str
    warning: str
    danger: str
    info: str
    dark: bool


def _mix(c1: str, c2: str, t: float) -> str:
    a, b = QColor(c1), QColor(c2)
    return QColor(int(a.red() + (b.red() - a.red()) * t), int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t)).name()


def _readable_on(color: str) -> str:
    c = QColor(color)
    lum = 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()
    return "#0b0d10" if lum > 0.6 else "#ffffff"


def system_is_dark() -> bool:
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication
        return QGuiApplication.styleHints().colorScheme() != Qt.ColorScheme.Light
    except Exception:
        return True


def palette_for(theme: str = "Dark", accent: str = "#2563eb") -> Palette:
    dark = theme == "Dark" or (theme == "System" and system_is_dark())
    accent = QColor(accent).name() if QColor(accent).isValid() else "#2563eb"
    if dark:
        bg, surface = "#14161a", "#1c1f24"
        return Palette(
            bg=bg, sidebar="#101215", surface=surface, surface2="#23272e", border="#2a2e35",
            text="#e6e6e6", muted="#9aa0a6", faint="#6b7076",
            accent=accent, accent_hover=_mix(accent, "#ffffff", 0.12), accent_soft=_mix(surface, accent, 0.22),
            on_accent=_readable_on(accent),
            success="#3ddc84", warning="#fbbf24", danger="#f87171", info="#60a5fa", dark=True)
    bg, surface = "#f5f6f8", "#ffffff"
    return Palette(
        bg=bg, sidebar="#eceef1", surface=surface, surface2="#f0f1f3", border="#dcdfe3",
        text="#1a1a1a", muted="#55595e", faint="#8a8f96",
        accent=accent, accent_hover=_mix(accent, "#000000", 0.12), accent_soft=_mix(surface, accent, 0.14),
        on_accent=_readable_on(accent),
        success="#1a9e56", warning="#b7791f", danger="#d92626", info="#2563eb", dark=False)


# Colours for each assistant state (orb, pills). Resolved against the palette.
def state_color(state: str, p: Palette) -> str:
    return {
        "offline": p.faint,
        "idle": p.muted,
        "wake_listening": p.accent,
        "wake_detected": p.success,
        "command_listening": p.success,
        "listening": p.success,
        "processing": p.info,
        "executing": p.warning,
        "speaking": "#fb923c" if p.dark else "#c2410c",
        "error": p.danger,
    }.get(state, p.muted)


def build_stylesheet(theme="Dark", accent="#2563eb", font_family="Segoe UI", font_size=13,
                     compact=False) -> str:
    p = palette_for(theme, accent)
    pad = 4 if compact else 6
    fs = int(font_size)
    return f"""
QWidget {{
    background-color: {p.bg};
    color: {p.text};
    font-family: "{font_family}", "Segoe UI", Arial, sans-serif;
    font-size: {fs}px;
}}
QToolTip {{ background: {p.surface2}; color: {p.text}; border: 1px solid {p.border}; padding: 4px; }}

QLabel#PageTitle {{ font-size: {fs + 9}px; font-weight: 600; color: {p.text}; }}
QLabel#SectionTitle {{ font-size: {fs + 2}px; font-weight: 600; color: {p.text}; }}
QLabel#CardTitle {{ font-size: {fs - 1}px; font-weight: 600; color: {p.muted}; letter-spacing: 0.5px; }}
QLabel#Subtitle, QLabel#Muted {{ color: {p.muted}; }}
QLabel#Faint {{ color: {p.faint}; font-size: {fs - 1}px; }}
QLabel#StateLabel {{ font-size: {fs + 11}px; font-weight: 600; }}
QLabel#ErrorText {{ color: {p.danger}; }}
QLabel#OkText {{ color: {p.success}; }}
QLabel#WarnText {{ color: {p.warning}; }}

QFrame#Card, QFrame#StatCard, QFrame#ModuleRow {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 10px;
}}
QFrame#Card QLabel, QFrame#StatCard QLabel, QFrame#ModuleRow QLabel {{ background: transparent; }}
QFrame#Banner {{ background: {_mix(p.surface, p.danger, 0.18)}; border: 1px solid {p.danger}; border-radius: 8px; }}
QFrame#Banner QLabel {{ background: transparent; color: {p.text}; }}

QLabel#StatTitle {{ color: {p.muted}; font-size: {fs - 1}px; }}
QLabel#StatValue {{ color: {p.text}; font-size: {fs + 7}px; font-weight: 600; }}
QLabel#StatValueWarn {{ color: {p.danger}; font-size: {fs + 7}px; font-weight: 600; }}
QLabel#StatValueGood {{ color: {p.success}; font-size: {fs + 7}px; font-weight: 600; }}
QLabel#TileGoal {{ color: {p.faint}; font-size: {fs - 2}px; }}
QLabel#TileGoalMet {{ color: {p.success}; font-size: {fs - 2}px; }}
QLabel#TileGoalMiss {{ color: {p.danger}; font-size: {fs - 2}px; }}
QLabel#ModuleName {{ font-weight: 600; font-size: {fs + 1}px; margin-left: 6px; margin-right: 10px; }}
QLabel#ModuleStatusOn {{ color: {p.success}; font-weight: 600; }}
QLabel#ModuleStatusOff {{ color: {p.faint}; font-weight: 600; }}
QLabel#ModuleDescription {{ color: {p.muted}; }}
QFrame#SubtaskPanel {{ background-color: {p.surface2}; border-radius: 6px; }}
QLabel#SubtaskDone {{ color: {p.success}; }}
QLabel#SubtaskPending {{ color: {p.faint}; }}

QListWidget#Sidebar {{
    background-color: {p.sidebar};
    border: none;
    outline: none;
    font-size: {fs + 1}px;
    padding-top: 8px;
}}
QListWidget#Sidebar::item {{ padding: {pad + 4}px 14px; color: {p.muted}; border-radius: 8px; margin: 2px 8px; }}
QListWidget#Sidebar::item:hover {{ background-color: {p.surface2}; color: {p.text}; }}
QListWidget#Sidebar::item:selected {{ background-color: {p.accent_soft}; color: {p.text}; }}
QWidget#SidebarPanel {{ background-color: {p.sidebar}; }}
QWidget#SidebarPanel QLabel {{ background: transparent; }}

QListWidget#SettingsNav {{ background: transparent; border: none; outline: none; }}
QListWidget#SettingsNav::item {{ padding: {pad + 2}px 10px; border-radius: 6px; color: {p.muted}; }}
QListWidget#SettingsNav::item:selected {{ background: {p.accent_soft}; color: {p.text}; }}
QListWidget#SettingsNav::item:hover {{ background: {p.surface2}; }}

QPushButton {{
    background-color: {p.surface2};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 7px;
    padding: {pad}px 14px;
}}
QPushButton:hover {{ border-color: {p.accent}; }}
QPushButton:pressed {{ background-color: {p.accent_soft}; }}
QPushButton:disabled {{ color: {p.faint}; border-color: {p.border}; }}
QPushButton#Primary {{ background-color: {p.accent}; color: {p.on_accent}; border: none; font-weight: 600; }}
QPushButton#Primary:hover {{ background-color: {p.accent_hover}; }}
QPushButton#Danger {{ background-color: transparent; color: {p.danger}; border: 1px solid {p.danger}; }}
QPushButton#Danger:hover {{ background-color: {_mix(p.surface, p.danger, 0.2)}; }}
QPushButton#Ghost {{ background: transparent; border: none; color: {p.muted}; padding: 2px 6px; }}
QPushButton#Ghost:hover {{ color: {p.text}; }}
QPushButton#Swatch {{ border-radius: 12px; min-width: 24px; max-width: 24px; min-height: 24px; max-height: 24px; padding: 0; }}

QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QDoubleSpinBox, QSpinBox, QTimeEdit, QFontComboBox {{
    background-color: {p.surface2};
    border: 1px solid {p.border};
    border-radius: 7px;
    padding: {pad - 1}px {pad + 1}px;
    color: {p.text};
    selection-background-color: {p.accent};
    selection-color: {p.on_accent};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus {{
    border-color: {p.accent};
}}
QComboBox QAbstractItemView {{ background: {p.surface}; border: 1px solid {p.border}; selection-background-color: {p.accent_soft}; }}
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {p.faint}; background: {p.surface2}; }}
QCheckBox::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; }}
QSlider::groove:horizontal {{ height: 4px; background: {p.border}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {p.text}; width: 14px; height: 14px; margin: -6px 0; border-radius: 7px; }}
QGroupBox {{
    background-color: {p.surface};
    border: 1px solid {p.border};
    border-radius: 10px;
    margin-top: 14px;
    padding: {10 if compact else 14}px;
    font-weight: 600;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {p.muted}; }}
QGroupBox QLabel, QGroupBox QCheckBox {{ background: transparent; font-weight: normal; }}
QWidget#FormRow {{ background: transparent; }}

QProgressBar {{ background-color: {p.surface2}; border: none; border-radius: 4px; text-align: center; color: {p.text}; max-height: 8px; }}
QProgressBar::chunk {{ background-color: {p.accent}; border-radius: 4px; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p.border}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {p.faint}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {p.border}; border-radius: 4px; min-width: 30px; }}

QTableWidget, QTreeWidget, QListWidget {{
    background-color: {p.surface};
    alternate-background-color: {p.surface2};
    border: 1px solid {p.border};
    border-radius: 8px;
    gridline-color: {p.border};
}}
QHeaderView::section {{ background: {p.surface2}; color: {p.muted}; border: none; padding: 6px; font-weight: 600; }}
QTableWidget::item:selected, QListWidget::item:selected {{ background: {p.accent_soft}; color: {p.text}; }}

QPlainTextEdit#ConsoleOutput {{
    background-color: {"#0e0f11" if p.dark else "#ffffff"};
    color: {p.text};
    font-family: "Cascadia Mono", "Consolas", "Courier New", monospace;
    font-size: {fs - 1}px;
}}
QTextBrowser#ChatView {{ background-color: {p.surface}; border: none; }}
QMenu {{ background: {p.surface}; border: 1px solid {p.border}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {p.accent_soft}; }}
"""


# Backwards compatible helper used by older code.
def stylesheet_for(theme_name: str) -> str:
    try:
        from core.config import config
        a = config.get("appearance", {}) or {}
        return build_stylesheet(theme_name, a.get("accent", "#2563eb"), a.get("font_family", "Segoe UI"),
                                a.get("font_size", 13), a.get("compact", False))
    except Exception:
        return build_stylesheet(theme_name)


def current_palette() -> Palette:
    from core.config import config
    a = config.get("appearance", {}) or {}
    return palette_for(a.get("theme", config.get("theme", "Dark")), a.get("accent", "#2563eb"))

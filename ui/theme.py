"""
ui/theme.py

SAINT's design tokens and the one application stylesheet built from them.
Minimal and quiet: near-black surfaces, hairline borders, one accent (SAINT
orange by default), colour reserved for state. Theme, accent, font and density
come from Settings > Appearance and are applied live.
"""

from dataclasses import dataclass

from PySide6.QtGui import QColor, QPalette


@dataclass
class Palette:
    bg: str
    sidebar: str
    surface: str
    surface2: str
    raised: str
    border: str
    border_strong: str
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


def rgba(color: str, alpha: float) -> str:
    c = QColor(color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{int(alpha * 255)})"


def _readable_on(color: str) -> str:
    c = QColor(color)
    lum = 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()
    return "#0b0c0e" if lum > 0.55 else "#ffffff"


def system_is_dark() -> bool:
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication
        return QGuiApplication.styleHints().colorScheme() != Qt.ColorScheme.Light
    except Exception:
        return True


def palette_for(theme: str = "Dark", accent: str = "#feaa34") -> Palette:
    dark = theme == "Dark" or (theme == "System" and system_is_dark())
    accent = QColor(accent).name() if QColor(accent).isValid() else "#feaa34"
    if dark:
        surface = "#121317"
        return Palette(
            bg="#0a0b0d", sidebar="#0d0e11", surface=surface, surface2="#18191e", raised="#1e1f25",
            border="#1f2127", border_strong="#2b2d34", text="#eceef2", muted="#8a8f98", faint="#555a63",
            accent=accent, accent_hover=_mix(accent, "#ffffff", 0.14), accent_soft=_mix(surface, accent, 0.16),
            on_accent=_readable_on(accent),
            success="#4cc38a", warning="#f5b544", danger="#f06a6a", info="#7c9cff", dark=True)
    surface = "#ffffff"
    return Palette(
        bg="#f6f6f7", sidebar="#efeff1", surface=surface, surface2="#f3f3f5", raised="#ffffff",
        border="#e4e4e8", border_strong="#d4d4da", text="#131417", muted="#5e626b", faint="#9a9ea6",
        accent=accent, accent_hover=_mix(accent, "#000000", 0.12), accent_soft=_mix(surface, accent, 0.14),
        on_accent=_readable_on(accent),
        success="#16a34a", warning="#b7791f", danger="#dc2626", info="#3b5bdb", dark=False)


def state_color(state: str, p: Palette) -> str:
    """Colour of each assistant state (orb, halo, pills)."""
    d = p.dark
    return {
        "offline": p.faint,
        "idle": p.muted,
        "wake_listening": p.accent,
        "wake_detected": "#5eead4" if d else "#0d9488",
        "command_listening": "#5eead4" if d else "#0d9488",
        "listening": "#5eead4" if d else "#0d9488",
        "processing": "#8b9cff" if d else "#4f5bd5",
        "observing": "#8b9cff" if d else "#4f5bd5",
        "executing": "#c084fc" if d else "#9333ea",
        "speaking": "#ff8a5c" if d else "#e0561f",
        "error": p.danger,
    }.get(state, p.muted)


def state_word(state: str) -> str:
    """One-word status for pills (the full label is shown next to it)."""
    return {"offline": "Mic off", "idle": "Idle", "wake_listening": "Ready", "wake_detected": "Heard you",
            "command_listening": "Listening", "listening": "Listening", "processing": "Thinking",
            "observing": "Looking", "executing": "Working", "speaking": "Speaking", "error": "Error"}.get(state, state)


def qt_palette(p: Palette) -> QPalette:
    """Native-painted bits (menus, combo popups, selection) follow the theme too."""
    pal = QPalette()
    for role, color in ((QPalette.Window, p.bg), (QPalette.WindowText, p.text), (QPalette.Base, p.surface),
                        (QPalette.AlternateBase, p.surface2), (QPalette.Text, p.text),
                        (QPalette.Button, p.surface2), (QPalette.ButtonText, p.text),
                        (QPalette.Highlight, p.accent), (QPalette.HighlightedText, p.on_accent),
                        (QPalette.ToolTipBase, p.raised), (QPalette.ToolTipText, p.text),
                        (QPalette.PlaceholderText, p.faint), (QPalette.Link, p.accent)):
        pal.setColor(role, QColor(color))
    return pal


def build_stylesheet(theme="Dark", accent="#feaa34", font_family="Segoe UI", font_size=13,
                     compact=False) -> str:
    p = palette_for(theme, accent)
    pad = 4 if compact else 6
    fs = int(font_size)
    hover = rgba("#ffffff", 0.045) if p.dark else rgba("#000000", 0.045)
    return f"""
* {{ outline: none; }}
QWidget {{
    color: {p.text};
    font-family: "{font_family}", "Segoe UI Variable Text", "Segoe UI", Arial, sans-serif;
    font-size: {fs}px;
}}
QMainWindow, QDialog, QMessageBox, QInputDialog, QColorDialog {{ background-color: {p.bg}; }}
QWidget#Content {{ background-color: {p.bg}; }}
QToolTip {{ background: {p.raised}; color: {p.text}; border: 1px solid {p.border_strong}; padding: 5px 8px; border-radius: 6px; }}
QLabel {{ background: transparent; }}

/* ---------- type ---------- */
QLabel#PageTitle {{ font-size: {fs + 10}px; font-weight: 600; }}
QLabel#PageSubtitle, QLabel#Subtitle, QLabel#Muted {{ color: {p.muted}; }}
QLabel#SectionTitle {{ font-size: {fs + 2}px; font-weight: 600; }}
QLabel#CardTitle {{ font-size: {fs - 2}px; font-weight: 600; color: {p.faint}; }}
QLabel#Faint {{ color: {p.faint}; font-size: {fs - 1}px; }}
QLabel#StateLabel {{ font-size: {fs + 13}px; font-weight: 600; }}
QLabel#Display {{ font-size: {fs + 22}px; font-weight: 300; }}
QLabel#Wordmark {{ font-family: "Segoe UI Variable Display", "Segoe UI", sans-serif; font-size: {fs + 3}px; font-weight: 700; }}
QLabel#ErrorText {{ color: {p.danger}; }}
QLabel#OkText {{ color: {p.success}; }}
QLabel#WarnText {{ color: {p.warning}; }}
QLabel#Kbd {{
    background: {p.surface2}; border: 1px solid {p.border_strong}; border-bottom-width: 2px;
    border-radius: 5px; padding: 0px 5px; color: {p.muted}; font-size: {fs - 3}px;
    font-family: "Cascadia Mono", Consolas, monospace;
}}

/* ---------- chips ---------- */
QLabel#Chip {{ background: {p.surface2}; border: 1px solid {p.border}; border-radius: 9px; padding: 1px 8px; color: {p.muted}; font-size: {fs - 2}px; }}
QLabel#ChipAccent {{ background: {p.accent_soft}; border: 1px solid {rgba(p.accent, 0.35)}; border-radius: 9px; padding: 1px 8px; color: {p.accent}; font-size: {fs - 2}px; }}
QLabel#ChipOk {{ background: {rgba(p.success, 0.12)}; border: 1px solid {rgba(p.success, 0.3)}; border-radius: 9px; padding: 1px 8px; color: {p.success}; font-size: {fs - 2}px; }}
QLabel#ChipWarn {{ background: {rgba(p.warning, 0.12)}; border: 1px solid {rgba(p.warning, 0.3)}; border-radius: 9px; padding: 1px 8px; color: {p.warning}; font-size: {fs - 2}px; }}
QLabel#ChipErr {{ background: {rgba(p.danger, 0.12)}; border: 1px solid {rgba(p.danger, 0.3)}; border-radius: 9px; padding: 1px 8px; color: {p.danger}; font-size: {fs - 2}px; }}

/* ---------- surfaces ---------- */
QFrame#Card, QFrame#StatCard, QFrame#ModuleRow {{
    background-color: {p.surface}; border: 1px solid {p.border}; border-radius: 12px;
}}
QFrame#Row {{ background: transparent; border: none; border-bottom: 1px solid {p.border}; }}
QFrame#Row:hover {{ background: {hover}; }}
QFrame#Divider {{ background: {p.border}; border: none; max-height: 1px; min-height: 1px; }}
QFrame#Sidebar {{ background: {p.sidebar}; border: none; border-right: 1px solid {p.border}; }}
QFrame#NavPill {{ background: {p.surface2}; border: 1px solid {p.border_strong}; border-radius: 8px; }}
QFrame#Toast {{ background: {p.raised}; border: 1px solid {p.border_strong}; border-radius: 10px; }}
QFrame#PalettePanel {{ background: {p.surface}; border: 1px solid {p.border_strong}; border-radius: 14px; }}
QFrame#OverlayCard {{ background: {rgba(p.surface, 0.78)}; border: 1px solid {rgba("#ffffff" if p.dark else "#000000", 0.08)}; border-radius: 16px; }}
QFrame#SegmentBar {{ background: {p.surface2}; border: 1px solid {p.border}; border-radius: 9px; }}
QFrame#Banner {{ background: {rgba(p.danger, 0.12)}; border: 1px solid {rgba(p.danger, 0.4)}; border-radius: 10px; }}

/* ---------- navigation ---------- */
QPushButton#NavButton {{
    background: transparent; border: none; border-radius: 8px; color: {p.muted};
    text-align: left; padding: {pad + 1}px 10px; font-size: {fs}px;
}}
QPushButton#NavButton:hover {{ color: {p.text}; background: {hover}; }}
QPushButton#NavButton:checked {{ color: {p.text}; background: transparent; }}
QPushButton#SearchButton {{
    background: {p.surface}; border: 1px solid {p.border}; border-radius: 8px; color: {p.faint};
    text-align: left; padding: {pad + 1}px 10px;
}}
QPushButton#SearchButton:hover {{ border-color: {p.border_strong}; color: {p.muted}; }}
QListWidget#SettingsNav {{ background: transparent; border: none; }}
QListWidget#SettingsNav::item {{ padding: {pad + 2}px 10px; border-radius: 7px; color: {p.muted}; }}
QListWidget#SettingsNav::item:selected {{ background: {p.surface2}; color: {p.text}; }}
QListWidget#SettingsNav::item:hover {{ background: {hover}; color: {p.text}; }}

/* ---------- buttons ---------- */
QPushButton {{
    background-color: {p.surface2}; color: {p.text}; border: 1px solid {p.border_strong};
    border-radius: 8px; padding: {pad}px 14px;
}}
QPushButton:hover {{ background-color: {p.raised}; border-color: {p.faint}; }}
QPushButton:pressed {{ background-color: {p.surface}; }}
QPushButton:disabled {{ color: {p.faint}; border-color: {p.border}; background: {p.surface}; }}
QPushButton#Primary {{ background-color: {p.accent}; color: {p.on_accent}; border: 1px solid {p.accent}; font-weight: 600; }}
QPushButton#Primary:hover {{ background-color: {p.accent_hover}; border-color: {p.accent_hover}; }}
QPushButton#Danger {{ background-color: transparent; color: {p.danger}; border: 1px solid {rgba(p.danger, 0.5)}; }}
QPushButton#Danger:hover {{ background-color: {rgba(p.danger, 0.12)}; }}
QPushButton#Ghost {{ background: transparent; border: none; color: {p.muted}; padding: 3px 8px; }}
QPushButton#Ghost:hover {{ color: {p.text}; background: {hover}; }}
QPushButton#Swatch {{ border-radius: 12px; min-width: 24px; max-width: 24px; min-height: 24px; max-height: 24px; padding: 0; }}
QPushButton#Segment {{ background: transparent; border: 1px solid transparent; border-radius: 7px; padding: 3px 12px; color: {p.muted}; }}
QPushButton#Segment:hover {{ color: {p.text}; }}
QPushButton#Segment:checked {{ background: {p.raised}; border-color: {p.border_strong}; color: {p.text}; }}
QPushButton#SceneButton {{ text-align: left; padding: 8px 12px; border-radius: 10px; background: {p.surface2}; }}
QToolButton#IconButton {{ background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 5px; }}
QToolButton#IconButton:hover {{ background: {p.surface2}; border-color: {p.border}; }}
QToolButton#IconButton:checked {{ background: {p.accent_soft}; border-color: {rgba(p.accent, 0.35)}; }}
QToolButton#IconButton:disabled {{ background: transparent; }}
QToolButton#PlayButton {{ background: {p.text}; border: none; border-radius: 18px; padding: 7px; }}
QToolButton#PlayButton:hover {{ background: {p.accent}; }}

/* ---------- inputs ---------- */
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QDoubleSpinBox, QSpinBox, QTimeEdit, QFontComboBox {{
    background-color: {p.surface}; border: 1px solid {p.border_strong}; border-radius: 8px;
    padding: {pad - 1}px {pad + 2}px; color: {p.text};
    selection-background-color: {p.accent}; selection-color: {p.on_accent};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus {{
    border-color: {p.accent};
}}
QLineEdit#BigInput {{ font-size: {fs + 4}px; padding: 11px 14px; border-radius: 12px; background: {p.surface}; }}
QLineEdit#PaletteInput {{ font-size: {fs + 3}px; padding: 12px 14px; border: none; border-bottom: 1px solid {p.border}; border-radius: 0px; background: transparent; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {p.raised}; border: 1px solid {p.border_strong}; selection-background-color: {p.accent_soft}; selection-color: {p.text}; padding: 4px; }}
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid {p.border_strong}; background: {p.surface2}; }}
QCheckBox::indicator:hover {{ border-color: {p.faint}; }}
QCheckBox::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; }}
QSlider::groove:horizontal {{ height: 4px; background: {p.border_strong}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {p.text}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
QGroupBox {{
    background-color: {p.surface}; border: 1px solid {p.border}; border-radius: 12px;
    margin-top: 16px; padding: {10 if compact else 16}px; font-weight: 600;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 4px; color: {p.muted}; }}
QGroupBox QLabel, QGroupBox QCheckBox {{ font-weight: normal; }}
QWidget#FormRow {{ background: transparent; }}
QProgressBar {{ background-color: {p.surface2}; border: none; border-radius: 3px; max-height: 6px; color: transparent; }}
QProgressBar::chunk {{ background-color: {p.accent}; border-radius: 3px; }}

/* ---------- scrolling / lists ---------- */
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p.border_strong}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {p.faint}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {p.border_strong}; border-radius: 3px; min-width: 30px; }}
QTableWidget, QTreeWidget, QListWidget {{
    background-color: {p.surface}; alternate-background-color: {p.surface}; border: 1px solid {p.border};
    border-radius: 10px; gridline-color: transparent;
}}
QTableWidget::item, QListWidget::item {{ padding: 6px 4px; border-bottom: 1px solid {p.border}; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: {p.surface}; color: {p.faint}; border: none; border-bottom: 1px solid {p.border}; padding: 7px 6px; font-weight: 600; font-size: {fs - 2}px; }}
QTableWidget::item:selected, QListWidget::item:selected {{ background: {p.accent_soft}; color: {p.text}; }}
QListWidget#Flat {{ background: transparent; border: none; }}
QListWidget#PaletteList {{ background: transparent; border: none; padding: 6px; }}
QListWidget#PaletteList::item {{ padding: 8px 10px; border-radius: 8px; border: none; color: {p.text}; }}
QListWidget#PaletteList::item:selected {{ background: {p.surface2}; }}
QPlainTextEdit#ConsoleOutput {{
    background-color: {p.surface}; border: 1px solid {p.border}; border-radius: 12px; padding: 8px;
    font-family: "Cascadia Mono", Consolas, "Courier New", monospace; font-size: {fs - 1}px;
}}
QTextBrowser#ChatView {{ background-color: transparent; border: none; }}
QMenu {{ background: {p.raised}; border: 1px solid {p.border_strong}; padding: 5px; border-radius: 10px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {p.surface2}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 4px 6px; }}
"""


def current_palette() -> Palette:
    from core.config import config
    a = config.get("appearance", {}) or {}
    return palette_for(a.get("theme", config.get("theme", "Dark")), a.get("accent", "#feaa34"))

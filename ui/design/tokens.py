"""
ui/design/tokens.py

Design tokens. Components read these instead of hard-coding numbers, so density, spacing and
type can be redesigned in one place. All sizes are logical pixels (Qt scales them for DPI).
"""

from core.config import config

# ---- spacing (a 4 px grid) ---------------------------------------------------------------
SPACE_XXS = 2
SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 24
SPACE_XXL = 32

PAGE_MARGINS = (32, 24, 32, 24)          # left, top, right, bottom
SECTION_GAP = 16                         # between cards on a page
CARD_PADDING = (18, 16, 18, 16)
CARD_GAP = 10                            # between rows inside a card

# ---- shape ----------------------------------------------------------------------------------
RADIUS_SM = 6
RADIUS_MD = 10
RADIUS_LG = 14

# ---- type scale (added to the base font size from Settings › Appearance) ---------------------
TYPE = {"caption": -2, "small": -1, "body": 0, "title": 2, "heading": 6, "display": 14}

# ---- motion (ms) ------------------------------------------------------------------------------
FAST = 120
BASE = 200
SLOW = 320

# ---- status --------------------------------------------------------------------------------------
# A status word -> the palette colour it uses (ui/theme.Palette attribute).
STATUS_COLOR = {"ok": "success", "ready": "success", "connected": "success", "running": "accent",
                "busy": "accent", "warn": "warning", "attention": "warning", "error": "danger",
                "failed": "danger", "off": "faint", "idle": "muted", "info": "info"}

# Layout breakpoints (content width) for responsive components.
NARROW = 560
WIDE = 980


def compact() -> bool:
    return bool(config.get("appearance.compact", False))


def gap(size: int) -> int:
    """A spacing token adjusted for density (compact mode tightens everything by a third)."""
    return max(SPACE_XXS, int(round(size * (0.67 if compact() else 1.0))))


def status_color(status: str, palette) -> str:
    return getattr(palette, STATUS_COLOR.get((status or "").lower(), "muted"))

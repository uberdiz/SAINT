"""Round-trip check: install the built model ZIPs the way the app does, from local files.

Fails unless core.model_assets can extract both archives into the layout SAINT reads.
Run after prepare_model_assets.py (used by the release workflow).
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "build" / "release-assets"
os.environ["SAINT_DATA_DIR"] = tempfile.mkdtemp(prefix="saint-verify-")
os.environ["SAINT_MODEL_ASSET_BASE"] = ASSETS.as_uri()
sys.path.insert(0, str(ROOT))

from core import model_assets  # noqa: E402

result = model_assets.ensure_all()
print(result)
if not all(result.values()):
    raise SystemExit(f"model asset round-trip failed: {result}")

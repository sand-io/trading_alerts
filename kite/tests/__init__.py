"""Kite regression tests; support the application's script-style imports."""
from pathlib import Path
import sys

KITE_DIR = Path(__file__).resolve().parents[1]
if str(KITE_DIR) not in sys.path:
    sys.path.insert(0, str(KITE_DIR))

"""Pytest bootstrap: make the repo root importable so `from pipeline import ...`
works regardless of where pytest is invoked from.
"""
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

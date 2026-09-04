"""Pytest configuration and shared fixtures."""

import sys
from pathlib import Path

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

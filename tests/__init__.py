"""Make ``src/`` importable so the tests run from a checkout without installing.

Importing anything under ``tests`` executes this file, which is what both
``python -m unittest discover`` and ``pytest`` do.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DATA = Path(__file__).resolve().parent / "data"

"""Make the test directory importable so `from helpers import ...` works."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

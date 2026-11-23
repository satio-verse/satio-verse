"""让测试能 import tools/persona_ab.py (tools 不是包)."""
import sys
from pathlib import Path

TOOLS = str(Path(__file__).resolve().parents[1] / "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

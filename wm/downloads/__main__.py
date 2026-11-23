"""python -m wm.downloads serve | watch | status (见 daemon.py)."""
import sys

from .daemon import main

sys.exit(main())

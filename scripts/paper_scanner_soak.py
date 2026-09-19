"""Windows/Linux wrapper for the read-only Phase 6 PAPER scanner soak.

Issue #350. Observer only. Does not place, cancel, or sign venue orders.
Does not start discovery or pricing work.

Prefer:

    python -m sports_hedge.application.scanner_phase6 --duration-seconds 720
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "backend" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sports_hedge.application.scanner_phase6 import main

if __name__ == "__main__":
    raise SystemExit(main())

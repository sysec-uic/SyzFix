"""Canonical location of saved analyzer results.

Defaults to analysis/results/ next to this package (the syzfix repo checkout
under an editable install); set SYZFIX_RESULTS_DIR to point elsewhere.
"""

import os
from pathlib import Path

RESULTS_DIR = Path(os.environ.get(
    "SYZFIX_RESULTS_DIR", Path(__file__).resolve().parent / "results"))

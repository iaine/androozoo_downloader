"""AndroZoo downloader package.

Public API lives in :mod:`androzoo.core`; the command-line entry point is in
:mod:`androzoo.cli` and the desktop UI in :mod:`androzoo.ui.app`.
"""

from .core import Job, Result, Status, download_one, read_hashes, run

__version__ = "0.1.0"

__all__ = [
    "Job",
    "Result",
    "Status",
    "download_one",
    "read_hashes",
    "run",
    "__version__",
]

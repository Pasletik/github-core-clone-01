"""github-core-clone-01: a GitHub-compatible git forge in one Python process.

Standard library only. See README.md for the surfaces and the layout.
"""

from .store import Store
from .server import serve

__all__ = ["Store", "serve"]

"""Hermes XMPP platform plugin.

The plugin vendors its non-core dependencies in a ``deps`` subdirectory so it
can be installed independently of the Hermes gateway's Python environment.
"""

import site  # noqa: I001
from pathlib import Path  # noqa: I001

_DEPS_DIR = Path(__file__).resolve().parent / "deps"
if _DEPS_DIR.is_dir():
    site.addsitedir(str(_DEPS_DIR))

from .adapter import register  # noqa: I001, E402

__all__ = ["register"]

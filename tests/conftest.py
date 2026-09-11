"""Test bootstrap for the plugin repository.

The repository root is itself a Hermes plugin (``__init__.py`` + ``adapter.py``
at the root), so it must NOT be importable as a package during tests: a bare
``__init__.py`` on ``sys.path`` is imported as a top-level module named
``__init__`` and its relative imports collapse with
"attempted relative import with no known parent package".

Putting this conftest in ``tests/`` instead of relying on ``pythonpath = .``
keeps the repository root off pytest's import path while still letting the test
modules import the flat helper modules (``hermes_xmpp_plugin_common``) that sit
beside the plugin files.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

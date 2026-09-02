"""Common helpers for the Hermes XMPP plugin installer/uninstaller.

Canonical implementation lives in ``xmpp_plugin_source/hermes_xmpp_plugin_common.py``
(shipped with the plugin so core-route installs can share it). This root-level
module re-exports everything for the repo-root installer/uninstaller scripts and
their tests.

The vendored module is loaded DIRECTLY by file path (importlib) rather than
through the ``xmpp_plugin_source`` package, because importing that package
executes ``__init__.py`` -> ``adapter.py`` -> ``import httpx``, which would
make the installer/uninstaller require the plugin's runtime dependencies.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC_PATH = Path(__file__).resolve().parent / "xmpp_plugin_source" / "hermes_xmpp_plugin_common.py"
_SPEC = importlib.util.spec_from_file_location("hermes_xmpp_plugin_common_vendored", _SPEC_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover
    raise ImportError(f"Cannot load vendored common module from {_SPEC_PATH}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
# Register in sys.modules so repeated imports share one module object.
import sys  # noqa: E402

sys.modules.setdefault("hermes_xmpp_plugin_common_vendored", _MODULE)

# Re-export the full public surface (plus DEFAULT_HERMES_HOME).
globals().update({name: getattr(_MODULE, name) for name in dir(_MODULE) if not name.startswith("__")})

# Explicit re-exports so ruff's F822 check can see they exist on the module.
DEFAULT_HERMES_HOME = _MODULE.DEFAULT_HERMES_HOME
_env_text_changed = _MODULE._env_text_changed
_load_env_credentials = _MODULE._load_env_credentials
_upsert_env_line = _MODULE._upsert_env_line
_upsert_xmpp_allow_all_users = _MODULE._upsert_xmpp_allow_all_users
add_default_xmpp_config = _MODULE.add_default_xmpp_config
add_voice_and_stt_defaults = _MODULE.add_voice_and_stt_defaults
append_env_credentials = _MODULE.append_env_credentials
backup_file = _MODULE.backup_file
disable_plugin = _MODULE.disable_plugin
enable_plugin = _MODULE.enable_plugin
get_hermes_home = _MODULE.get_hermes_home
get_hermes_python = _MODULE.get_hermes_python
get_profile_dir = _MODULE.get_profile_dir
get_yaml_editor = _MODULE.get_yaml_editor
is_plugin_enabled = _MODULE.is_plugin_enabled
normalize_allowed_users = _MODULE.normalize_allowed_users
remove_xmpp_config = _MODULE.remove_xmpp_config

__all__ = [
    "DEFAULT_HERMES_HOME",
    "_env_text_changed",
    "_load_env_credentials",
    "_upsert_env_line",
    "_upsert_xmpp_allow_all_users",
    "add_default_xmpp_config",
    "add_voice_and_stt_defaults",
    "append_env_credentials",
    "backup_file",
    "disable_plugin",
    "enable_plugin",
    "get_hermes_home",
    "get_hermes_python",
    "get_profile_dir",
    "get_yaml_editor",
    "is_plugin_enabled",
    "normalize_allowed_users",
    "remove_xmpp_config",
]

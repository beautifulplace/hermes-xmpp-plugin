"""Common helpers for the Hermes XMPP plugin installer/uninstaller.

Canonical implementation lives in ``xmpp_plugin_source/hermes_xmpp_plugin_common.py``
(shipped with the plugin so core-route installs can share it). This root-level
module re-exports everything for the repo-root installer/uninstaller scripts and
their tests.
"""

from xmpp_plugin_source.hermes_xmpp_plugin_common import *  # noqa: F401,F403
from xmpp_plugin_source.hermes_xmpp_plugin_common import (  # noqa: F401
    _env_text_changed,
    _load_env_credentials,
    _upsert_env_line,
    _upsert_xmpp_allow_all_users,
    add_default_xmpp_config,
    add_voice_and_stt_defaults,
    append_env_credentials,
    backup_file,
    disable_plugin,
    enable_plugin,
    get_hermes_home,
    get_hermes_python,
    get_profile_dir,
    get_yaml_editor,
    is_plugin_enabled,
    normalize_allowed_users,
    remove_xmpp_config,
)

DEFAULT_HERMES_HOME = __import__(
    "xmpp_plugin_source.hermes_xmpp_plugin_common",
    fromlist=["DEFAULT_HERMES_HOME"],
).DEFAULT_HERMES_HOME

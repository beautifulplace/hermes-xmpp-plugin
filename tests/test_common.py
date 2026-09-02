import pathlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_xmpp_plugin_common import (
    add_default_xmpp_config,
    add_voice_and_stt_defaults,
    disable_plugin,
    enable_plugin,
    get_hermes_home,
    get_profile_dir,
)


def test_get_hermes_home_default(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    home = get_hermes_home(None)
    assert home == Path.home() / ".hermes"


def test_get_hermes_home_env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("HERMES_HOME", tmp)
        home = get_hermes_home(None)
        assert home == Path(tmp)


def test_get_profile_dir_default():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        profile_dir = get_profile_dir(home)
        assert profile_dir == home


def test_get_profile_dir_named():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        profile_dir = get_profile_dir(home, "work")
        assert profile_dir == home / "profiles" / "work"


def test_enable_plugin_creates_block():
    result = enable_plugin("")
    assert "plugins:" in result
    assert "- platforms/xmpp" in result


def test_disable_plugin_removes():
    config = "plugins:\n  enabled:\n    - platforms/xmpp\n"
    result = disable_plugin(config)
    assert "platforms/xmpp" not in result
    # Empty plugins block is removed to avoid leftover clutter.
    assert "plugins:" not in result


def test_disable_plugin_preserves_other_plugins():
    config = "plugins:\n  enabled:\n    - platforms/xmpp\n    - platforms/other\n"
    result = disable_plugin(config)
    assert "platforms/xmpp" not in result
    assert "- platforms/other" in result
    assert "plugins:" in result


def test_add_default_xmpp_config():
    result = add_default_xmpp_config("")
    assert "platforms:" in result
    assert "xmpp:" in result
    assert "omemo_enabled: true" in result


def test_add_default_xmpp_config_existing_platforms():
    config = "plugins:\n  enabled: []\nplatforms:\n  other_platform:\n    enabled: true\n"
    result = add_default_xmpp_config(config)
    assert "xmpp:" in result
    assert "other_platform:" in result


def test_add_voice_and_stt_defaults():
    result = add_voice_and_stt_defaults("")
    assert "voice:" in result
    assert "auto_tts: false" in result
    assert "tts:" in result
    assert "provider: edge" in result
    assert "stt:" in result
    assert "enabled: true" in result
    assert "provider: local" in result


def test_add_voice_and_stt_defaults_preserves_existing():
    config = "voice:\n  auto_tts: true\n"
    result = add_voice_and_stt_defaults(config)
    assert "auto_tts: true" in result
    assert "auto_tts: false" not in result


def test_add_voice_and_stt_defaults_fills_missing_provider_keys():
    """Fresh Hermes install may add stt/tts blocks without provider keys."""
    config = """tts:
  use_gateway: false
stt:
  enabled: true
  local:
    model: base
  openai:
    model: whisper-1
"""
    result = add_voice_and_stt_defaults(config)
    assert "stt.provider: local" not in result  # not dotted
    assert "tts:\n  provider: edge\n  use_gateway: false" in result
    assert "stt:\n  provider: local\n  enabled: true" in result
    assert "voice:\n  auto_tts: false" in result
    assert result.count("provider:") == 2


def test_normalize_allowed_users():
    from install_xmpp_plugin import normalize_allowed_users

    assert normalize_allowed_users("") == ""
    assert normalize_allowed_users("   ") == ""
    assert normalize_allowed_users("a@x.com") == "a@x.com"
    assert normalize_allowed_users(" a@x.com , b@y.net ,,c@z.org ") == "a@x.com,b@y.net,c@z.org"


def test_upsert_env_line_inserts_updates_dedupes():
    from hermes_xmpp_plugin_common import _upsert_env_line

    # Insert into empty file.
    lines, changed = _upsert_env_line([], "A", "1")
    assert lines == ['A="1"'] and changed

    # Update existing in place.
    lines, changed = _upsert_env_line(["X=1", 'A="old"', "Y=2"], "A", "new")
    assert lines == ["X=1", 'A="new"', "Y=2"] and changed

    # No-op when identical, including trailing whitespace.
    lines, changed = _upsert_env_line(['A="same"'], "A", "same")
    assert lines == ['A="same"'] and not changed
    lines, changed = _upsert_env_line(['  A="same"  '], "A", "same")
    assert lines == ['  A="same"  '] and not changed

    # Drop duplicate key lines, keep the first position.
    lines, changed = _upsert_env_line(['A="1"', 'B="2"', 'A="3"'], "A", "z")
    assert lines == ['A="z"', 'B="2"'] and changed

    # Spaced 'A = "v"' is canonicalized to KEY="value" form.
    lines, changed = _upsert_env_line(['A = "v"'], "A", "v")
    assert lines == ['A="v"'] and changed


def test_append_env_credentials_writes_allowed_users(tmp_path):
    """New install writes XMPP_ALLOWED_USERS alongside credentials."""
    import install_xmpp_plugin as inst

    env_path = tmp_path / ".env"
    inst.append_env_credentials(
        env_path, "bot@x.com", "pw", allowed_users="a@x.com,b@y.net"
    )
    text = env_path.read_text()
    assert 'XMPP_USER_JID="bot@x.com"' in text
    assert 'XMPP_PASSWORD="pw"' in text
    assert 'XMPP_ALLOWED_USERS="a@x.com,b@y.net"' in text


def test_append_env_credentials_updates_existing_allowed_users(tmp_path):
    """Reinstall with a changed list upserts in place; unchanged list rewrites nothing."""
    import install_xmpp_plugin as inst

    env_path = tmp_path / ".env"
    env_path.write_text(
        'XMPP_USER_JID="bot@x.com"\n'
        'XMPP_PASSWORD="pw"\n'
        'XMPP_ALLOWED_USERS="old@x.com"\n'
    )

    inst.append_env_credentials(
        env_path, "bot@x.com", "pw", allowed_users="new@x.com,other@y.net"
    )
    text = env_path.read_text()
    assert 'XMPP_ALLOWED_USERS="new@x.com,other@y.net"' in text
    assert "old@x.com" not in text
    assert text.count("XMPP_ALLOWED_USERS") == 1

    # Unchanged list: no rewrite at all.
    inst.append_env_credentials(
        env_path, "bot@x.com", "pw", allowed_users="new@x.com,other@y.net"
    )
    assert env_path.read_text() == text


def test_add_default_xmpp_config_allow_all_users():
    """allow_all_users=True writes allow_all_users: true in the default block."""
    result = add_default_xmpp_config("", allow_all_users=True)
    assert "allow_all_users: true" in result
    assert "allow_all_users: false" not in result


def test_add_default_xmpp_config_default_denies():
    """Default (no explicit opt-in) writes allow_all_users: false."""
    result = add_default_xmpp_config("")
    assert "allow_all_users: false" in result


def test_add_default_xmpp_config_upserts_existing_block():
    """An existing xmpp block gets allow_all_users upserted in place."""
    config = (
        "platforms:\n"
        "  xmpp:\n"
        "    enabled: false\n"
        "    omemo_enabled: true\n"
    )
    result = add_default_xmpp_config(config, allow_all_users=True)
    assert "enabled: true" in result
    assert "allow_all_users: true" in result
    assert "omemo_enabled: true" in result


def test_append_env_credentials_writes_allow_all(tmp_path):
    """allow_all_users=True writes XMPP_ALLOW_ALL_USERS=true."""
    import install_xmpp_plugin as inst

    env_path = tmp_path / ".env"
    inst.append_env_credentials(
        env_path, "bot@x.com", "pw", allow_all_users=True
    )
    text = env_path.read_text()
    assert 'XMPP_ALLOW_ALL_USERS="true"' in text


def test_append_env_credentials_allowlist_clears_stale_allow_all(tmp_path):
    """An explicit allowlist clears a stale allow-all flag from a prior install."""
    import install_xmpp_plugin as inst

    env_path = tmp_path / ".env"
    env_path.write_text('XMPP_ALLOW_ALL_USERS="true"\n')
    inst.append_env_credentials(
        env_path, "bot@x.com", "pw", allowed_users="a@x.com"
    )
    text = env_path.read_text()
    assert 'XMPP_ALLOWED_USERS="a@x.com"' in text
    assert 'XMPP_ALLOW_ALL_USERS="false"' in text


def test_append_env_credentials_no_allowlist_clears_stale_allow_all(tmp_path):
    """No allowlist and no allow-all clears a stale allow-all flag (deny-all)."""
    import install_xmpp_plugin as inst

    env_path = tmp_path / ".env"
    env_path.write_text('XMPP_ALLOW_ALL_USERS="true"\n')
    inst.append_env_credentials(env_path, "bot@x.com", "pw")
    text = env_path.read_text()
    assert 'XMPP_ALLOW_ALL_USERS="false"' in text


def test_append_env_credentials_seeds_home_channel_from_first_allowed(tmp_path):
    """Fresh install seeds XMPP_HOME_CHANNEL from the first allowed user."""
    import install_xmpp_plugin as inst

    env_path = tmp_path / ".env"
    inst.append_env_credentials(
        env_path, "bot@x.com", "pw", allowed_users="a@x.com,b@y.net",
        home_channel="a@x.com",
    )
    text = env_path.read_text()
    assert 'XMPP_HOME_CHANNEL="a@x.com"' in text


def test_append_env_credentials_existing_home_channel_wins(tmp_path):
    """An existing XMPP_HOME_CHANNEL (user- or /sethome-set) is never overwritten."""
    import install_xmpp_plugin as inst

    env_path = tmp_path / ".env"
    env_path.write_text(
        'XMPP_USER_JID="bot@x.com"\n'
        'XMPP_PASSWORD="pw"\n'
        'XMPP_HOME_CHANNEL="original@x.com"\n'
    )
    inst.append_env_credentials(
        env_path, "bot@x.com", "pw", allowed_users="a@x.com",
        home_channel="a@x.com",
    )
    text = env_path.read_text()
    assert 'XMPP_HOME_CHANNEL="original@x.com"' in text
    assert text.count("XMPP_HOME_CHANNEL") == 1


def test_append_env_credentials_no_allowlist_no_home_seed(tmp_path):
    """Empty allowlist -> no XMPP_HOME_CHANNEL seed."""
    import install_xmpp_plugin as inst

    env_path = tmp_path / ".env"
    inst.append_env_credentials(env_path, "bot@x.com", "pw", home_channel="")
    text = env_path.read_text()
    assert "XMPP_HOME_CHANNEL" not in text


def test_disable_plugin_luna_shape_duplicate_plugins_blocks():
    """Luna's config: stale empty flow-style block + installer block.

    disable_plugin must remove the item from the LAST (winning) block and
    drop stale empty-list blocks, not be fooled by the first empty block.
    """
    config = (
        "model:\n"
        "  default: glm-5.3-flash:cloud\n"
        "plugins:\n"
        "  enabled: []\n"
        "_config_version: 39\n"
        "plugins:\n"
        "  enabled:\n"
        "    - platforms/xmpp\n"
        "\n"
        "platforms:\n"
        "  xmpp:\n"
        "    enabled: true\n"
    )
    result = disable_plugin(config)
    from hermes_xmpp_plugin_common import is_plugin_enabled

    assert not is_plugin_enabled(result)
    assert "platforms/xmpp" not in result
    # The stale empty flow-style block is dropped too.
    assert result.count("plugins:") == 0


def test_enable_plugin_luna_shape_deduplicates_stale_block():
    """enable_plugin fills the last real block and removes a stale empty duplicate."""
    config = (
        "model:\n"
        "  default: glm\n"
        "plugins:\n"
        "  enabled: []\n"
        "_config_version: 39\n"
        "plugins:\n"
        "  enabled:\n"
        "    - other/plugin\n"
        "\n"
        "platforms:\n"
        "  other:\n"
        "    enabled: true\n"
    )
    result = enable_plugin(config)
    from hermes_xmpp_plugin_common import is_plugin_enabled

    assert is_plugin_enabled(result)
    assert result.count("plugins:") == 1
    # other/plugin preserved, xmpp appended after it.
    assert "other/plugin" in result and "platforms/xmpp" in result


def test_disable_plugin_preserves_nonempty_other_blocks():
    """Uninstall must not drop a plugins block that still lists other plugins."""
    config = (
        "plugins:\n"
        "  enabled: []\n"
        "plugins:\n"
        "  enabled:\n"
        "    - platforms/xmpp\n"
        "    - other/plugin\n"
    )
    result = disable_plugin(config)
    from hermes_xmpp_plugin_common import is_plugin_enabled

    assert not is_plugin_enabled(result)
    assert "other/plugin" in result
    assert "platforms/xmpp" not in result


def test_enable_disable_roundtrip_luna_shape():
    """Full cycle on the duplicate-block shape ends with one clean block.

    Mirrors the installer sequence: enable_plugin + add_default_xmpp_config,
    then the uninstaller sequence: disable_plugin + remove_xmpp_config.
    """
    config = (
        "model:\n"
        "  default: glm\n"
        "plugins:\n"
        "  enabled: []\n"
        "_config_version: 39\n"
        "platforms:\n"
        "  xmpp:\n"
        "    enabled: false\n"
    )
    from hermes_xmpp_plugin_common import (
        add_default_xmpp_config,
        is_plugin_enabled,
        remove_xmpp_config,
    )

    # Install sequence.
    enabled = enable_plugin(config)
    enabled = add_default_xmpp_config(enabled)
    assert enabled.count("plugins:") == 1
    assert is_plugin_enabled(enabled)
    assert "enabled: true" in enabled
    assert "allow_all_users: false" in enabled

    # Uninstall sequence.
    disabled = disable_plugin(enabled)
    disabled = remove_xmpp_config(disabled)
    assert not is_plugin_enabled(disabled)
    assert disabled.count("plugins:") == 0
    assert "xmpp" not in disabled


def test_root_common_shim_reexports_vendored_module():
    """Repo-root hermes_xmpp_plugin_common re-exports the vendored copy."""
    from hermes_xmpp_plugin_common_vendored import append_env_credentials as _v

    import hermes_xmpp_plugin_common as root

    # The shim loads the vendored file directly (no package __init__ -> no
    # adapter/httpx import chain) and re-exports the same function objects.
    assert root.append_env_credentials is _v
    for name in ("add_default_xmpp_config", "enable_plugin", "disable_plugin",
                 "normalize_allowed_users", "add_voice_and_stt_defaults",
                 "is_plugin_enabled", "remove_xmpp_config"):
        assert callable(getattr(root, name)), name


def test_post_install_enable_plugin_in_config(tmp_path):
    """post_install.enable_plugin_in_config enables + adds defaults idempotently."""
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "xmpp_plugin_source"))
    import post_install

    config = tmp_path / "config.yaml"
    config.write_text("plugins:\n  enabled: []\n")
    post_install.enable_plugin_in_config(config, add_defaults=True)
    text = config.read_text()
    assert "platforms/xmpp" in text
    assert "omemo_enabled: true" in text

    # Re-run: idempotent, no duplicate enable
    post_install.enable_plugin_in_config(config, add_defaults=True)
    text2 = config.read_text()
    assert text2.count("platforms/xmpp") == text.count("platforms/xmpp")


def test_post_install_seeds_home_and_allowlist(tmp_path):
    """post_install writes allowlist + home seed via the shared helper."""
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "xmpp_plugin_source"))
    from hermes_xmpp_plugin_common import append_env_credentials

    env = tmp_path / ".env"
    append_env_credentials(env, "bot@x.com", "pw",
                           allowed_users="a@x.com,b@y.net", home_channel="a@x.com")
    text = env.read_text()
    assert 'XMPP_ALLOWED_USERS="a@x.com,b@y.net"' in text
    assert 'XMPP_HOME_CHANNEL="a@x.com"' in text
    assert 'XMPP_USER_JID="bot@x.com"' in text

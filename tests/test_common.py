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
    from install_xmpp_plugin import _upsert_env_line

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

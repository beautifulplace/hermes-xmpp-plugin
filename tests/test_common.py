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


def test_enable_plugin_appends():
    config = "plugins:\n  enabled:\n    - foo\n"
    result = enable_plugin(config)
    assert "- platforms/xmpp" in result
    assert "- foo" in result


def test_enable_plugin_creates_block():
    result = enable_plugin("")
    assert "plugins:" in result
    assert "- platforms/xmpp" in result


def test_disable_plugin_removes():
    config = "plugins:\n  enabled:\n    - platforms/xmpp\n"
    result = disable_plugin(config)
    assert "platforms/xmpp" not in result
    assert "plugins:" in result


def test_add_default_xmpp_config():
    result = add_default_xmpp_config("")
    assert "platforms:" in result
    assert "xmpp:" in result
    assert "omemo_enabled: true" in result


def test_add_default_xmpp_config_existing_platforms():
    config = "plugins:\n  enabled: []\nplatforms:\n  mattermost:\n    enabled: true\n"
    result = add_default_xmpp_config(config)
    assert "xmpp:" in result
    assert "mattermost:" in result


def test_add_voice_and_stt_defaults():
    result = add_voice_and_stt_defaults("")
    assert "voice:" in result
    assert "auto_tts: true" in result
    assert "tts:" in result
    assert "provider: edge" in result
    assert "stt:" in result
    assert "enabled: true" in result
    assert "provider: local" in result


def test_add_voice_and_stt_defaults_preserves_existing():
    config = "voice:\n  auto_tts: false\n"
    result = add_voice_and_stt_defaults(config)
    assert "auto_tts: false" in result
    assert "auto_tts: true" not in result

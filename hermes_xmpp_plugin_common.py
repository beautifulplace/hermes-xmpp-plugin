"""Common helpers for the Hermes XMPP plugin installer/uninstaller.

These helpers deliberately avoid heavy dependencies like PyYAML/ruamel.yaml
so the scripts can run in a clean environment. They operate on the
plugins.enabled list in ~/.hermes/config.yaml with regex-based mutations that
preserve comments and formatting as much as possible.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Optional

DEFAULT_HERMES_HOME = Path.home() / ".hermes"


def get_hermes_home(cli_value: Optional[str] = None) -> Path:
    """Resolve the Hermes home directory.

    Priority:
    1. CLI --hermes-home argument
    2. $HERMES_HOME environment variable
    3. Default ~/.hermes
    """
    if cli_value:
        path = Path(cli_value).expanduser().resolve()
    elif os.environ.get("HERMES_HOME"):
        path = Path(os.environ["HERMES_HOME"]).expanduser().resolve()
    else:
        path = DEFAULT_HERMES_HOME

    return path


def get_profile_dir(hermes_home: Path, profile: Optional[str] = None) -> Path:
    """Return the active profile directory.

    If a profile name is provided, use it. Otherwise check $HERMES_PROFILE,
    then look for a sticky default in profiles/active_profile. Fall back to
    the base hermes_home.
    """
    if profile:
        return hermes_home / "profiles" / profile

    env_profile = os.environ.get("HERMES_PROFILE")
    if env_profile:
        return hermes_home / "profiles" / env_profile

    active_file = hermes_home / "profiles" / "active_profile"
    if active_file.exists():
        active_profile = active_file.read_text().strip()
        if active_profile:
            return hermes_home / "profiles" / active_profile

    return hermes_home


def get_hermes_python(hermes_home: Path, cli_python: Optional[str] = None) -> Path:
    """Find a suitable Python interpreter.

    Priority:
    1. CLI --python argument
    2. Hermes venv python: <home>/hermes-agent/venv/bin/python
    3. Hermes source venv: <home>/hermes-agent/.venv/bin/python
    4. Current interpreter
    """
    if cli_python:
        python = Path(cli_python).expanduser().resolve()
        if python.exists():
            return python
        raise FileNotFoundError(f"Specified python not found: {python}")

    candidates = [
        hermes_home / "hermes-agent" / "venv" / "bin" / "python",
        hermes_home / "hermes-agent" / ".venv" / "bin" / "python",
        Path.home() / ".local" / "share" / "hermes" / "venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()

    return Path(Path(os.__file__).parent).parent / "bin" / "python"


def backup_file(path: Path, suffix: str) -> Path:
    """Create a numbered backup of path."""
    backup_path = path.with_suffix(path.suffix + suffix)
    if backup_path.exists():
        for i in range(1, 100):
            numbered = path.with_suffix(f"{path.suffix}{suffix}.{i}")
            if not numbered.exists():
                backup_path = numbered
                break
    shutil.copy2(path, backup_path)
    return backup_path


def _find_block_bounds(text: str, key: str) -> tuple[int, int]:
    """Return the start/end line indices of a top-level YAML block."""
    pattern = re.compile(rf"^{re.escape(key)}:\s*", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return -1, -1

    start = text[: match.start()].count("\n")
    lines = text.splitlines()
    end = len(lines)

    parent_indent = len(lines[start]) - len(lines[start].lstrip())
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() == "":
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= parent_indent and not line.lstrip().startswith("#"):
            end = i
            break

    return start, end


def is_plugin_enabled(config_text: str) -> bool:
    """Return True if platforms/xmpp is already in plugins.enabled."""
    start, end = _find_block_bounds(config_text, "plugins")
    if start < 0:
        return False
    block = "\n".join(config_text.splitlines()[start:end])
    enabled_match = re.search(r"enabled:\s*\n((?:\s+-\s+.*\n?)+)", block)
    if not enabled_match:
        return False
    items = re.findall(r"-\s+(\S+)", enabled_match.group(1))
    return "platforms/xmpp" in items


def enable_plugin(config_text: str) -> str:
    """Add platforms/xmpp to plugins.enabled, creating the block if needed.

    If a platforms block exists, the plugins block is inserted immediately
    before it so the enabled-platforms section sits near the platform
    definitions. Otherwise it is appended to the end of the file.
    """
    if is_plugin_enabled(config_text):
        return config_text

    new_plugins_block = "plugins:\n  enabled:\n    - platforms/xmpp\n"

    if re.search(r"^platforms:\s*$", config_text, re.MULTILINE):
        # Insert plugins block right before platforms block.
        return re.sub(
            r"^(platforms:\s*)$",
            lambda m: new_plugins_block.rstrip() + "\n\n" + m.group(1),
            config_text,
            count=1,
            flags=re.MULTILINE,
        )

    if re.search(r"^plugins:\s*$", config_text, re.MULTILINE):
        # plugins block exists, ensure enabled list exists and append.
        def repl(match: re.Match) -> str:
            block = match.group(0)
            enabled_match = re.search(r"enabled:\s*\n((?:\s+-\s+.*\n?)+)", block)
            if enabled_match:
                # Append to existing list.
                list_body = enabled_match.group(1)
                indent_match = re.search(r"^(\s+)-", list_body, re.MULTILINE)
                indent = indent_match.group(1) if indent_match else "  "
                return block.replace(
                    list_body, list_body.rstrip() + f"\n{indent}- platforms/xmpp\n"
                )
            else:
                return block.rstrip() + "\n  enabled:\n    - platforms/xmpp\n"

        return re.sub(
            r"^plugins:\s*\n(?:  .+\n?)*",
            repl,
            config_text,
            count=1,
            flags=re.MULTILINE,
        )

    # Create plugins block at the end of file.
    return config_text.rstrip() + "\n\n" + new_plugins_block + "\n"


def disable_plugin(config_text: str) -> str:
    """Remove platforms/xmpp from plugins.enabled."""
    start, end = _find_block_bounds(config_text, "plugins")
    if start < 0:
        return config_text

    block = "\n".join(config_text.splitlines()[start:end])
    enabled_match = re.search(r"enabled:\s*\n((?:\s+-\s+.*\n?)+)", block)
    if not enabled_match:
        return config_text

    list_body = enabled_match.group(1)
    lines = list_body.splitlines()
    filtered = [line for line in lines if not re.match(r"^\s*-\s+platforms/xmpp\s*$", line)]
    if len(filtered) == len(lines):
        return config_text

    new_list = "\n".join(filtered) + "\n" if filtered else ""
    new_block = block.replace(enabled_match.group(1), new_list)

    lines = config_text.splitlines()
    lines[start:end] = new_block.splitlines()
    return "\n".join(lines) + "\n"


def add_default_xmpp_config(config_text: str, jid: str = "", password: str = "", avatar_path: str = "") -> str:
    """Add a default platforms.xmpp block if one does not exist."""
    if re.search(r"^platforms:\s*$", config_text, re.MULTILINE):
        # platforms block exists.
        start, end = _find_block_bounds(config_text, "platforms")
        block = "\n".join(config_text.splitlines()[start:end])
        if re.search(r"^\s+xmpp:\s*$", block, re.MULTILINE):
            return config_text

        default_xmpp = f"""\n  xmpp:
    enabled: true
    user_jid: "{jid}"
    password: "{password}"
    omemo_enabled: true
    omemo_allow_untrusted: true
    typing_indicator: true
    voice_reply: false
    voice_model: en-GB-SoniaNeural
    voice_format: m4a
    avatar_path: "{avatar_path}"
    home_channel: ""
    allow_all_users: false
"""
        return re.sub(
            r"^(platforms:\s*\n(?:  .+\n?)*)",
            lambda m: m.group(1).rstrip() + default_xmpp,
            config_text,
            count=1,
            flags=re.MULTILINE,
        )

    default_block = f"""platforms:
  xmpp:
    enabled: true
    user_jid: "{jid}"
    password: "{password}"
    omemo_enabled: true
    omemo_allow_untrusted: true
    typing_indicator: true
    voice_reply: false
    voice_model: en-GB-SoniaNeural
    voice_format: m4a
    avatar_path: "{avatar_path}"
    home_channel: ""
    allow_all_users: false
"""
    return config_text.rstrip() + "\n\n" + default_block + "\n"


def remove_xmpp_config(config_text: str) -> str:
    """Remove the platforms.xmpp block from config.yaml."""
    start, end = _find_block_bounds(config_text, "platforms")
    if start < 0:
        return config_text

    lines = config_text.splitlines()
    block = lines[start:end]
    new_block = []
    skip = False
    for line in block:
        stripped = line.lstrip()
        if stripped.startswith("xmpp:"):
            skip = True
            continue
        if skip:
            indent = len(line) - len(line.lstrip())
            if stripped and indent <= 2:
                skip = False
            else:
                continue
        new_block.append(line)

    lines[start:end] = new_block
    return "\n".join(lines) + "\n"

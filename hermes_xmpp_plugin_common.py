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
import sys
from pathlib import Path
from typing import Optional

DEFAULT_HERMES_HOME = Path.home() / ".hermes"


def get_yaml_editor():
    """Return a YAML loader/dumper and a flag indicating whether it is ruamel.yaml."""
    try:
        from ruamel.yaml import YAML
        yaml = YAML()
        yaml.preserve_quotes = True
        return yaml, True
    except ImportError:
        pass
    try:
        import yaml
        return yaml, False
    except ImportError:
        pass
    return None, False


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
    then look for a sticky default in ~/.hermes/active_profile. Fall back to
    the base hermes_home.
    """
    if profile:
        return hermes_home / "profiles" / profile

    env_profile = os.environ.get("HERMES_PROFILE")
    if env_profile:
        return hermes_home / "profiles" / env_profile

    active_file = hermes_home / "active_profile"
    if active_file.exists():
        active_profile = active_file.read_text().strip()
        if active_profile:
            return hermes_home / "profiles" / active_profile

    return hermes_home


def get_hermes_python(
    hermes_home: Path,
    cli_python: Optional[str] = None,
    fallback_home: Optional[Path] = None,
) -> Path:
    """Find a suitable Python interpreter.

    Priority:
    1. CLI --python argument
    2. Profile-local Hermes venv: <home>/hermes-agent/venv/bin/python
    3. Profile-local source venv: <home>/hermes-agent/.venv/bin/python
    4. Base/default Hermes venv: <fallback_home>/hermes-agent/venv/bin/python
    5. Current interpreter
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
    if fallback_home and fallback_home != hermes_home:
        candidates.extend([
            fallback_home / "hermes-agent" / "venv" / "bin" / "python",
            fallback_home / "hermes-agent" / ".venv" / "bin" / "python",
        ])

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()

    return Path(sys.executable)


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


def _iter_plugin_blocks(config_text: str) -> list[tuple[int, int, list[str]]]:
    """Return (start, end, block_lines) for every top-level 'plugins:' block.

    Configs touched by older installers can carry duplicate 'plugins:' blocks
    (e.g. an empty flow-style 'enabled: []' left by profile creation followed
    by the installer's block). Block-scoped logic must consider all of them
    because YAML's last-wins rule means later blocks shadow earlier ones.
    """
    lines = config_text.splitlines()
    blocks: list[tuple[int, int, list[str]]] = []
    pattern = re.compile(r"^plugins:\s*$")
    for i, line in enumerate(lines):
        if pattern.match(line):
            start = i
            end = len(lines)
            for j in range(start + 1, len(lines)):
                candidate = lines[j]
                if candidate.strip() == "":
                    continue
                indent = len(candidate) - len(candidate.lstrip())
                if indent == 0 and not candidate.lstrip().startswith("#"):
                    end = j
                    break
            blocks.append((start, end, lines[start:end]))
    return blocks


def _parse_enabled_items(block_lines: list[str]) -> list[str]:
    """Extract plugin names from a plugins block's enabled list.

    Handles both block style and flow style ('enabled: []', '[a, b]').
    Returns [] when the block has no enabled list or an empty one.
    """
    text = "\n".join(block_lines)
    enabled_match = re.search(r"enabled:\s*\n((?:\s+-\s+.*)+)", text)
    if enabled_match:
        return re.findall(r"-\s+(\S+)", enabled_match.group(1))
    flow_match = re.search(r"enabled:\s*\[([^\]]*)\]", text)
    if flow_match:
        return [
            item.strip().strip("\"'")
            for item in flow_match.group(1).split(",")
            if item.strip()
        ]
    return []


def _all_enabled_items(config_text: str) -> list[str]:
    """Union of enabled items across every plugins block."""
    items: list[str] = []
    for _start, _end, block_lines in _iter_plugin_blocks(config_text):
        items.extend(_parse_enabled_items(block_lines))
    return items


def is_plugin_enabled(config_text: str) -> bool:
    """Return True if platforms/xmpp is in plugins.enabled in any plugins block."""
    return "platforms/xmpp" in _all_enabled_items(config_text)


def enable_plugin(config_text: str) -> str:
    """Add platforms/xmpp to plugins.enabled, creating the block if needed.

    With duplicate 'plugins:' blocks (e.g. an empty one left by profile
    creation plus an installer-written one), YAML's last-wins rule applies,
    so the item is appended to the LAST block's enabled list and stale
    empty-list plugins blocks before it are dropped.
    """
    if is_plugin_enabled(config_text):
        return config_text

    blocks = _iter_plugin_blocks(config_text)
    if blocks:
        lines = config_text.splitlines()
        # Drop stale empty-list plugins blocks that precede the last one.
        for start, end, block_lines in reversed(blocks[:-1]):
            if not _parse_enabled_items(block_lines):
                del lines[start:end]

        text = "\n".join(lines)
        blocks = _iter_plugin_blocks(text)
        start, end, block_lines = blocks[-1]
        block = "\n".join(block_lines)
        items = _parse_enabled_items(block_lines)

        if re.search(r"^\s+enabled:\s*\n((?:\s+-\s+.*)+)", block):
            # Block-style list: append using the existing item indent.
            list_match = re.search(r"enabled:\s*\n((?:\s+-\s+.*)+)", block)
            if list_match is None:  # pragma: no cover - verified by the branch test
                raise AssertionError("unreachable: block-style list verified above")
            indent = list_match.group(1)[: -len(list_match.group(1).lstrip())] or "    "
            new_block = block.rstrip() + f"\n{indent}- platforms/xmpp"
            lines[start:end] = new_block.splitlines()
        elif items:
            # Non-empty flow-style list: convert to block style.
            rendered = "enabled:\n" + "\n".join(f"    - {item}" for item in items)
            new_block = re.sub(
                r"enabled:\s*\[[^\]]*\]", rendered, block, count=1
            ).rstrip() + "\n    - platforms/xmpp"
            lines[start:end] = new_block.splitlines()
        elif re.search(r"^\s+enabled:\s*\[\s*\]\s*$", block, re.MULTILINE):
            # Empty flow-style list: convert to block style with the new item.
            new_block = re.sub(
                r"enabled:\s*\[\s*\]",
                "enabled:\n    - platforms/xmpp",
                block,
                count=1,
                flags=re.MULTILINE,
            )
            lines[start:end] = new_block.splitlines()
        elif re.search(r"^\s+enabled:\s*$", block, re.MULTILINE):
            # Empty block-style list: fill it.
            new_block = block.rstrip() + "\n    - platforms/xmpp"
            lines[start:end] = new_block.splitlines()
        else:
            # plugins block without an enabled key: add one.
            lines[start:end] = block_lines + ["  enabled:", "    - platforms/xmpp"]

        result = "\n".join(lines)
        result = re.sub(r"\n\n\n+", "\n\n", result).rstrip("\n")
        return result + ("\n" if config_text.endswith("\n") else "\n")

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

    # Create plugins block at the end of file.
    return config_text.rstrip() + "\n\n" + new_plugins_block + "\n"


def disable_plugin(config_text: str) -> str:
    """Remove platforms/xmpp from plugins.enabled across ALL plugins blocks.

    A config may contain duplicate 'plugins:' blocks (e.g. an empty one left
    by profile creation); the item is removed wherever it appears. Blocks
    whose enabled list is empty afterwards are dropped, so a stale empty-list
    duplicate does not survive the uninstall. A config without
    platforms/xmpp enabled anywhere is returned unchanged.
    """
    blocks = _iter_plugin_blocks(config_text)
    if not blocks:
        return config_text

    lines = config_text.splitlines()
    removed = False
    # Process last-to-first so earlier indices stay valid after deletion.
    for start, end, block_lines in reversed(blocks):
        items = _parse_enabled_items(block_lines)
        if "platforms/xmpp" not in items:
            continue
        removed = True
        remaining = [
            line
            for line in block_lines
            if not re.match(r"^\s*-\s+platforms/xmpp\s*$", line)
        ]
        if _parse_enabled_items(remaining):
            lines[start:end] = remaining
        else:
            # Enabled list is now empty: drop the whole block.
            del lines[start:end]

    if not removed:
        return config_text

    # Drop any remaining plugins blocks with empty enabled lists
    # (stale duplicates that no longer carry anything).
    for start, end, block_lines in reversed(_iter_plugin_blocks("\n".join(lines))):
        if not _parse_enabled_items(block_lines) and re.search(
            r"^\s+enabled:", "\n".join(block_lines), re.MULTILINE
        ):
            del lines[start:end]

    result = "\n".join(lines)
    # Remove any double blank lines left by removing a block.
    result = re.sub(r"\n\n\n+", "\n\n", result)
    return result.rstrip() + "\n"


def add_default_xmpp_config(config_text: str, allow_all_users: bool = False) -> str:
    """Add a default platforms.xmpp block if one does not exist.

    Credentials are intentionally NOT written into config.yaml; they are stored
    in the Hermes .env file instead. ``allow_all_users`` reflects the user's
    explicit choice during install: True means the user opted to allow every
    sender (no allowlist), False means an allowlist is expected.
    """
    allow_all = "true" if allow_all_users else "false"
    if re.search(r"^platforms:\s*$", config_text, re.MULTILINE):
        # platforms block exists.
        start, end = _find_block_bounds(config_text, "platforms")
        block = "\n".join(config_text.splitlines()[start:end])
        if re.search(r"^\s+xmpp:\s*$", block, re.MULTILINE):
            # An xmpp block exists. If it was left disabled (profile default
            # "enabled: false" on fresh profiles), flip it on so the freshly
            # installed plugin actually starts, and upsert allow_all_users to
            # reflect the user's explicit choice.
            result = re.sub(
                r"^(\s+xmpp:\s*\n\s+enabled:\s*)false\s*$",
                r"\1true",
                config_text,
                count=1,
                flags=re.MULTILINE,
            )
            return _upsert_xmpp_allow_all_users(result, allow_all)

        default_xmpp = f"""\n  xmpp:
    enabled: true
    omemo_enabled: true
    omemo_allow_untrusted: true
    allow_all_users: {allow_all}
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
    omemo_enabled: true
    omemo_allow_untrusted: true
    allow_all_users: {allow_all}
"""
    return config_text.rstrip() + "\n\n" + default_block + "\n"


def _upsert_xmpp_allow_all_users(config_text: str, allow_all: str) -> str:
    """Set allow_all_users within an existing platforms.xmpp block.

    Replaces the value in place when the key is present, otherwise appends it
    after the last key in the xmpp block. Returns config_text unchanged when
    no xmpp block is found.
    """
    match = re.search(r"^(\s*)xmpp:\s*$", config_text, re.MULTILINE)
    if not match:
        return config_text
    indent = match.group(1)
    lines = config_text.splitlines()
    start = config_text[: match.start()].count("\n")
    xmpp_indent = len(indent)
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() == "":
            continue
        if len(line) - len(line.lstrip()) <= xmpp_indent:
            end = i
            break
    block = "\n".join(lines[start:end])
    if re.search(rf"^{indent}\s+allow_all_users:", block, re.MULTILINE):
        block = re.sub(
            rf"^({indent}\s+allow_all_users:\s*).*$",
            lambda m: m.group(1) + allow_all,
            block,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        block = block.rstrip() + f"\n{indent}  allow_all_users: {allow_all}"
    lines[start:end] = block.splitlines()
    return "\n".join(lines)







def add_voice_and_stt_defaults(config_text: str) -> str:
    '''Ensure voice/TTS/STT defaults required for XMPP voice replies exist.

    Adds missing top-level blocks and fills in missing provider/auto_tts keys
    when the block already exists. Existing user settings are preserved.
    '''
    config_text = _ensure_block(config_text, "stt", "enabled: true\n  provider: local\n  local:\n    model: tiny")
    config_text = _ensure_key_in_block(config_text, "stt", "provider", "local")

    config_text = _ensure_block(config_text, "tts", "provider: edge\n  use_gateway: false")
    config_text = _ensure_key_in_block(config_text, "tts", "provider", "edge")

    config_text = _ensure_block(config_text, "voice", "auto_tts: false")
    config_text = _ensure_key_in_block(config_text, "voice", "auto_tts", "false")

    return config_text


def _ensure_block(config_text: str, key: str, body: str) -> str:
    """Add a top-level YAML block if it does not exist."""
    if not re.search(rf"^{re.escape(key)}:\s*$", config_text, re.MULTILINE):
        config_text = config_text.rstrip() + f"\n\n{key}:\n  {body}\n"
    return config_text


def _ensure_key_in_block(config_text: str, key: str, option: str, value: str) -> str:
    """Add a key/value under a top-level block if the key is missing."""
    pattern = re.compile(rf"^{re.escape(key)}:\s*$", re.MULTILINE)
    match = pattern.search(config_text)
    if not match:
        return config_text

    lines = config_text.splitlines()
    start = config_text[:match.start()].count("\n")
    key_indent = len(lines[start]) - len(lines[start].lstrip())

    # Scan the block to see whether the option already exists.
    has_option = False
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() == "":
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= key_indent and not line.lstrip().startswith("#"):
            break
        stripped = line.lstrip()
        if stripped.startswith(f"{option}:"):
            has_option = True
            break

    if has_option:
        return config_text

    # Insert the option after the key line, preserving existing body.
    lines.insert(start + 1, f"{' ' * (key_indent + 2)}{option}: {value}")
    return "\n".join(lines) + "\n"

def remove_xmpp_config(config_text: str) -> str:
    """Remove the platforms.xmpp block from config.yaml.

    If the platforms block is left empty, remove it entirely.
    """
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

    # If platforms block only contains the "platforms:" line now, remove it.
    non_comment = [line for line in new_block if line.strip() and not line.lstrip().startswith("#")]
    if non_comment == ["platforms:"]:
        lines[start:end] = []
    else:
        lines[start:end] = new_block

    result = "\n".join(lines)
    result = re.sub(r"\n\n\n+", "\n\n", result)
    return result.rstrip() + "\n"

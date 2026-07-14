#!/usr/bin/env python3
"""Install the Hermes XMPP platform plugin.

Copies the plugin source into the active Hermes profile, enables it in
config.yaml, and ensures Python dependencies are installed in the Hermes
virtual environment.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn, Optional

from hermes_xmpp_plugin_common import (
    add_default_xmpp_config,
    backup_file,
    enable_plugin,
    get_hermes_home,
    get_hermes_python,
    get_profile_dir,
)

REQUIRED_PLUGIN_FILES = {
    "__init__.py",
    "adapter.py",
    "omemo_plugin.py",
    "plugin.yaml",
    "README.md",
}

DEPENDENCIES: list[tuple[str, str, bool]] = [
    # (pip package, python import name, required)
    ("slixmpp", "slixmpp", True),
    ("httpx", "httpx", True),
    ("Pillow", "PIL", True),
    ("cryptography", "cryptography", True),
    ("slixmpp-omemo", "slixmpp_omemo", True),
]

def fail(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def copy_plugin(plugin_src: Path, plugin_dest: Path, force: bool) -> None:
    if not plugin_src.exists():
        fail(f"Plugin source directory not found: {plugin_src}")

    missing = REQUIRED_PLUGIN_FILES - {p.name for p in plugin_src.iterdir() if p.is_file()}
    if missing:
        fail(f"Plugin source is missing required files: {sorted(missing)}")

    if plugin_dest.exists():
        if not force:
            fail(
                f"Plugin already installed at {plugin_dest}. "
                "Use --force to overwrite, or run uninstall first."
            )
        print(f"Removing existing plugin at {plugin_dest}")
        shutil.rmtree(plugin_dest)

    print(f"Installing plugin to {plugin_dest}")
    shutil.copytree(plugin_src, plugin_dest)


def install_dependencies(
    python: Path,
    plugin_dest: Path,
) -> None:
    """Ensure plugin dependencies are importable by the gateway.

    First checks whether each dependency is already available in the gateway's
    Python environment. Any missing packages are installed into a ``deps``
    subdirectory under the plugin so we do not modify externally-managed Python
    installations (uv, system PEP-668, etc.).
    """
    deps_dir = plugin_dest / "deps"
    deps_dir.mkdir(parents=True, exist_ok=True)

    def _python_env() -> dict[str, str]:
        """Return an environment that includes the plugin deps dir on PYTHONPATH."""
        env = os.environ.copy()
        pythonpath = [str(deps_dir)]
        if env.get("PYTHONPATH"):
            pythonpath.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(pythonpath))
        return env

    to_install = []
    for pip_name, import_name, required in DEPENDENCIES:
        try:
            subprocess.run(
                [str(python), "-c", f"import {import_name}"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"  {pip_name}: already installed")
        except subprocess.CalledProcessError:
            to_install.append(pip_name)

    if to_install:
        print(
            f"Installing missing dependencies into {deps_dir} with {python}: "
            f"{', '.join(to_install)}"
        )
        subprocess.run(
            [
                str(python), "-m", "pip", "install",
                "--target", str(deps_dir),
                "--upgrade",
                *to_install,
            ],
            check=True,
        )
    else:
        print("All dependencies are satisfied.")


def enable_plugin_in_config(
    config_path: Path,
    add_defaults: bool,
    avatar_path: str = "",
) -> None:
    if not config_path.exists():
        print(f"Config not found at {config_path}; creating minimal config")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_text = ""
    else:
        config_text = config_path.read_text()

    config_text = enable_plugin(config_text)
    if add_defaults:
        config_text = add_default_xmpp_config(
            config_text, avatar_path=avatar_path
        )

    config_path.write_text(config_text)


# Kept for backward compatibility if a caller passes the old keyword arguments,
# but the installer no longer uses them.
def _ensure_stt_config(config_text: str, model: str) -> str:
    return config_text


def _set_config_value(
    config_text: str,
    top_key: str,
    sub_key: str,
    option: str,
    value: str,
) -> str:
    """Set a scalar value inside a nested block using regex (no YAML lib required)."""
    lines = config_text.splitlines()
    in_top = False
    in_sub = False
    sub_indent = -1
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not in_top:
            if re.match(rf"^{top_key}:\s*$", stripped):
                in_top = True
            continue
        # Inside top block; detect sub_key at top+2 indent.
        if in_sub:
            # End of sub-block if indent drops below sub_indent.
            if stripped and line.startswith(" ") and len(line) - len(stripped) < sub_indent:
                in_sub = False
                continue
            # Match option line at sub_indent + 2 spaces.
            opt_match = re.match(rf"^ {{{sub_indent + 2}}}{option}:\s*(.*)$", line)
            if opt_match:
                lines[i] = f"{' ' * (sub_indent + 2)}{option}: {value}"
                return "\n".join(lines) + "\n"
        else:
            match = re.match(rf"^  {sub_key}:\s*$", stripped)
            if match:
                in_sub = True
                sub_indent = len(line) - len(stripped)
    # Not found: append to sub_key block if it exists, else append whole block.
    return config_text.rstrip() + "\n"


def prompt_xmpp_credentials(
    args: argparse.Namespace,
    env_path: Path,
) -> tuple[str, str, str]:
    """Return (jid, password, avatar_path), prompting for missing values.

    If the Hermes .env file already contains XMPP_USER_JID or XMPP_PASSWORD,
    those values are shown as defaults; the user can press Enter to keep them.
    """
    defaults = _load_env_credentials(env_path)

    print("\nXMPP account setup")
    print("-" * 40)

    default_jid = args.jid or defaults.get("XMPP_USER_JID", "")
    if default_jid:
        prompt = f"XMPP JID [{default_jid}]: "
    else:
        prompt = "XMPP JID (e.g. hermes@example.com): "
    jid = input(prompt).strip()
    if not jid:
        jid = default_jid
    while not jid:
        print("JID is required.")
        jid = input("XMPP JID (e.g. hermes@example.com): ").strip()

    if args.password:
        password = args.password
    else:
        default_password = defaults.get("XMPP_PASSWORD", "")
        if default_password:
            prompt = "XMPP password [press Enter to keep existing]: "
        else:
            prompt = "XMPP password: "
        password = getpass.getpass(prompt)
        if not password:
            password = default_password
        while not password:
            print("Password is required.")
            password = getpass.getpass("XMPP password: ")

    avatar_path = args.avatar_path or ""
    if not avatar_path:
        print(
            "\nOptional avatar image. Recommended: a square PNG or JPEG, "
            "at least 480x480 pixels. The plugin will crop to a centered "
            "square and resize to 480x480."
        )
        avatar_path = input("Avatar file path (leave blank for none): ").strip()

    return jid, password, avatar_path


def append_env_credentials(
    env_path: Path,
    jid: str,
    password: str,
) -> None:
    """Append credentials to the Hermes .env file if not already present.

    Stores XMPP_JID and XMPP_PASSWORD. Never writes secrets to config.yaml.
    """
    lines: list[str] = []
    if env_path.exists():
        text = env_path.read_text()
        lines = text.splitlines()
        if not text.endswith("\n"):
            lines.append("")

    existing_keys = {line.split("=", 1)[0].strip() for line in lines if "=" in line}
    additions: list[str] = []
    if "XMPP_USER_JID" not in existing_keys:
        additions.append(f'XMPP_USER_JID="{jid}"')
    if "XMPP_PASSWORD" not in existing_keys:
        additions.append(f'XMPP_PASSWORD="{password}"')

    if not additions:
        return

    if env_path.exists():
        env_path.write_text("\n".join(lines + additions) + "\n")
    else:
        env_path.write_text("\n".join(additions) + "\n")
    print(f"Appended credentials to {env_path}")


def _load_env_credentials(env_path: Path) -> dict[str, str]:
    """Load existing credentials from the Hermes .env file."""
    if not env_path.exists():
        return {}
    result: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in ("XMPP_USER_JID", "XMPP_PASSWORD"):
            result[key] = value.strip().strip('"\'')
    return result


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install the Hermes XMPP platform plugin."
    )
    parser.add_argument(
        "--hermes-home",
        metavar="DIR",
        help="Hermes home directory (default: $HERMES_HOME or ~/.hermes)",
    )
    parser.add_argument(
        "--profile",
        metavar="NAME",
        help="Hermes profile to target (default: active profile or default)",
    )
    parser.add_argument(
        "--plugin-src",
        metavar="DIR",
        default=None,
        help="Directory containing the plugin source (default: xmpp_plugin_source next to this script)",
    )
    parser.add_argument(
        "--python",
        metavar="PATH",
        help="Python interpreter to use for dependency installs (default: Hermes venv python)",
    )
    parser.add_argument(
        "--no-defaults",
        action="store_true",
        help="Do not add a default platforms.xmpp block to config.yaml",
    )
    parser.add_argument(
        "--jid",
        metavar="JID",
        help="XMPP JID (e.g. hermes@example.com). If omitted, you will be prompted unless --no-defaults is set.",
    )
    parser.add_argument(
        "--password",
        metavar="PASSWORD",
        help="XMPP password. If omitted, you will be prompted securely unless --no-defaults is set.",
    )
    parser.add_argument(
        "--avatar-path",
        metavar="PATH",
        help="Path to an avatar image. If omitted, you will be prompted.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing plugin installation",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip interactive prompts; requires --jid and --password if not using --no-defaults",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        hermes_home = get_hermes_home(args.hermes_home)
        profile_dir = get_profile_dir(hermes_home, args.profile)
    except FileNotFoundError as exc:
        fail(str(exc))

    plugin_dest = profile_dir / "plugins" / "platforms" / "xmpp"
    config_path = profile_dir / "config.yaml"
    plugin_src = (
        Path(args.plugin_src).expanduser().resolve()
        if args.plugin_src
        else Path(__file__).resolve().parent / "xmpp_plugin_source"
    )

    try:
        python = get_hermes_python(profile_dir, args.python)
    except FileNotFoundError as exc:
        fail(str(exc))

    print("=" * 60)
    print("Hermes XMPP Platform Plugin Installer")
    print("=" * 60)
    print(f"Hermes home:      {hermes_home}")
    print(f"Profile dir:      {profile_dir}")
    print(f"Plugin source:    {plugin_src}")
    print(f"Plugin destination: {plugin_dest}")
    print(f"Python interpreter: {python}")

    env_path = profile_dir / ".env"

    jid = ""
    password = ""
    avatar_path = ""
    if not args.no_defaults:
        if args.non_interactive:
            if not args.jid or not args.password:
                fail("--non-interactive requires --jid and --password")
            jid = args.jid
            password = args.password
            avatar_path = args.avatar_path or ""
        else:
            jid, password, avatar_path = prompt_xmpp_credentials(args, env_path)

    copy_plugin(plugin_src, plugin_dest, force=args.force)
    install_dependencies(
        python,
        plugin_dest,
    )

    if config_path.exists():
        backup_path = backup_file(config_path, ".install-backup")
        print(f"Backed up config to {backup_path}")

    enable_plugin_in_config(
        config_path,
        add_defaults=not args.no_defaults,
        avatar_path=avatar_path,
    )

    if not args.no_defaults and jid and password:
        append_env_credentials(env_path, jid, password)
        print("  XMPP credentials stored in .env (not config.yaml).")

    print("\nInstallation complete.")
    print("Configure STT/TTS in Hermes core config.yaml:")
    print("  stt:")
    print("    provider: local")
    print("    local:")
    print("      model: medium")
    print("  voice:")
    print("    auto_tts: true")
    print("  tts:")
    print("    provider: edge")
    print("Restart the Hermes gateway to load the plugin:")
    print("  hermes gateway restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())

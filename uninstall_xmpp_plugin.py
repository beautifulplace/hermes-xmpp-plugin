#!/usr/bin/env python3
"""Uninstall the Hermes XMPP platform plugin.

Removes the plugin from the active Hermes profile and disables it in
config.yaml. A backup of the config is created before editing.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Optional

from hermes_xmpp_plugin_common import (
    backup_file,
    disable_plugin,
    get_hermes_home,
    get_profile_dir,
    remove_xmpp_config,
)


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def remove_plugin(plugin_dest: Path) -> None:
    if not plugin_dest.exists():
        print(f"Plugin not installed at {plugin_dest}")
        return
    print(f"Removing plugin at {plugin_dest}")
    shutil.rmtree(plugin_dest)


def disable_plugin_in_config(config_path: Path, keep_config: bool, interactive: bool) -> None:
    if not config_path.exists():
        print(f"Config not found at {config_path}; skipping config update")
        return

    config_text = config_path.read_text()
    config_text = disable_plugin(config_text)

    remove_config = not keep_config
    if interactive and not keep_config:
        answer = input("Remove the platforms.xmpp block from config.yaml? [Y/n]: ").strip().lower()
        remove_config = answer in ("", "y", "yes")

    if remove_config:
        config_text = remove_xmpp_config(config_text)
    else:
        print("Keeping platforms.xmpp block in config.yaml")

    config_path.write_text(config_text)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Uninstall the Hermes XMPP platform plugin."
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
        "--keep-config",
        action="store_true",
        help="Keep the platforms.xmpp block in config.yaml without prompting",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip interactive prompts; implies removing platforms.xmpp config",
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

    print("=" * 60)
    print("Hermes XMPP Platform Plugin Uninstaller")
    print("=" * 60)
    print(f"Hermes home:   {hermes_home}")
    print(f"Profile dir:   {profile_dir}")

    remove_plugin(plugin_dest)

    if config_path.exists():
        backup_path = backup_file(config_path, ".uninstall-backup")
        print(f"Backed up config to {backup_path}")
        disable_plugin_in_config(
            config_path,
            keep_config=args.keep_config,
            interactive=not args.non_interactive,
        )

    print("\nUninstall complete.")
    print("Restart the Hermes gateway for changes to take effect:")
    print("  hermes gateway restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())

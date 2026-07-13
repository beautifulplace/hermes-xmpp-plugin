#!/usr/bin/env python3
"""Install the Hermes XMPP platform plugin.

Copies the plugin source into the active Hermes profile, enables it in
config.yaml, and ensures Python dependencies are installed in the Hermes
virtual environment.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

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
    ("slixmpp-omemo", "slixmpp_omemo", False),
    ("edge-tts", "edge_tts", False),
]


def fail(message: str) -> None:
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


def install_dependencies(python: Path, plugin_dest: Path, only_required: bool) -> None:
    """Ensure plugin dependencies are importable by the gateway.

    First checks whether each dependency is already available in the gateway's
    Python environment. Any missing packages are installed into a ``deps``
    subdirectory under the plugin so we do not modify externally-managed Python
    installations (uv, system PEP-668, etc.).
    """
    deps_dir = plugin_dest / "deps"
    deps_dir.mkdir(parents=True, exist_ok=True)

    to_install = []
    for pip_name, import_name, required in DEPENDENCIES:
        if only_required and not required:
            continue
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


def enable_plugin_in_config(config_path: Path, add_defaults: bool) -> None:
    if not config_path.exists():
        print(f"Config not found at {config_path}; creating minimal config")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_text = ""
    else:
        config_text = config_path.read_text()

    config_text = enable_plugin(config_text)
    if add_defaults:
        config_text = add_default_xmpp_config(config_text)

    config_path.write_text(config_text)


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
        "--only-required-deps",
        action="store_true",
        help="Install only required dependencies; skip optional ones (OMEMO, voice)",
    )
    parser.add_argument(
        "--no-defaults",
        action="store_true",
        help="Do not add a default platforms.xmpp block to config.yaml",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing plugin installation",
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

    copy_plugin(plugin_src, plugin_dest, force=args.force)
    install_dependencies(python, plugin_dest, only_required=args.only_required_deps)

    if config_path.exists():
        backup_path = backup_file(config_path, ".install-backup")
        print(f"Backed up config to {backup_path}")

    enable_plugin_in_config(config_path, add_defaults=not args.no_defaults)

    print("\nInstallation complete.")
    print("Restart the Hermes gateway to load the plugin:")
    print("  hermes gateway restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())

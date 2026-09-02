#!/usr/bin/env python3
"""Install the Hermes XMPP platform plugin.

Copies the plugin source into the active Hermes profile, enables it in
config.yaml, and ensures Python dependencies are installed in the Hermes
virtual environment.
"""

from __future__ import annotations

import argparse
import getpass
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn, Optional

from hermes_xmpp_plugin_common import (
    add_default_xmpp_config,
    add_voice_and_stt_defaults,
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

DEPENDENCIES: list[tuple[str, str]] = [
    # (pip package, python import name)
    ("slixmpp", "slixmpp"),
    ("httpx", "httpx"),
    ("Pillow", "PIL"),
    ("cryptography", "cryptography"),
    ("slixmpp-omemo", "slixmpp_omemo"),
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

    to_install = []
    for pip_name, import_name in DEPENDENCIES:
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
    allow_all_users: bool = False,
) -> None:
    if not config_path.exists():
        print(f"Config not found at {config_path}; creating minimal config")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_text = ""
    else:
        config_text = config_path.read_text()

    config_text = enable_plugin(config_text)
    if add_defaults:
        config_text = add_default_xmpp_config(config_text, allow_all_users=allow_all_users)
        config_text = add_voice_and_stt_defaults(config_text)

    config_path.write_text(config_text)


def validate_avatar_path(path: str) -> tuple[bool, str]:
    """Return (ok, message) for a proposed avatar path."""
    if not path:
        return True, ""
    p = Path(path).expanduser()
    if not p.exists():
        return False, f"Avatar path does not exist: {p}"
    if not p.is_file():
        return False, f"Avatar path is not a file: {p}"
    return True, ""


def _upsert_env_line(lines: list[str], key: str, value: str) -> tuple[list[str], bool]:
    """Insert or update KEY="value" among .env lines. Returns (new_lines, changed).

    Updates an existing line in place (dropping duplicate key lines) instead of
    appending a second entry for the same key.
    """
    rendered = f'{key}="{value}"'
    out: list[str] = []
    replaced = False
    changed = False
    for line in lines:
        if "=" in line and line.split("=", 1)[0].strip() == key:
            if replaced:
                continue  # drop duplicate key lines
            if line.strip() != rendered:
                out.append(rendered)
                changed = True
            else:
                out.append(line)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(rendered)
        changed = True
    return out, changed


def normalize_allowed_users(raw: str) -> str:
    """Normalize a comma-separated XMPP allowlist string ('' when empty)."""
    return ",".join(part.strip() for part in raw.split(",") if part.strip())


def prompt_for_avatar(avatar_path: str) -> str:
    """Validate and optionally re-prompt for an avatar path."""
    ok, msg = validate_avatar_path(avatar_path)
    if ok:
        return avatar_path

    print(f"\nWARNING: {msg}")
    while True:
        new_path = input("Enter a valid avatar file path (or leave blank for none): ").strip()
        if not new_path:
            return ""
        ok, msg = validate_avatar_path(new_path)
        if ok:
            return new_path
        print(f"WARNING: {msg}")


def prompt_xmpp_credentials(
    args: argparse.Namespace,
    env_path: Path,
):
    """Return (jid, password, avatar_path, allowed_users, allow_all_users).

    If the Hermes .env file already contains XMPP_USER_JID or XMPP_PASSWORD,
    those values are shown as defaults; the user can press Enter to keep them.

    ``allow_all_users`` is True only when the user explicitly opted to allow
    every sender (no allowlist). Leaving the allowlist blank without that
    explicit opt-in does NOT silently open the bot to everyone.
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

    if args.allowed_users is not None:
        allowed_users = normalize_allowed_users(args.allowed_users)
    else:
        default_allowed = defaults.get("XMPP_ALLOWED_USERS", "")
        print(
            "\nAllowed users (who may talk to this bot). Comma-separated XMPP JIDs."
        )
        print(
            "IMPORTANT: if you do not set an allowlist, ANY user who can reach "
            "your agent over XMPP will be able to talk to it."
        )
        prompt = (
            f"Allowed user JIDs [{default_allowed}]: "
            if default_allowed
            else "Allowed user JIDs (comma-separated, blank to allow everyone): "
        )
        raw = input(prompt).strip()
        if not raw:
            raw = default_allowed
        allowed_users = normalize_allowed_users(raw)

    allow_all_users = False
    if not allowed_users:
        # No allowlist: require an explicit opt-in to allow all users rather
        # than silently opening the bot to every sender.
        print(
            "\nNo allowed users were set. If you leave it this way, ANY user "
            "who can reach your agent over XMPP will be able to talk to it."
        )
        while True:
            choice = input(
                "Allow ALL users to talk to this agent? (yes/no): "
            ).strip().lower()
            if choice in ("yes", "y"):
                allow_all_users = True
                break
            if choice in ("no", "n"):
                allow_all_users = False
                break
            print("Please answer yes or no.")

    avatar_path = args.avatar_path or ""
    if not avatar_path:
        print(
            "\nOptional avatar image. Recommended: a square PNG or JPEG, "
            "at least 480x480 pixels. The plugin will crop to a centered "
            "square and resize to 480x480."
        )
        avatar_path = input("Avatar file path (leave blank for none): ").strip()

    avatar_path = prompt_for_avatar(avatar_path)

    return jid, password, avatar_path, allowed_users, allow_all_users


def append_env_credentials(
    env_path: Path,
    jid: str,
    password: str,
    avatar_path: str = "",
    allowed_users: str = "",
    allow_all_users: bool = False,
    home_channel: str = "",
) -> None:
    """Append credentials and avatar path to the Hermes .env file if not already present.

    Stores XMPP_USER_JID, XMPP_PASSWORD, and XMPP_AVATAR_PATH. Never writes secrets to config.yaml.
    XMPP_ALLOWED_USERS is upserted (existing value updated in place) so reinstalling
    with a new allowlist takes effect without manual .env editing.
    XMPP_ALLOW_ALL_USERS is written only when the user explicitly opted to allow
    every sender (no allowlist).
    XMPP_HOME_CHANNEL is seeded from the first allowed user (cron/restart
    notification target) unless it is already set in .env — an existing value
    (or one set later via /sethome) always wins.
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
    if avatar_path and "XMPP_AVATAR_PATH" not in existing_keys:
        additions.append(f'XMPP_AVATAR_PATH="{avatar_path}"')

    if allowed_users:
        lines, upserted = _upsert_env_line(lines, "XMPP_ALLOWED_USERS", allowed_users)
        # An explicit allowlist must not be silently defeated by a stale
        # allow-all flag from a previous install. Clear it so the allowlist
        # actually takes effect.
        lines, cleared = _upsert_env_line(lines, "XMPP_ALLOW_ALL_USERS", "false")
        upserted = upserted or cleared
    else:
        upserted = False

    if allow_all_users:
        lines, allow_all_upserted = _upsert_env_line(
            lines, "XMPP_ALLOW_ALL_USERS", "true"
        )
        upserted = upserted or allow_all_upserted
    elif not allowed_users:
        # No allowlist and not allow-all: clear any stale allow-all flag from a
        # previous install so the bot defaults to deny-all rather than silently
        # staying open to every sender.
        lines, cleared = _upsert_env_line(lines, "XMPP_ALLOW_ALL_USERS", "false")
        upserted = upserted or cleared

    # Seed the cron/restart-notification home target from the first allowed
    # user so the bot has a delivery target without /sethome. Never overwrite
    # an existing XMPP_HOME_CHANNEL (install-time choice or /sethome result).
    if home_channel and "XMPP_HOME_CHANNEL" not in existing_keys:
        lines, seeded = _upsert_env_line(lines, "XMPP_HOME_CHANNEL", home_channel)
        upserted = upserted or seeded

    if additions:
        body = (lines + additions) if (env_path.exists() or lines) else additions
        env_path.write_text("\n".join(body) + "\n")
    elif upserted or (lines and _env_text_changed(env_path, lines)):
        env_path.write_text("\n".join(lines) + "\n")

    if additions:
        print(f"Appended credentials to {env_path}")
    if allowed_users:
        print(f"Allowed users written to {env_path}: XMPP_ALLOWED_USERS={allowed_users}")
    if allow_all_users:
        print(f"Allow-all-users written to {env_path}: XMPP_ALLOW_ALL_USERS=true")


def _env_text_changed(env_path: Path, lines: list[str]) -> bool:
    if not env_path.exists():
        return bool(lines)
    return env_path.read_text() != "\n".join(lines) + "\n"


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
        if key in ("XMPP_USER_JID", "XMPP_PASSWORD", "XMPP_ALLOWED_USERS"):
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
        "--allowed-users",
        metavar="JIDS",
        help=(
            "Comma-separated XMPP JIDs allowed to talk to the bot. Stored as "
            "XMPP_ALLOWED_USERS in the profile .env. If omitted, you will be "
            "prompted unless --no-defaults is set."
        ),
    )
    parser.add_argument(
        "--allow-all-users",
        action="store_true",
        help=(
            "Allow every user to talk to the bot (no allowlist). Sets "
            "XMPP_ALLOW_ALL_USERS=true and allow_all_users: true in config.yaml. "
            "Use only if you explicitly want to open the agent to all senders."
        ),
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
        python = get_hermes_python(
            profile_dir,
            args.python,
            fallback_home=get_hermes_home(None),
        )
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
    allowed_users = ""
    allow_all_users = False
    if not args.no_defaults:
        if args.non_interactive:
            if not args.jid or not args.password:
                fail("--non-interactive requires --jid and --password")
            jid = args.jid
            password = args.password
            avatar_path = args.avatar_path or ""
            ok, msg = validate_avatar_path(avatar_path)
            if avatar_path and not ok:
                fail(msg)
            allowed_users = normalize_allowed_users(args.allowed_users or "")
            allow_all_users = bool(args.allow_all_users)
        else:
            jid, password, avatar_path, allowed_users, allow_all_users = (
                prompt_xmpp_credentials(args, env_path)
            )

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
        allow_all_users=allow_all_users,
    )

    if not args.no_defaults and jid and password:
        # Seed the .env home channel from the first allowed user so cron
        # delivery and restart notifications work without /sethome. Empty
        # allowlist -> no seed (there is no sensible default target).
        first_allowed = allowed_users.split(",")[0].strip() if allowed_users else ""
        append_env_credentials(
            env_path,
            jid,
            password,
            avatar_path=avatar_path,
            allowed_users=allowed_users,
            allow_all_users=allow_all_users,
            home_channel=first_allowed,
        )
        if first_allowed:
            print(f"  Home channel seeded from first allowed user: XMPP_HOME_CHANNEL={first_allowed}")
        print("  XMPP credentials stored in .env (not config.yaml).")

    print("\nInstallation complete.")
    print("Voice-message defaults were added to config.yaml:")
    print("  stt:")
    print("    enabled: true")
    print("    provider: local")
    print("    local:")
    print("      model: tiny")
    print("  voice:")
    print("    auto_tts: false")
    print("  tts:")
    print("    provider: edge")
    print("Restart the Hermes gateway to load the plugin:")
    print("  hermes gateway restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())

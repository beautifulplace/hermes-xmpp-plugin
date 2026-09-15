#!/usr/bin/env python3
"""Post-install configuration for the Hermes XMPP plugin.

Run this AFTER ``hermes plugins install`` (the core installer) to complete
what that command intentionally does not do:

  * install plugin Python dependencies (into the plugin's own ``deps/``
    directory, never touches externally-managed Pythons, PEP 668, uv, etc.)
  * add the default ``platforms.xmpp`` block (OMEMO on by default) and the
    voice/STT defaults to config.yaml
  * prompt for the bot JID and password, the allowed-users allowlist
    (deny-all by default), and an optional avatar path
  * write XMPP credentials, allowlist, home channel, and avatar path into the
    profile .env

This is the ONLY installer step beyond the native ``hermes plugins install``
command; run it, then restart the gateway.

Usage:
    python3 post_install.py [options]

Safe to re-run: existing .env values win, config defaults are only added
when missing, and dependencies already importable are skipped.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NoReturn

# The vendored common module lives next to this script (we are inside the
# installed plugin directory).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hermes_xmpp_plugin_common import (  # noqa: E402
    _load_env_credentials,
    add_default_xmpp_config,
    add_voice_and_stt_defaults,
    append_env_credentials,
    backup_file,
    enable_plugin,
    get_hermes_home,
    get_hermes_python,
    get_profile_dir,
    is_plugin_enabled,
    normalize_allowed_users,
    validate_avatar_path,
)

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
    raise SystemExit(1)


def install_dependencies(python: Path, plugin_dir: Path) -> None:
    """Ensure plugin dependencies are importable by the gateway.

    The gateway imports vendored deps from ``plugin_dir/deps`` (added to
    ``sys.path`` by the plugin ``__init__.py``), NOT from the interpreter's
    site-packages. So the correct "already satisfied" check is whether the
    dependency is importable with ``deps`` prepended to ``sys.path``; probing
    the bare venv python always finds them missing and re-downloads the whole
    81 MB tree on every re-run. Only deps missing from ``deps/`` are installed.
    """
    deps_dir = plugin_dir / "deps"
    deps_dir.mkdir(parents=True, exist_ok=True)

    to_install = []
    for pip_name, import_name in DEPENDENCIES:
        probe = (
            f"import sys; sys.path.insert(0, {str(deps_dir)!r}); "
            f"import {import_name}"
        )
        try:
            import subprocess

            subprocess.run(
                [str(python), "-c", probe],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print(f"  {pip_name}: already installed")
        except Exception:
            to_install.append(pip_name)

    if to_install:
        print(
            f"Installing missing dependencies into {deps_dir} with {python}: "
            f"{', '.join(to_install)}"
        )
        import subprocess

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
    """Enable the plugin in config.yaml, adding default blocks if requested."""
    if not config_path.exists():
        print(f"ERROR: config not found: {config_path}")
        raise SystemExit(1)

    text = config_path.read_text()
    if is_plugin_enabled(text):
        print("Plugin already enabled in config.yaml.")
    else:
        text = enable_plugin(text)
        print("Enabled platforms/xmpp in plugins list.")

    if add_defaults:
        text = add_default_xmpp_config(text, allow_all_users=allow_all_users)
        text = add_voice_and_stt_defaults(text)
        print("Default platforms.xmpp block present (OMEMO enabled by default).")
        print("Voice/STT defaults present.")

    config_path.write_text(text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Post-install configuration for the Hermes XMPP plugin "
        "(run after 'hermes plugins install')."
    )
    parser.add_argument("--hermes-home", help="Hermes home directory (default: ~/.hermes)")
    parser.add_argument("--profile", default=None, help="Hermes profile name")
    parser.add_argument("--python", default=None, help="Python interpreter for dependency install")
    parser.add_argument(
        "--allow-all-users", action="store_true",
        help="Allow ALL XMPP users to talk to the bot (no allowlist). "
        "Without this (and without an allowlist) the bot denies every sender.",
    )
    parser.add_argument(
        "--allowed-users", default=None,
        help="Comma-separated XMPP JIDs allowed to talk to the bot",
    )
    parser.add_argument(
        "--home-channel", default=None,
        help="Default JID for cron/notifications (default: first allowed user; "
        "existing .env value always wins)",
    )
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="Never prompt; requires --allow-all-users or --allowed-users to be meaningful",
    )
    args = parser.parse_args(argv)

    try:
        hermes_home = get_hermes_home(args.hermes_home)
        profile_dir = get_profile_dir(hermes_home, args.profile)
    except FileNotFoundError as exc:
        fail(str(exc))

    config_path = profile_dir / "config.yaml"
    env_path = profile_dir / ".env"
    plugin_dir = Path(__file__).resolve().parent

    print("=" * 60)
    print("Hermes XMPP Plugin - Post-Install Configuration")
    print("=" * 60)
    print(f"Hermes home:   {hermes_home}")
    print(f"Profile dir:   {profile_dir}")
    print(f"Plugin dir:    {plugin_dir}")

    try:
        python = get_hermes_python(
            profile_dir,
            args.python,
            fallback_home=get_hermes_home(None),
        )
    except FileNotFoundError as exc:
        fail(str(exc))

    # 1. Dependencies (never into system Python)
    install_dependencies(python, plugin_dir)

    # 2. Enable plugin + default config blocks
    if config_path.exists():
        backup_path = backup_file(config_path, ".postinstall-backup")
        print(f"Backed up config to {backup_path}")
    enable_plugin_in_config(config_path, add_defaults=True, allow_all_users=args.allow_all_users)

    # 3. Credentials, allowlist, avatar in .env
    existing = _load_env_credentials(env_path)

    # JID/password first: prompt with the existing value as the default so the
    # user can keep it (Enter) or change it. Only skipped under --non-interactive.
    jid = existing.get("XMPP_USER_JID", "")
    password = existing.get("XMPP_PASSWORD", "")
    if not args.non_interactive:
        jid_prompt = f"XMPP JID for the bot account [{jid}]: " if jid else "XMPP JID for the bot account: "
        jid_answer = input(jid_prompt).strip()
        if jid_answer:
            jid = jid_answer
        while not jid:
            jid = input("XMPP JID is required (e.g. hermes@example.com): ").strip()

        import getpass

        pw_prompt = "XMPP password [Enter to keep existing]: " if password else "XMPP password: "
        pw_answer = getpass.getpass(pw_prompt).strip()
        if pw_answer:
            password = pw_answer
        while not password:
            password = getpass.getpass("XMPP password is required: ").strip()
    if not jid or not password:
        print(
            "WARNING: XMPP_USER_JID / XMPP_PASSWORD are not both set in "
            f"{env_path}. Set them manually or the adapter cannot connect."
        )

    # Allowed users: at least one is REQUIRED (the bot is unusable without a
    # sender allowlist, exactly like the JID/password). The existing allowlist
    # is shown as the default and Enter keeps it.
    allowed_users = normalize_allowed_users(args.allowed_users or "")
    allow_all = bool(args.allow_all_users)
    if not allowed_users and not allow_all and not args.non_interactive:
        existing_allowed = existing.get("XMPP_ALLOWED_USERS", "")
        prompt = (
            f"Allowed XMPP users (comma-separated JIDs) [{existing_allowed}]: "
            if existing_allowed
            else "Allowed XMPP users (comma-separated JIDs): "
        )
        raw = input(prompt).strip()
        allowed_users = normalize_allowed_users(raw or existing_allowed)
        while not allowed_users:
            print("At least one allowed user is required.")
            raw = input("Allowed XMPP users (comma-separated JIDs): ").strip()
            allowed_users = normalize_allowed_users(raw)
    elif not allowed_users and not allow_all and args.non_interactive:
        fail(
            "No allowed users: pass --allowed-users or --allow-all-users "
            "(the bot is unusable without at least one allowed sender)."
        )

    home_channel = ""
    if args.home_channel:
        home_channel = args.home_channel.strip()
    elif allowed_users:
        home_channel = allowed_users.split(",")[0].strip()

    avatar_path = existing.get("XMPP_AVATAR_PATH", "")
    if not args.non_interactive:
        print(
            "\nOptional avatar image. Recommended: a square PNG or JPEG, "
            "at least 480x480 pixels. The plugin will crop to a centered "
            "square and resize to 480x480."
        )
        if avatar_path:
            avatar_path = input(f"Avatar file path [{avatar_path}]: ").strip() or avatar_path
        else:
            avatar_path = input("Avatar file path (leave blank for none): ").strip()
        ok, msg = validate_avatar_path(avatar_path)
        while not ok:
            print(f"WARNING: {msg}")
            avatar_path = input("Enter a valid avatar file path (or leave blank for none): ").strip()
            ok, msg = validate_avatar_path(avatar_path)

    append_env_credentials(
        env_path,
        jid=jid,
        password=password,
        avatar_path=avatar_path,
        allowed_users=allowed_users,
        allow_all_users=allow_all,
        home_channel=home_channel,
    )
    if jid and password:
        print("  XMPP credentials stored in .env (not config.yaml).")
    if allowed_users:
        print(f"Allowed users: XMPP_ALLOWED_USERS={allowed_users}")
    if allow_all:
        print(f"Allow-all-users written to {env_path}: XMPP_ALLOW_ALL_USERS=true")
    if home_channel and not _env_already_has_home(env_path):
        print(f"Home channel seeded: XMPP_HOME_CHANNEL={home_channel}")

    print("\nPost-install complete.")
    print("Restart the Hermes gateway to load the plugin:")
    print("  hermes gateway restart")
    return 0


def _env_already_has_home(env_path: Path) -> bool:
    """True when XMPP_HOME_CHANNEL is already set in the profile .env."""
    if not env_path.exists():
        return False
    for line in env_path.read_text().splitlines():
        if "=" in line and line.split("=", 1)[0].strip() == "XMPP_HOME_CHANNEL":
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())

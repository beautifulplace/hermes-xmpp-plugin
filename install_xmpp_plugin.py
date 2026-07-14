#!/usr/bin/env python3
"""Install the Hermes XMPP platform plugin.

Copies the plugin source into the active Hermes profile, enables it in
config.yaml, and ensures Python dependencies are installed in the Hermes
virtual environment.
"""

from __future__ import annotations

import argparse
import getpass
import io
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
    get_yaml_editor,
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

WHISPER_MODELS = ("tiny", "base", "small", "medium", "large-v1", "large-v2", "large-v3")
DEFAULT_WHISPER_MODEL = "tiny"


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
    only_required: bool,
    whisper_model: Optional[str] = None,
    melotts: bool = False,
    hf_token: str = "",
) -> None:
    """Ensure plugin dependencies are importable by the gateway.

    First checks whether each dependency is already available in the gateway's
    Python environment. Any missing packages are installed into a ``deps``
    subdirectory under the plugin so we do not modify externally-managed Python
    installations (uv, system PEP-668, etc.).

    If ``whisper_model`` is set, ``faster-whisper`` is installed and the model
    is pre-downloaded so that first-use transcription does not block on a
    network download.
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

    if whisper_model:
        try:
            subprocess.run(
                [str(python), "-c", "import faster_whisper"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print("  faster-whisper: already installed")
        except subprocess.CalledProcessError:
            to_install.append("faster-whisper")

    if melotts:
        try:
            subprocess.run(
                [str(python), "-c", "from melo.api import TTS"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print("  melotts: already installed")
        except subprocess.CalledProcessError:
            to_install.append("git+https://github.com/myshell-ai/MeloTTS.git")

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

    if whisper_model:
        print(f"Pre-downloading faster-whisper model: {whisper_model}")
        if whisper_model.startswith("large"):
            total_ram_gb = 0
            try:
                with open("/proc/meminfo") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            total_ram_gb = int(line.split()[1]) / (1024 * 1024)
                            break
            except Exception:
                pass
            if total_ram_gb and total_ram_gb < 8:
                print(
                    f"  WARNING: '{whisper_model}' needs ~3-4 GB RAM. "
                    f"This system reports {total_ram_gb:.1f} GB total RAM. "
                    "Consider using 'tiny', 'base', or 'small' instead.",
                    file=sys.stderr,
                )
            print(
                "  WARNING: large models are very slow on CPU and need a GPU for real-time. "
                "Consider 'medium', 'small', or 'base' for CPU inference.",
                file=sys.stderr,
            )
        print(
            "  The model will be downloaded from Hugging Face. "
            "For large models an HF_TOKEN is recommended for faster downloads."
        )
        print(f"  Download size for '{whisper_model}': see https://github.com/SYSTRAN/faster-whisper#model-size")
        try:
            env = _python_env()
            # If an HF_TOKEN was collected/stored, use it for the download.
            if hf_token:
                env["HF_TOKEN"] = hf_token
                print("  Using HF_TOKEN from installer prompt for model download.")
            elif os.environ.get("HF_TOKEN"):
                env["HF_TOKEN"] = os.environ.get("HF_TOKEN") or ""
                print("  Using HF_TOKEN from environment for model download.")
            print("  Downloading model... (progress will appear below)")
            subprocess.run(
                [
                    str(python), "-c",
                    "import os; "
                    "import sys; "
                    "from huggingface_hub import snapshot_download; "
                    "from faster_whisper import WhisperModel; "
                    "model = sys.argv[1]; "
                    "repo = f'Systran/faster-whisper-{model}'; "
                    "snapshot_download(repo, local_files_only=False); "
                    "WhisperModel(model, device='cpu', compute_type='int8')",
                    whisper_model,
                ],
                check=True,
                env=env,
            )
            print(f"  faster-whisper model '{whisper_model}' is ready.")
        except subprocess.CalledProcessError as exc:
            print(
                f"  WARNING: failed to pre-download faster-whisper model: {exc}",
                file=sys.stderr,
            )

    if melotts:
        print("Pre-downloading MeloTTS English model")
        try:
            env = _python_env()
            if hf_token:
                env["HF_TOKEN"] = hf_token
                print("  Using HF_TOKEN from installer prompt for MeloTTS download.")
            elif os.environ.get("HF_TOKEN"):
                env["HF_TOKEN"] = os.environ.get("HF_TOKEN") or ""
                print("  Using HF_TOKEN from environment for MeloTTS download.")
            print("  Downloading model... (progress will appear below)")
            subprocess.run(
                [
                    str(python), "-c",
                    "import os; "
                    "import sys; "
                    "from melo.api import TTS; "
                    "model = TTS(language='EN', use_hf=True); "
                    "print('MeloTTS EN model ready')",
                ],
                check=True,
                env=env,
            )
            print("  MeloTTS English model is ready.")
        except subprocess.CalledProcessError as exc:
            print(
                f"  WARNING: failed to pre-download MeloTTS model: {exc}",
                file=sys.stderr,
            )


def enable_plugin_in_config(
    config_path: Path,
    add_defaults: bool,
    avatar_path: str = "",
    voice_tts: str = "",
    voice_model: str = "",
    whisper_model: str = "",
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

    if voice_tts:
        config_text = _set_config_value(config_text, "platforms", "xmpp", "voice_tts", voice_tts)
    if voice_model:
        config_text = _set_config_value(config_text, "platforms", "xmpp", "voice_model", voice_model)

    if whisper_model:
        # Ensure STT is enabled with the chosen local model.
        config_text = _ensure_stt_config(config_text, whisper_model)

    config_path.write_text(config_text)


def _ensure_stt_config(config_text: str, model: str) -> str:
    """Enable local STT in config.yaml and set the faster-whisper model."""
    yaml, uses_ruamel = get_yaml_editor()
    if uses_ruamel:
        data = yaml.load(config_text)
    else:
        data = yaml.safe_load(config_text)

    if data is None:
        data = {}
    if "stt" not in data or not isinstance(data["stt"], dict):
        data["stt"] = {}
    data["stt"]["enabled"] = True
    data["stt"]["provider"] = "local"
    if "local" not in data["stt"] or not isinstance(data["stt"]["local"], dict):
        data["stt"]["local"] = {}
    data["stt"]["local"]["model"] = model

    if uses_ruamel:
        stream = io.StringIO()
        yaml.dump(data, stream)
        return stream.getvalue()
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


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
    whisper_model: str = "",
    with_melotts: bool = False,
) -> tuple[str, str, str, str]:
    """Return (jid, password, avatar_path, hf_token), prompting for missing values.

    If the Hermes .env file already contains XMPP_USER_JID, XMPP_PASSWORD, or
    HF_TOKEN, those values are shown as defaults; the user can press Enter to
    keep them.
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

    hf_token = ""
    needs_hf_token = (
        (whisper_model and whisper_model.startswith("large")) or with_melotts
    )
    if needs_hf_token:
        print(
            "\nHugging Face token (optional). Model downloads are faster "
            "and more reliable with an HF_TOKEN. Leave blank to skip."
        )
        default_hf_token = defaults.get("HF_TOKEN", "")
        if default_hf_token:
            prompt = "HF_TOKEN [press Enter to keep existing]: "
        else:
            prompt = "HF_TOKEN: "
        hf_token = getpass.getpass(prompt)
        if not hf_token:
            hf_token = default_hf_token

    return jid, password, avatar_path, hf_token


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
        if key in ("XMPP_USER_JID", "XMPP_PASSWORD", "HF_TOKEN"):
            result[key] = value.strip().strip('"\'')
    return result


def append_env_credentials(
    env_path: Path,
    jid: str,
    password: str,
    hf_token: str = "",
) -> None:
    """Append credentials to the Hermes .env file if not already present.

    Stores XMPP_JID, XMPP_PASSWORD, and optionally HF_TOKEN. Never writes
    secrets to config.yaml.
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
    if hf_token and "HF_TOKEN" not in existing_keys:
        additions.append(f'HF_TOKEN="{hf_token}"')

    if not additions:
        return

    if env_path.exists():
        env_path.write_text("\n".join(lines + additions) + "\n")
    else:
        env_path.write_text("\n".join(additions) + "\n")
    print(f"Appended credentials to {env_path}")


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
        "--with-whisper",
        metavar="MODEL",
        nargs="?",
        const=DEFAULT_WHISPER_MODEL,
        default=None,
        choices=WHISPER_MODELS,
        help=(
            "Install faster-whisper and enable local STT. "
            "MODEL can be one of: tiny, base, small, medium, large-v1, large-v2, large-v3. "
            "If no model is specified, defaults to tiny."
        ),
    )
    parser.add_argument(
        "--with-melotts",
        action="store_true",
        help="Install MeloTTS for high-quality local text-to-speech voice replies",
    )
    parser.add_argument(
        "--voice-tts",
        choices=("edge", "melo"),
        default=None,
        help="Set platforms.xmpp.voice_tts in config.yaml (default: edge). Use 'melo' with --with-melotts.",
    )
    parser.add_argument(
        "--voice-model",
        metavar="MODEL",
        default=None,
        help=(
            "Set platforms.xmpp.voice_model in config.yaml. For MeloTTS this can be "
            "a speaker name such as EN-Default, EN-US, EN-BR, EN-AU, EN-IN."
        ),
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

    # Prompt for credentials before any download so that secrets (HF_TOKEN)
    # are available when we pre-download the Whisper model.
    jid = ""
    password = ""
    avatar_path = ""
    hf_token = ""
    if not args.no_defaults:
        if args.non_interactive:
            if not args.jid or not args.password:
                fail("--non-interactive requires --jid and --password")
            jid = args.jid
            password = args.password
            avatar_path = args.avatar_path or ""
            if args.with_whisper and args.with_whisper.startswith("large"):
                print(
                    "WARNING: --with-whisper large* models may need HF_TOKEN for reliable downloads. "
                    "Set HF_TOKEN in the .env file manually, or run interactively.",
                    file=sys.stderr,
                )
            if args.with_melotts:
                print(
                    "WARNING: --with-melotts downloads models from Hugging Face. "
                    "Set HF_TOKEN in the .env file manually for faster downloads, or run interactively.",
                    file=sys.stderr,
                )
        else:
            jid, password, avatar_path, hf_token = prompt_xmpp_credentials(
                args, env_path,
                whisper_model=args.with_whisper,
                with_melotts=args.with_melotts,
            )

    # If we collected an HF_TOKEN during credential prompting, write it to
    # .env immediately so the model pre-download can use it.
    if hf_token and env_path.exists():
        append_env_credentials(env_path, jid, password, hf_token=hf_token)

    copy_plugin(plugin_src, plugin_dest, force=args.force)
    install_dependencies(
        python,
        plugin_dest,
        only_required=args.only_required_deps,
        whisper_model=args.with_whisper,
        melotts=args.with_melotts,
        hf_token=hf_token,
    )

    if config_path.exists():
        backup_path = backup_file(config_path, ".install-backup")
        print(f"Backed up config to {backup_path}")

    enable_plugin_in_config(
        config_path,
        add_defaults=not args.no_defaults,
        avatar_path=avatar_path,
        voice_tts=args.voice_tts or ("melo" if args.with_melotts else "edge"),
        voice_model=args.voice_model or ("EN-Default" if args.with_melotts else "en-US-AriaNeural"),
        whisper_model=args.with_whisper,
    )

    if not args.no_defaults and jid and password:
        append_env_credentials(env_path, jid, password, hf_token=hf_token)
        print("  XMPP credentials stored in .env (not config.yaml).")

    print("\nInstallation complete.")
    if args.with_whisper:
        print(f"Local STT enabled with faster-whisper model: {args.with_whisper}")
    if args.with_melotts:
        print("Local TTS enabled with MeloTTS.")
    print("Restart the Hermes gateway to load the plugin:")
    print("  hermes gateway restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())

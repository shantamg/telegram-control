#!/usr/bin/env python3
"""Install and verify the shared Herdr launch and Telegram handoff tools."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
WRAPPER_MARKERS = (
    b"# Keep Herdr's terminal environment",
    b"# Herdr scoped Codex launcher",
)
LOCAL_CODEX_PATHS = (
    Path("/opt/homebrew/bin/codex"),
    Path("/usr/local/bin/codex"),
    Path("/usr/bin/codex"),
)


def write_file(destination: Path, data: bytes, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError(f"Expected a real file at {destination}")
    if destination.is_file() and destination.read_bytes() == data:
        destination.chmod(mode)
        return
    fd, staged = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), mode)
        os.replace(staged, destination)
    finally:
        Path(staged).unlink(missing_ok=True)


def install_link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() and link.resolve() == target.resolve():
        return
    if link.exists() or link.is_symlink():
        raise ValueError(f"Conflicting entry at {link}")
    link.symlink_to(os.path.relpath(target, link.parent))


def codex_path(home: Path) -> Path:
    candidates = (home / ".local/share/herdr-tools/bin/codex", *LOCAL_CODEX_PATHS)
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise ValueError("Install Codex CLI before enabling the Herdr launcher.")


def base_paths(home: Path) -> tuple[Path, Path, Path]:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or str(home / ".config"))
    data = Path(os.environ.get("XDG_DATA_HOME") or str(home / ".local/share"))
    if sys.platform == "darwin":
        bridge = home / "Library/Application Support/telegram-bridge"
    else:
        bridge = config / "telegram-bridge"
    return config, data / "telegram-control", bridge


def verify_checkout() -> str:
    for required in ("herdr_telegram.py", "telegram_control.py", "vendor/ai-accounts"):
        if not (ROOT / required).exists():
            raise ValueError(f"Checkout is incomplete: {required}")
    if not any((ROOT / "vendor/ai-accounts").iterdir()):
        raise ValueError("Initialize the pinned ai-accounts submodule before installation.")
    result = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def install(home: Path) -> dict:
    config, deploy, _ = base_paths(home)
    revision = verify_checkout()
    binary = home / ".local/bin"
    codex = codex_path(home)
    help_text = subprocess.check_output([str(codex), "--help"], text=True)
    if "--no-daemon" not in help_text:
        raise ValueError(f"{codex} needs a Codex release with --no-daemon.")
    wrapper = binary / "codex"
    if wrapper.is_symlink() or (wrapper.is_file() and not any(
        marker in wrapper.read_bytes() for marker in WRAPPER_MARKERS
    )):
        raise ValueError(f"Review the existing custom Codex launcher at {wrapper}.")

    app = deploy / "app"
    if app.exists() and not app.is_symlink() and app.resolve() != ROOT:
        raise ValueError(f"{app} is a real installation. Move it to a versioned release first.")
    for name in ("herdr-show", "herdr-spawn"):
        link = binary / name
        target = home / ".agents/skills/herdr-session/scripts" / name
        if (link.exists() or link.is_symlink()) and link.resolve() != target.resolve():
            raise ValueError(f"Conflicting launcher link at {link}")
    for name in ("herdr-session", "herdr-telegram-handoff"):
        destination = home / ".agents/skills" / name
        if destination.is_symlink():
            raise ValueError(f"Expected a shared skill directory at {destination}")
        link = home / ".claude/skills" / name
        if (link.exists() or link.is_symlink()) and link.resolve() != destination.resolve():
            raise ValueError(f"Conflicting Claude skill link at {link}")
    deploy.mkdir(parents=True, exist_ok=True)

    def launcher(name: str) -> str:
        command = shlex.join([sys.executable, str(app / "deploy/herdr-run.py"), name])
        return f'#!/bin/sh\nexec {command} "$@"\n'

    codex_wrapper = (ROOT / "deploy/codex-herdr").read_text()
    guard = 'if [ -n "${HC_TERMINAL_ID:-}" ]; then'
    dispatch = launcher("codex").splitlines()[1]
    write_file(wrapper, codex_wrapper.replace(guard, guard + "\n    " + dispatch).encode(), 0o755)
    template = (ROOT / "deploy/herdr-codex-template.md").read_text()
    write_file(
        config / "herdr-core/templates/codex.md",
        template.replace("{{CODEX_COMMAND}}", str(wrapper)).encode(),
        0o644,
    )

    guides = home / ".agents/skills"
    for name in ("herdr-session", "herdr-telegram-handoff"):
        source = ROOT / "skills" / name
        destination = guides / name
        content = (source / "SKILL.md").read_text()
        if name == "herdr-telegram-handoff":
            content = content.replace(
                '/usr/bin/python3 "{{TELEGRAM_CONTROL_ROOT}}/herdr_telegram.py"',
                "herdr-telegram",
            )
        write_file(destination / "SKILL.md", content.encode(), 0o644)
        install_link(home / ".claude/skills" / name, destination)
    for name in ("herdr-show", "herdr-spawn"):
        relative = Path("herdr-session/scripts") / name
        write_file(guides / relative, launcher(name).encode(), 0o755)
        install_link(binary / name, guides / relative)

    write_file(binary / "herdr-telegram", launcher("herdr-telegram").encode(), 0o755)
    # Keep the app link stable because peers store an absolute callback path.
    if app.resolve() != ROOT:
        with tempfile.TemporaryDirectory(prefix=".app-", dir=deploy) as temporary:
            staged_link = Path(temporary) / "app"
            staged_link.symlink_to(ROOT, target_is_directory=True)
            os.replace(staged_link, app)

    return {**check(host=home), "revision": revision}


def check(host: Path) -> dict:
    _, deploy, bridge = base_paths(host)
    settings = bridge / "herdr-relay.json"
    relay = json.loads(settings.read_text()) if settings.is_file() else {}
    if not isinstance(relay, dict):
        raise ValueError("Herdr relay settings must be a JSON object.")
    local_bin = host / ".local/bin"
    tools = {
        name: (local_bin / name).is_file() and os.access(local_bin / name, os.X_OK)
        for name in ("hc", "herdr-show", "herdr-spawn", "herdr-telegram", "codex")
    }
    app = deploy / "app"
    info = {"checkout": str(app.resolve()), "tools": tools}
    if (app / "herdr_telegram.py").is_file():
        revision = subprocess.run(
            ["git", "-C", str(app), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        info["revision"] = revision.stdout.strip()
    else:
        tools["checkout"] = False
    if relay.get("hub"):
        info["hub"] = relay["hub"]
    if relay.get("peers"):
        info["peers"] = sorted(relay["peers"])
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install", "check"])
    parser.add_argument("--home", type=Path, default=Path.home(),
                        help="Account directory used for installation and test environments.")
    parser.add_argument("--compare-branch", metavar="BRANCH",
                        help="With check, compare the installed revision to this branch on origin.")
    args = parser.parse_args()
    if args.compare_branch and args.command != "check":
        parser.error("--compare-branch requires check")
    try:
        result = install(args.home) if args.command == "install" else check(args.home)
        if args.compare_branch:
            branch = "refs/heads/" + args.compare_branch
            output = subprocess.check_output(
                ["git", "-C", result["checkout"], "ls-remote", "--exit-code", "origin", branch],
                text=True, timeout=30,
            )
            matches = [line.split()[0] for line in output.splitlines()
                       if line.split()[1] == branch]
            if len(matches) != 1:
                raise ValueError(f"Could not resolve branch {args.compare_branch}.")
            result["remote_revision"] = matches[0]
            result["matches_remote"] = result.get("revision") == matches[0]
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(result, indent=2))
    if not all(result["tools"].values()):
        return 1
    return 2 if result.get("matches_remote") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())

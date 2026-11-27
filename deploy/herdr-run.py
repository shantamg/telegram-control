#!/usr/bin/env python3
"""Check the shared release before dispatching an installed Herdr integration command."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def git(checkout, *args, timeout=30):
    return subprocess.check_output(
        ["git", "-C", str(checkout), *args], text=True, timeout=timeout,
        stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    ).strip()


def refresh(deploy):
    app = deploy / "app"
    with (deploy / ".update.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        remote = git(app, "ls-remote", "--exit-code", "origin", "refs/heads/main", timeout=8)
        revision, ref = remote.split()
        if ref != "refs/heads/main" or len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
            raise ValueError("Invalid main revision from origin")
        restart = deploy / ".receiver-reload-pending"
        if git(app, "rev-parse", "HEAD") != revision:
            release = deploy / ("app-" + revision)
            origin = git(app, "remote", "get-url", "origin")
            if not release.exists():
                with tempfile.TemporaryDirectory(prefix=".download-", dir=deploy) as directory:
                    staged = Path(directory) / "checkout"
                    git(deploy, "clone", "--quiet", "--no-checkout", "--single-branch",
                        "--branch", "main", "--", origin, str(staged), timeout=120)
                    git(staged, "checkout", "--quiet", "--detach", revision)
                    git(staged, "submodule", "update", "--init", "--recursive", timeout=120)
                    staged.rename(release)
            if git(release, "rev-parse", "HEAD") != revision:
                raise ValueError("Versioned checkout has an unexpected revision")
            if sys.platform.startswith("linux") and subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", "telegram-control.service"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            ).returncode == 0:
                restart.touch()
            subprocess.run([sys.executable, str(release / "deploy/herdr-host.py"), "install"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, check=True)
        if restart.exists():
            subprocess.run([sys.executable, str(app / "telegram_control.py"), "request-restart",
                            "--reason", "Apply updated Herdr integration"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, check=True)
            restart.unlink()


def main():
    # Launchers call this script through the stable app symlink.
    deploy = Path(__file__).absolute().parents[2]
    try:
        refresh(deploy)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Herdr update check failed. Using the installed release. {exc}", file=sys.stderr)
    app = deploy / "app"
    name = sys.argv[1]
    if name in ("herdr-show", "herdr-spawn"):
        command = [str(app / "skills/herdr-session/scripts" / name)]
    elif name == "codex":
        command = [str(app / "deploy/codex-herdr")]
    elif name in ("herdr-telegram", "relay-helper", "handler"):
        entry = app / ("on_message.py" if name == "handler" else "herdr_telegram.py")
        if name == "herdr-telegram" and (deploy / "mac-relay-entry.py").is_file():
            entry = deploy / "mac-relay-entry.py"
        command = [sys.executable, str(entry)]
        # Only the immediately dispatched script skips its own direct-entry check.
        os.environ["HERDR_DISPATCHED_SCRIPT"] = str(entry.resolve())
    else:
        raise ValueError("Unknown installed Herdr command")
    os.execv(command[0], command + sys.argv[2:])


if __name__ == "__main__":
    main()

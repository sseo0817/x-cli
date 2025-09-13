from __future__ import annotations

import os
import shutil
import subprocess
from typing import List, Tuple


TAG = "# x-cli: run-once"


def _crontab_read() -> List[str]:
    try:
        out = subprocess.check_output(["crontab", "-l"], stderr=subprocess.STDOUT, text=True)
        return [line.rstrip("\n") for line in out.splitlines()]
    except subprocess.CalledProcessError as e:
        # Exit code 1 with "no crontab for user" is common; treat as empty
        return []


def _crontab_write(lines: List[str]) -> None:
    text = "\n".join(lines) + ("\n" if lines and not lines[-1].endswith("\n") else "")
    proc = subprocess.Popen(["crontab", "-"], stdin=subprocess.PIPE, text=True)
    assert proc.stdin is not None
    proc.stdin.write(text)
    proc.stdin.close()
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("failed to write crontab")


def _cron_line(repo_path: str) -> str:
    repo_abs = os.path.abspath(repo_path)
    # If a virtualenv exists, activate it before invoking the CLI.
    # Support common names: .venv/ and venv/
    venv_activate_primary = os.path.join(repo_abs, ".venv", "bin", "activate")
    venv_activate_alt = os.path.join(repo_abs, "venv", "bin", "activate")
    # Build a POSIX-sh compatible snippet that conditionally sources the venv
    venv_snippet = (
        f"VENV_ACT=\"{venv_activate_primary}\"; "
        f"[ -f \"$VENV_ACT\" ] || VENV_ACT=\"{venv_activate_alt}\"; "
        f"[ -f \"$VENV_ACT\" ] && . \"$VENV_ACT\";"
    )
    cmd = (
        f"cd {repo_abs} && {venv_snippet} "
        f"{repo_abs}/bin/x run-once >> $HOME/.x-cli/cron.log 2>&1 {TAG}"
    )
    return f"* * * * * {cmd}"


def cron_status(repo_path: str) -> Tuple[bool, str]:
    target = _cron_line(repo_path)
    lines = _crontab_read()
    for line in lines:
        if line.strip().endswith(TAG):
            return True, line
    return False, ""


def cron_on(repo_path: str) -> Tuple[bool, str]:
    # Ensure crontab binary exists
    if not shutil.which("crontab"):
        raise RuntimeError("crontab command not found on this system")
    lines = _crontab_read()
    # Remove any existing x-cli entries
    lines = [ln for ln in lines if TAG not in ln]
    # Add our entry
    entry = _cron_line(repo_path)
    lines.append(entry)
    _crontab_write(lines)
    return True, entry


def cron_off(repo_path: str) -> Tuple[bool, int]:
    if not shutil.which("crontab"):
        raise RuntimeError("crontab command not found on this system")
    lines = _crontab_read()
    before = len(lines)
    lines = [ln for ln in lines if TAG not in ln]
    removed = before - len(lines)
    _crontab_write(lines)
    return True, removed

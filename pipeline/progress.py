"""Commit and push data/ mid-run so a long backfill keeps its progress."""

import subprocess

from config import REPO_ROOT


def push_progress(message: str) -> None:
    run = lambda *a: subprocess.run(a, cwd=REPO_ROOT, check=False)
    run("git", "add", "data")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO_ROOT).returncode == 0:
        return
    run("git", "commit", "-q", "-m", message)
    run("git", "pull", "-q", "--rebase", "--autostash", "origin", "main")
    run("git", "push", "-q")

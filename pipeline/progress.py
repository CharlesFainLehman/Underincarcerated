"""Commit and push backfill progress mid-run so a long backfill keeps it.

Only data/backfill is committed: the daily job owns everything else in
data/, and the two must never edit the same file. A plain merge (not a
rebase) is used so a concurrent daily commit is absorbed without touching
history; there is no overlap in files, so it cannot conflict.
"""

import subprocess

from config import BACKFILL_DIR, REPO_ROOT


def push_progress(message: str) -> None:
    def run(*a):
        return subprocess.run(a, cwd=REPO_ROOT, check=False)

    run("git", "add", str(BACKFILL_DIR))
    if run("git", "diff", "--cached", "--quiet").returncode == 0:
        return
    run("git", "commit", "-q", "-m", message)
    run("git", "pull", "-q", "--no-rebase", "--no-edit", "origin", "main")
    if run("git", "push", "-q").returncode != 0:
        print("  push failed; progress is committed locally and will push next time")

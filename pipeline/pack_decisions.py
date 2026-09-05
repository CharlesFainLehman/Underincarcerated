"""Gzip this run's decision logs and prune old ones, before the commit step.

Workflow artifacts are not readable from every environment, and the logs are
the only record of what the model decided and why. Gzipped they are ~60 KB
per day; keep 30 days in the repo.
"""

import gzip
import shutil
import time

from config import DECISIONS_DIR

KEEP_DAYS = 30


def main() -> None:
    if not DECISIONS_DIR.exists():
        return
    for p in DECISIONS_DIR.glob("*.jsonl"):
        with open(p, "rb") as src, gzip.open(p.with_suffix(".jsonl.gz"), "wb") as dst:
            shutil.copyfileobj(src, dst)
        p.unlink()
    cutoff = time.time() - KEEP_DAYS * 86400
    for p in DECISIONS_DIR.glob("*.jsonl.gz"):
        if p.stat().st_mtime < cutoff:
            p.unlink()
    print(f"decision logs packed: {sorted(x.name for x in DECISIONS_DIR.glob('*.gz'))}")


if __name__ == "__main__":
    main()

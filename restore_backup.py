"""Restore a BrevityBot backup (brevitybot-backup-YYYY-MM-DD.json.gz) into Redis.

    REDIS_URL=redis://... python restore_backup.py backup.json.gz            # dry run
    REDIS_URL=redis://... python restore_backup.py backup.json.gz --apply    # write missing keys
    REDIS_URL=redis://... python restore_backup.py backup.json.gz --apply --overwrite

Existing keys are left alone unless --overwrite is given.
"""
import gzip
import json
import os
import sys

import redis


def restore(r, data: dict, *, apply: bool, overwrite: bool) -> tuple[int, int]:
    """Write ``{key: {"type", "value"}}`` into ``r``. Returns (written, skipped)."""
    written = skipped = 0
    for key, item in data.items():
        if not overwrite and r.exists(key):
            skipped += 1
            continue
        written += 1
        if not apply:
            continue
        t, v = item["type"], item["value"]
        pipe = r.pipeline(transaction=True)
        pipe.delete(key)
        if t == "string":
            pipe.set(key, v)
        elif t == "hash" and v:
            pipe.hset(key, mapping=v)
        elif t == "set" and v:
            pipe.sadd(key, *v)
        elif t == "list" and v:
            pipe.rpush(key, *v)  # export is head-first, so RPUSH keeps order
        pipe.execute()
    return written, skipped


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    with gzip.open(sys.argv[1], "rt") as f:
        data = json.load(f)
    apply, overwrite = "--apply" in sys.argv, "--overwrite" in sys.argv
    r = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    written, skipped = restore(r, data, apply=apply, overwrite=overwrite)
    print(f"{'wrote' if apply else 'would write'} {written} keys, skipped {skipped} existing"
          + ("" if apply else " (dry run; pass --apply)"))

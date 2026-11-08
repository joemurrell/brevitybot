import asyncio
import gzip
import json
from unittest.mock import patch

import brevitybot


class _R:
    store = {
        "post_channels": ("hash", {"1": "2"}),
        "disabled_posting": ("set", {"b", "a"}),
        "post_freq:1": ("string", "6"),
        "greenie:1:9": ("list", ['{"correct":1}']),
        "brevity_terms": ("string", "[]"),  # must NOT be exported
    }

    async def scan_iter(self, match, count):
        import fnmatch
        for k in self.store:
            if fnmatch.fnmatch(k, match):
                yield k

    async def type(self, k): return self.store[k][0]
    async def get(self, k): return self.store[k][1]
    async def hgetall(self, k): return self.store[k][1]
    async def smembers(self, k): return self.store[k][1]
    async def lrange(self, k, a, b): return self.store[k][1]


def test_export_irreplaceable():
    with patch.object(brevitybot, "r", _R()):
        blob = asyncio.new_event_loop().run_until_complete(brevitybot.export_irreplaceable())
    data = json.loads(gzip.decompress(blob))
    assert set(data) == {"post_channels", "disabled_posting", "post_freq:1", "greenie:1:9"}
    assert data["disabled_posting"] == {"type": "set", "value": ["a", "b"]}


def test_upload_and_prune_deletes_only_old():
    from datetime import datetime, timedelta, timezone
    from unittest.mock import MagicMock

    now = datetime.now(timezone.utc)
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [{"Contents": [
        {"Key": "brevitybot-backup-old.json.gz", "LastModified": now - timedelta(days=31)},
        {"Key": "brevitybot-backup-new.json.gz", "LastModified": now - timedelta(days=1)},
    ]}]
    with patch("boto3.client", return_value=s3):
        pruned = brevitybot._upload_and_prune(b"x", "brevitybot-backup-today.json.gz")
    assert pruned == 1
    s3.put_object.assert_called_once()
    s3.delete_object.assert_called_once_with(Bucket=brevitybot.BACKUP_BUCKET, Key="brevitybot-backup-old.json.gz")


def test_restore_roundtrip_and_skip_existing():
    import restore_backup

    class Pipe:
        def __init__(self, r): self.r, self.ops = r, []
        def delete(self, k): self.ops.append(("del", k))
        def set(self, k, v): self.ops.append(("set", k, v))
        def hset(self, k, mapping): self.ops.append(("hset", k, mapping))
        def sadd(self, k, *v): self.ops.append(("sadd", k, set(v)))
        def rpush(self, k, *v): self.ops.append(("rpush", k, list(v)))
        def execute(self):
            for op, k, *a in self.ops:
                if op == "del": self.r.d.pop(k, None)
                elif op == "set": self.r.d[k] = a[0]
                elif op == "hset": self.r.d[k] = dict(a[0])
                elif op == "sadd": self.r.d[k] = a[0]
                elif op == "rpush": self.r.d[k] = a[0]

    class R:
        d = {"post_freq:1": "KEEP"}
        def exists(self, k): return k in self.d
        def pipeline(self, transaction=True): return Pipe(self)

    data = {
        "post_channels": {"type": "hash", "value": {"1": "2"}},
        "disabled_posting": {"type": "set", "value": ["a"]},
        "post_freq:1": {"type": "string", "value": "6"},
        "greenie:1:9": {"type": "list", "value": ["x", "y"]},
    }
    r = R()
    assert restore_backup.restore(r, data, apply=False, overwrite=False) == (3, 1) and "post_channels" not in r.d  # dry run
    assert restore_backup.restore(r, data, apply=True, overwrite=False) == (3, 1)
    assert r.d["post_freq:1"] == "KEEP"  # existing key untouched
    assert r.d["greenie:1:9"] == ["x", "y"] and r.d["post_channels"] == {"1": "2"}
    assert restore_backup.restore(r, data, apply=True, overwrite=True) == (4, 0)
    assert r.d["post_freq:1"] == "6"

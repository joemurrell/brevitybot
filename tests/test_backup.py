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

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

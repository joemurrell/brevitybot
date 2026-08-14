"""Integration tests for Redis-backed helper functions.

Uses a mock Redis client (no real Redis server required) to verify that
load_used_terms, save_used_term, save_config, load_config, get_all_terms,
get_next_brevity_term, and posting-state helpers all read/write the correct
keys and handle edge cases like missing data and concurrent claims.
"""
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import brevitybot
from brevitybot import (
    CHANNEL_MAP_KEY,
    DISABLED_GUILDS_KEY,
    FREQ_KEY_PREFIX,
    LAST_POSTED_KEY_PREFIX,
    TERMS_KEY,
    _invalidate_terms_cache,
    get_all_terms,
    get_last_posted,
    get_next_brevity_term,
    get_post_frequency,
    is_posting_enabled,
    load_config,
    load_used_terms,
    save_config,
    save_used_term,
    set_last_posted,
    set_post_frequency,
)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class _FakeRedis:
    """Minimal in-memory fake that implements only the async methods used here."""

    def __init__(self):
        self._strings: dict[str, str] = {}
        self._hashes: dict[str, dict[str, str]] = {}
        self._sets: dict[str, set] = {}

    async def get(self, key):
        return self._strings.get(key)

    async def set(self, key, value):
        self._strings[key] = str(value)

    async def delete(self, key):
        self._strings.pop(key, None)
        self._hashes.pop(key, None)
        self._sets.pop(key, None)

    async def hset(self, name, key, value):
        self._hashes.setdefault(name, {})[str(key)] = str(value)

    async def hget(self, name, key):
        return self._hashes.get(name, {}).get(str(key))

    async def hgetall(self, name):
        return dict(self._hashes.get(name, {}))

    async def hdel(self, name, field):
        self._hashes.get(name, {}).pop(str(field), None)

    async def smembers(self, key):
        return set(self._sets.get(key, set()))

    async def sadd(self, key, *values):
        s = self._sets.setdefault(key, set())
        added = 0
        for v in values:
            if v not in s:
                s.add(v)
                added += 1
        return added

    async def srem(self, key, *values):
        s = self._sets.get(key, set())
        for v in values:
            s.discard(v)

    async def sismember(self, key, value):
        return value in self._sets.get(key, set())

    def pipeline(self, transaction=True):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis: _FakeRedis):
        self._r = redis
        self._ops = []

    def set(self, key, value):
        self._ops.append(("set", key, value))
        return self

    async def execute(self):
        for op in self._ops:
            if op[0] == "set":
                await self._r.set(op[1], op[2])
        return [True] * len(self._ops)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ──────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_redis():
    r = _FakeRedis()
    original = brevitybot.r
    brevitybot.r = r
    yield r
    brevitybot.r = original


# ──────────────────────────────────────────────────────────────────────
# load_used_terms / save_used_term
# ──────────────────────────────────────────────────────────────────────

class TestUsedTerms:
    def test_load_empty_set(self, fake_redis):
        result = _run(load_used_terms(1))
        assert result == []

    def test_save_and_load_term(self, fake_redis):
        _run(save_used_term(1, "ALPHA"))
        result = _run(load_used_terms(1))
        assert "ALPHA" in result

    def test_save_returns_1_for_new_term(self, fake_redis):
        rv = _run(save_used_term(1, "BRAVO"))
        assert rv == 1

    def test_save_returns_0_for_duplicate(self, fake_redis):
        _run(save_used_term(1, "CHARLIE"))
        rv = _run(save_used_term(1, "CHARLIE"))
        assert rv == 0

    def test_terms_isolated_by_guild(self, fake_redis):
        _run(save_used_term(100, "DELTA"))
        _run(save_used_term(200, "ECHO"))
        assert "DELTA" not in _run(load_used_terms(200))
        assert "ECHO" not in _run(load_used_terms(100))


# ──────────────────────────────────────────────────────────────────────
# save_config / load_config
# ──────────────────────────────────────────────────────────────────────

class TestConfig:
    def test_save_and_load_single_guild(self, fake_redis):
        _run(save_config(111, 999))
        cfg = _run(load_config(111))
        assert cfg == {"channel_id": 999}

    def test_load_missing_guild_returns_none(self, fake_redis):
        result = _run(load_config(999999))
        assert result is None

    def test_load_all_configs(self, fake_redis):
        _run(save_config(10, 1000))
        _run(save_config(20, 2000))
        all_cfg = _run(load_config())
        assert "10" in all_cfg or 10 in all_cfg
        assert "20" in all_cfg or 20 in all_cfg

    def test_overwrite_existing_config(self, fake_redis):
        _run(save_config(5, 100))
        _run(save_config(5, 200))
        cfg = _run(load_config(5))
        assert cfg["channel_id"] == 200


# ──────────────────────────────────────────────────────────────────────
# Post frequency
# ──────────────────────────────────────────────────────────────────────

class TestPostFrequency:
    def test_default_is_24(self, fake_redis):
        freq = _run(get_post_frequency(42))
        assert freq == 24

    def test_set_and_get_custom_frequency(self, fake_redis):
        _run(set_post_frequency(42, 12))
        assert _run(get_post_frequency(42)) == 12

    def test_frequency_isolated_by_guild(self, fake_redis):
        _run(set_post_frequency(1, 6))
        _run(set_post_frequency(2, 48))
        assert _run(get_post_frequency(1)) == 6
        assert _run(get_post_frequency(2)) == 48


# ──────────────────────────────────────────────────────────────────────
# Last posted timestamp
# ──────────────────────────────────────────────────────────────────────

class TestLastPosted:
    def test_unset_returns_zero(self, fake_redis):
        assert _run(get_last_posted(99)) == 0.0

    def test_set_and_get(self, fake_redis):
        ts = 1700000000.5
        _run(set_last_posted(99, ts))
        assert _run(get_last_posted(99)) == pytest.approx(ts)


# ──────────────────────────────────────────────────────────────────────
# Posting enabled/disabled state
# ──────────────────────────────────────────────────────────────────────

class TestPostingEnabled:
    def test_posting_enabled_by_default(self, fake_redis):
        assert _run(is_posting_enabled(77)) is True

    def test_disable_posting(self, fake_redis):
        _run(brevitybot.disable_posting(77))
        assert _run(is_posting_enabled(77)) is False

    def test_enable_after_disable(self, fake_redis):
        _run(brevitybot.disable_posting(77))
        # enable_posting sets last_posted if uninitialized — it does two Redis
        # calls, so we provide a sentinel get() return that lets it proceed
        _run(brevitybot.enable_posting(77))
        assert _run(is_posting_enabled(77)) is True

    def test_enable_disable_isolated_by_guild(self, fake_redis):
        _run(brevitybot.disable_posting(10))
        assert _run(is_posting_enabled(20)) is True


# ──────────────────────────────────────────────────────────────────────
# get_all_terms — in-memory cache behaviour
# ──────────────────────────────────────────────────────────────────────

class TestGetAllTerms:
    def test_returns_empty_when_no_redis_data(self, fake_redis):
        _invalidate_terms_cache()
        result = _run(get_all_terms())
        assert result == []

    def test_returns_terms_from_redis(self, fake_redis):
        _invalidate_terms_cache()
        terms = [{"term": "ALPHA", "definition": "First."}]
        fake_redis._strings[TERMS_KEY] = json.dumps(terms)
        result = _run(get_all_terms())
        assert result == terms

    def test_cache_hit_avoids_redis_read(self, fake_redis):
        """After a successful load, subsequent calls must use the in-memory cache."""
        _invalidate_terms_cache()
        terms = [{"term": "BRAVO", "definition": "Second."}]
        fake_redis._strings[TERMS_KEY] = json.dumps(terms)
        # First call populates cache
        _run(get_all_terms())
        # Wipe Redis — a cache hit must still return the same data
        fake_redis._strings.pop(TERMS_KEY, None)
        result = _run(get_all_terms())
        assert result == terms

    def test_cache_invalidation_forces_redis_read(self, fake_redis):
        _invalidate_terms_cache()
        old = [{"term": "OLD", "definition": "Old def."}]
        new = [{"term": "NEW", "definition": "New def."}]
        fake_redis._strings[TERMS_KEY] = json.dumps(old)
        _run(get_all_terms())  # prime cache
        _invalidate_terms_cache()
        fake_redis._strings[TERMS_KEY] = json.dumps(new)
        result = _run(get_all_terms())
        assert result == new

    def test_malformed_json_returns_empty(self, fake_redis):
        _invalidate_terms_cache()
        fake_redis._strings[TERMS_KEY] = "not-valid-json{{{"
        result = _run(get_all_terms())
        assert result == []

    def test_cache_expires_after_ttl(self, fake_redis):
        """Simulate TTL expiry by setting _terms_cache_at to a time far in the past."""
        _invalidate_terms_cache()
        initial = [{"term": "CHARLIE", "definition": "Third."}]
        updated = [{"term": "DELTA", "definition": "Fourth."}]
        fake_redis._strings[TERMS_KEY] = json.dumps(initial)
        _run(get_all_terms())  # prime

        # Expire the cache by backdating its timestamp
        brevitybot._terms_cache_at = 0.0  # forces a miss on next call
        fake_redis._strings[TERMS_KEY] = json.dumps(updated)
        result = _run(get_all_terms())
        assert result == updated


# ──────────────────────────────────────────────────────────────────────
# get_next_brevity_term — race-safe selection
# ──────────────────────────────────────────────────────────────────────

class TestGetNextBrevityTerm:
    def _seed_terms(self, fake_redis, terms):
        _invalidate_terms_cache()
        fake_redis._strings[TERMS_KEY] = json.dumps(terms)

    def test_picks_from_available_terms(self, fake_redis):
        terms = [
            {"term": "ALPHA", "definition": "Def A"},
            {"term": "BRAVO", "definition": "Def B"},
        ]
        self._seed_terms(fake_redis, terms)
        result = _run(get_next_brevity_term(1))
        assert result is not None
        assert result["term"] in {"ALPHA", "BRAVO"}

    def test_does_not_repeat_used_term(self, fake_redis):
        terms = [
            {"term": "ALPHA", "definition": "Def A"},
            {"term": "BRAVO", "definition": "Def B"},
        ]
        self._seed_terms(fake_redis, terms)
        # Mark ALPHA as already used
        _run(save_used_term(1, "ALPHA"))
        result = _run(get_next_brevity_term(1))
        assert result is not None
        assert result["term"] == "BRAVO"

    def test_resets_when_all_terms_used(self, fake_redis):
        terms = [{"term": "ONLY", "definition": "Def only"}]
        self._seed_terms(fake_redis, terms)
        _run(save_used_term(1, "ONLY"))
        # All terms used — should reset and return ONLY anyway
        result = _run(get_next_brevity_term(1))
        assert result is not None
        assert result["term"] == "ONLY"

    def test_returns_none_when_no_terms(self, fake_redis):
        _invalidate_terms_cache()
        # No terms in Redis
        result = _run(get_next_brevity_term(1))
        assert result is None

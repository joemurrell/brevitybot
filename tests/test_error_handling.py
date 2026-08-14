"""Error-handling tests for BrevityBot.

Covers network/API failures, malformed API responses, timeout scenarios,
and graceful fallback behaviour — all without a real network connection.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import aiohttp
import pytest

import brevitybot
from brevitybot import (
    TERMS_KEY,
    _invalidate_terms_cache,
    get_all_terms,
    get_next_brevity_term,
    get_random_flickr_jet,
)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class _FakeRedis:
    def __init__(self):
        self._strings: dict = {}
        self._sets: dict = {}

    async def get(self, key):
        return self._strings.get(key)

    async def set(self, key, value):
        self._strings[key] = str(value)

    async def smembers(self, key):
        return set(self._sets.get(key, set()))

    async def sadd(self, key, *values):
        s = self._sets.setdefault(key, set())
        added = sum(1 for v in values if v not in s)
        s.update(values)
        return added

    async def delete(self, key):
        self._strings.pop(key, None)

    def pipeline(self, transaction=True):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, r):
        self._r = r
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


@pytest.fixture
def fake_redis():
    r = _FakeRedis()
    original = brevitybot.r
    brevitybot.r = r
    yield r
    brevitybot.r = original


# ──────────────────────────────────────────────────────────────────────
# get_all_terms — Redis error handling
# ──────────────────────────────────────────────────────────────────────

class TestGetAllTermsErrors:
    def test_json_decode_error_returns_empty(self, fake_redis):
        _invalidate_terms_cache()
        fake_redis._strings[TERMS_KEY] = "}{not json"
        result = _run(get_all_terms())
        assert result == []

    def test_empty_redis_value_returns_empty(self, fake_redis):
        _invalidate_terms_cache()
        # Redis returns None for missing key
        result = _run(get_all_terms())
        assert result == []

    def test_null_json_does_not_crash(self, fake_redis):
        _invalidate_terms_cache()
        fake_redis._strings[TERMS_KEY] = "null"
        # json.loads("null") == None; the function returns it without raising
        result = _run(get_all_terms())
        assert not result  # None or empty list — both are falsy


# ──────────────────────────────────────────────────────────────────────
# parse_brevity_terms — network failure scenarios
# ──────────────────────────────────────────────────────────────────────

class TestParseBrevityTermsNetworkErrors:
    """parse_brevity_terms makes real HTTP calls — we mock aiohttp."""

    def _mock_session(self, exc=None, status=200, body=b""):
        """Build an aiohttp.ClientSession mock that either raises or returns body."""
        response_mock = AsyncMock()
        response_mock.status = status
        response_mock.read = AsyncMock(return_value=body)
        # Context managers for session.get(...)
        cm = AsyncMock()
        if exc:
            cm.__aenter__ = AsyncMock(side_effect=exc)
        else:
            cm.__aenter__ = AsyncMock(return_value=response_mock)
        cm.__aexit__ = AsyncMock(return_value=False)

        session_mock = AsyncMock()
        session_mock.get = MagicMock(return_value=cm)

        session_cm = AsyncMock()
        session_cm.__aenter__ = AsyncMock(return_value=session_mock)
        session_cm.__aexit__ = AsyncMock(return_value=False)

        return session_cm

    def test_connection_error_returns_empty(self):
        exc = aiohttp.ClientConnectionError("connection refused")
        with patch("aiohttp.ClientSession", return_value=self._mock_session(exc=exc)):
            result = _run(brevitybot.parse_brevity_terms())
        assert result == []

    def test_timeout_error_returns_empty(self):
        exc = asyncio.TimeoutError()
        with patch("aiohttp.ClientSession", return_value=self._mock_session(exc=exc)):
            result = _run(brevitybot.parse_brevity_terms())
        assert result == []

    def test_non_200_response_attempts_parse(self):
        """A non-200 response still tries to parse whatever body arrived."""
        with patch("aiohttp.ClientSession", return_value=self._mock_session(status=404, body=b"")):
            result = _run(brevitybot.parse_brevity_terms())
        assert result == []

    def test_200_but_empty_body_returns_empty(self):
        with patch("aiohttp.ClientSession", return_value=self._mock_session(status=200, body=b"")):
            result = _run(brevitybot.parse_brevity_terms())
        assert result == []

    def test_200_with_valid_html_returns_terms(self):
        html = (
            b'<html><body><div class="mw-parser-output">'
            b"<dl><dt>ALPHA</dt><dd>Alpha definition.</dd></dl>"
            b"</div></body></html>"
        )
        with patch("aiohttp.ClientSession", return_value=self._mock_session(status=200, body=html)):
            result = _run(brevitybot.parse_brevity_terms())
        assert len(result) == 1
        assert result[0]["term"] == "ALPHA"


# ──────────────────────────────────────────────────────────────────────
# get_random_flickr_jet — API error handling
# ──────────────────────────────────────────────────────────────────────

class TestFlickrErrorHandling:
    def _mock_flickr(self, exc=None, data=None):
        response_mock = AsyncMock()
        response_mock.json = AsyncMock(return_value=data or {})
        cm = AsyncMock()
        if exc:
            cm.__aenter__ = AsyncMock(side_effect=exc)
        else:
            cm.__aenter__ = AsyncMock(return_value=response_mock)
        cm.__aexit__ = AsyncMock(return_value=False)
        session_mock = AsyncMock()
        session_mock.get = MagicMock(return_value=cm)
        session_cm = AsyncMock()
        session_cm.__aenter__ = AsyncMock(return_value=session_mock)
        session_cm.__aexit__ = AsyncMock(return_value=False)
        return session_cm

    def test_network_error_returns_none(self):
        exc = aiohttp.ClientConnectionError("nope")
        with patch("aiohttp.ClientSession", return_value=self._mock_flickr(exc=exc)):
            result = _run(get_random_flickr_jet("fake_key"))
        assert result is None

    def test_timeout_returns_none(self):
        exc = asyncio.TimeoutError()
        with patch("aiohttp.ClientSession", return_value=self._mock_flickr(exc=exc)):
            result = _run(get_random_flickr_jet("fake_key"))
        assert result is None

    def test_empty_photos_list_returns_none(self):
        data = {"photos": {"photo": []}}
        with patch("aiohttp.ClientSession", return_value=self._mock_flickr(data=data)):
            result = _run(get_random_flickr_jet("fake_key"))
        assert result is None

    def test_missing_photos_key_returns_none(self):
        data = {}
        with patch("aiohttp.ClientSession", return_value=self._mock_flickr(data=data)):
            result = _run(get_random_flickr_jet("fake_key"))
        assert result is None

    def test_none_api_key_handled_gracefully(self):
        exc = aiohttp.ClientConnectionError("no network")
        with patch("aiohttp.ClientSession", return_value=self._mock_flickr(exc=exc)):
            result = _run(get_random_flickr_jet(None))
        assert result is None

    def test_valid_response_returns_url(self):
        photo = {"farm": "1", "server": "2", "id": "123", "secret": "abc"}
        data = {"photos": {"photo": [photo]}}
        with patch("aiohttp.ClientSession", return_value=self._mock_flickr(data=data)):
            result = _run(get_random_flickr_jet("fake_key"))
        assert result is not None
        assert "staticflickr.com" in result
        assert "123_abc_b.jpg" in result


# ──────────────────────────────────────────────────────────────────────
# get_next_brevity_term — race / retry handling
# ──────────────────────────────────────────────────────────────────────

class TestGetNextBrevityTermErrors:
    def _seed(self, fake_redis, terms):
        _invalidate_terms_cache()
        fake_redis._strings[TERMS_KEY] = json.dumps(terms)

    def test_returns_none_on_empty_terms(self, fake_redis):
        _invalidate_terms_cache()
        result = _run(get_next_brevity_term(1))
        assert result is None

    def test_race_loss_retries_and_returns_term(self, fake_redis):
        """Simulate a single race-loss (sadd returns 0 once then 1) — the
        function should retry and eventually return a term."""
        terms = [
            {"term": "ALPHA", "definition": "A"},
            {"term": "BRAVO", "definition": "B"},
        ]
        self._seed(fake_redis, terms)

        call_count = [0]
        original_sadd = fake_redis.sadd.__func__ if hasattr(fake_redis.sadd, '__func__') else None

        async def patched_sadd(key, *values):
            call_count[0] += 1
            if call_count[0] == 1:
                # Simulate race loss on first attempt — don't actually add
                return 0
            s = fake_redis._sets.setdefault(key, set())
            added = sum(1 for v in values if v not in s)
            s.update(values)
            return added

        fake_redis.sadd = patched_sadd
        result = _run(get_next_brevity_term(1))
        assert result is not None


# ──────────────────────────────────────────────────────────────────────
# update_brevity_terms — error scenarios
# ──────────────────────────────────────────────────────────────────────

class TestUpdateBrevityTermsErrors:
    def test_empty_parse_result_keeps_existing_terms(self, fake_redis):
        """If parse_brevity_terms returns [] the function bails early without
        overwriting the existing terms in Redis."""
        _invalidate_terms_cache()
        existing = [{"term": "KEPT", "definition": "Retained."}]
        fake_redis._strings[TERMS_KEY] = json.dumps(existing)
        with patch.object(brevitybot, "parse_brevity_terms", new=AsyncMock(return_value=[])):
            result = _run(brevitybot.update_brevity_terms())
        assert result == (0, 0, 0)
        # Existing terms untouched
        stored = json.loads(fake_redis._strings.get(TERMS_KEY, "[]"))
        assert stored == existing

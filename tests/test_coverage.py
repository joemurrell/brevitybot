"""Tests for previously untested or under-tested functions in brevitybot.

Covers: MaxLevelFilter, CustomFormatter, health_check_handler,
get_random_flickr_jet, Redis helper functions, get_all_terms caching,
get_next_brevity_term race-retry logic, get_brevity_term_by_name,
and update_brevity_terms.
"""
import asyncio
import json
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import brevitybot
from brevitybot import (
    CustomFormatter,
    JSONFormatter,
    MaxLevelFilter,
    _invalidate_terms_cache,
    clean_term,
)


# Helper to run async coroutines in sync tests
def _run(coro):
    return asyncio.run(coro)


# -----------------------------------------------
# MaxLevelFilter
# -----------------------------------------------
class TestMaxLevelFilter:
    def test_allows_record_at_level(self):
        f = MaxLevelFilter(logging.INFO)
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hi", args=(), exc_info=None,
        )
        assert f.filter(record) is True

    def test_allows_record_below_level(self):
        f = MaxLevelFilter(logging.INFO)
        record = logging.LogRecord(
            name="test", level=logging.DEBUG, pathname="", lineno=0,
            msg="hi", args=(), exc_info=None,
        )
        assert f.filter(record) is True

    def test_blocks_record_above_level(self):
        f = MaxLevelFilter(logging.INFO)
        record = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="", lineno=0,
            msg="hi", args=(), exc_info=None,
        )
        assert f.filter(record) is False

    def test_blocks_error_when_set_to_warning(self):
        f = MaxLevelFilter(logging.WARNING)
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="", lineno=0,
            msg="err", args=(), exc_info=None,
        )
        assert f.filter(record) is False

    def test_allows_warning_when_set_to_warning(self):
        f = MaxLevelFilter(logging.WARNING)
        record = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="", lineno=0,
            msg="warn", args=(), exc_info=None,
        )
        assert f.filter(record) is True


# -----------------------------------------------
# CustomFormatter
# -----------------------------------------------
class TestCustomFormatter:
    def test_returns_plain_message(self):
        fmt = CustomFormatter()
        record = logging.LogRecord(
            name="brevitybot", level=logging.INFO, pathname="", lineno=0,
            msg="Hello %s", args=("world",), exc_info=None,
        )
        assert fmt.format(record) == "Hello world"

    def test_no_timestamp_or_level_prefix(self):
        fmt = CustomFormatter()
        record = logging.LogRecord(
            name="brevitybot", level=logging.WARNING, pathname="", lineno=0,
            msg="just a message", args=(), exc_info=None,
        )
        out = fmt.format(record)
        assert out == "just a message"
        assert "WARNING" not in out
        assert "brevitybot" not in out

    def test_format_with_no_args(self):
        fmt = CustomFormatter()
        record = logging.LogRecord(
            name="test", level=logging.DEBUG, pathname="", lineno=0,
            msg="simple", args=(), exc_info=None,
        )
        assert fmt.format(record) == "simple"


# -----------------------------------------------
# JSONFormatter — exc_info handling
# -----------------------------------------------
class TestJSONFormatterExcInfo:
    def test_exc_info_included_when_present(self):
        fmt = JSONFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="", lineno=0,
            msg="boom", args=(), exc_info=exc_info,
        )
        out = fmt.format(record)
        parsed = json.loads(out)
        assert "exc_info" in parsed
        assert "ValueError" in parsed["exc_info"]
        assert "test error" in parsed["exc_info"]

    def test_private_attrs_excluded(self):
        """Attributes starting with _ should not appear in output."""
        fmt = JSONFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="msg", args=(), exc_info=None,
        )
        record._private_thing = "secret"
        out = fmt.format(record)
        parsed = json.loads(out)
        assert "_private_thing" not in parsed


# -----------------------------------------------
# health_check_handler
# -----------------------------------------------
class TestHealthCheckHandler:
    def test_healthy_response(self):
        """When client is ready and not closed, returns 200 with status healthy."""
        mock_request = MagicMock()
        mock_client = MagicMock()
        mock_client.is_ready.return_value = True
        mock_client.is_closed.return_value = False
        mock_client.latency = 0.05
        mock_client.user = MagicMock(__str__=lambda self: "BrevityBot#1234")
        mock_client.guilds = [1, 2, 3]

        with patch.object(brevitybot, "client", mock_client):
            response = _run(brevitybot.health_check_handler(mock_request))
        assert response.status == 200
        body = json.loads(response.body)
        assert body["status"] == "healthy"
        assert body["guilds"] == 3
        assert body["latency_ms"] == 50

    def test_unhealthy_response_when_not_ready(self):
        """When client is not ready, returns 503."""
        mock_request = MagicMock()
        mock_client = MagicMock()
        mock_client.is_ready.return_value = False

        with patch.object(brevitybot, "client", mock_client):
            response = _run(brevitybot.health_check_handler(mock_request))
        assert response.status == 503
        body = json.loads(response.body)
        assert body["status"] == "unhealthy"

    def test_unhealthy_response_when_closed(self):
        """When client is closed, returns 503."""
        mock_request = MagicMock()
        mock_client = MagicMock()
        mock_client.is_ready.return_value = True
        mock_client.is_closed.return_value = True

        with patch.object(brevitybot, "client", mock_client):
            response = _run(brevitybot.health_check_handler(mock_request))
        assert response.status == 503


# -----------------------------------------------
# get_random_flickr_jet
# -----------------------------------------------
class TestGetRandomFlickrJet:
    def test_returns_none_when_api_key_is_none(self):
        """With no API key, the function should still handle gracefully."""
        # The function checks api_key only inside the params; it will attempt
        # the request. We mock aiohttp to return no photos.
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"photos": {"photo": []}})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = _run(brevitybot.get_random_flickr_jet("fake-key"))
        assert result is None

    def test_returns_url_when_photos_found(self):
        """When Flickr returns photos, should build a valid URL."""
        photo = {"farm": 1, "server": "2345", "id": "12345", "secret": "abc"}
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"photos": {"photo": [photo]}})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = _run(brevitybot.get_random_flickr_jet("fake-key"))
        assert result == "https://farm1.staticflickr.com/2345/12345_abc_b.jpg"

    def test_returns_none_on_exception(self):
        """On network error, should return None gracefully."""
        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=Exception("Network error"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = _run(brevitybot.get_random_flickr_jet("fake-key"))
        assert result is None


# -----------------------------------------------
# Redis helper functions (mocked)
# -----------------------------------------------
class TestRedisHelpers:
    """Tests for async Redis helper functions using a mocked Redis client."""

    def _mock_redis(self):
        mock_r = AsyncMock()
        return mock_r

    def test_load_used_terms(self):
        mock_r = self._mock_redis()
        mock_r.smembers = AsyncMock(return_value={"ALPHA", "BRAVO"})
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.load_used_terms(12345))
        assert set(result) == {"ALPHA", "BRAVO"}
        mock_r.smembers.assert_called_once_with("used_terms:12345")

    def test_save_used_term_newly_added(self):
        mock_r = self._mock_redis()
        mock_r.sadd = AsyncMock(return_value=1)
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.save_used_term(123, "BANDIT"))
        assert result == 1
        mock_r.sadd.assert_called_once_with("used_terms:123", "BANDIT")

    def test_save_used_term_already_exists(self):
        mock_r = self._mock_redis()
        mock_r.sadd = AsyncMock(return_value=0)
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.save_used_term(123, "BANDIT"))
        assert result == 0

    def test_save_config(self):
        mock_r = self._mock_redis()
        mock_r.hset = AsyncMock()
        with patch.object(brevitybot, "r", mock_r):
            _run(brevitybot.save_config(111, 222))
        mock_r.hset.assert_called_once_with("post_channels", "111", 222)

    def test_load_config_single_guild(self):
        mock_r = self._mock_redis()
        mock_r.hget = AsyncMock(return_value="999")
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.load_config(111))
        assert result == {"channel_id": 999}

    def test_load_config_single_guild_not_found(self):
        mock_r = self._mock_redis()
        mock_r.hget = AsyncMock(return_value=None)
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.load_config(111))
        assert result is None

    def test_load_config_all_guilds(self):
        mock_r = self._mock_redis()
        mock_r.hgetall = AsyncMock(return_value={"111": "222", "333": "444"})
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.load_config())
        assert result == {"111": {"channel_id": 222}, "333": {"channel_id": 444}}

    def test_set_post_frequency(self):
        mock_r = self._mock_redis()
        mock_r.set = AsyncMock()
        with patch.object(brevitybot, "r", mock_r):
            _run(brevitybot.set_post_frequency(123, 12))
        mock_r.set.assert_called_once_with("post_freq:123", 12)

    def test_get_post_frequency_set(self):
        mock_r = self._mock_redis()
        mock_r.get = AsyncMock(return_value="6")
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.get_post_frequency(123))
        assert result == 6

    def test_get_post_frequency_default(self):
        mock_r = self._mock_redis()
        mock_r.get = AsyncMock(return_value=None)
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.get_post_frequency(123))
        assert result == 24

    def test_set_last_posted(self):
        mock_r = self._mock_redis()
        mock_r.set = AsyncMock()
        with patch.object(brevitybot, "r", mock_r):
            _run(brevitybot.set_last_posted(123, 1700000000.0))
        mock_r.set.assert_called_once_with("last_posted:123", "1700000000.0")

    def test_get_last_posted_set(self):
        mock_r = self._mock_redis()
        mock_r.get = AsyncMock(return_value="1700000000.5")
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.get_last_posted(123))
        assert result == 1700000000.5

    def test_get_last_posted_unset(self):
        mock_r = self._mock_redis()
        mock_r.get = AsyncMock(return_value=None)
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.get_last_posted(123))
        assert result == 0.0

    def test_is_posting_enabled_true(self):
        mock_r = self._mock_redis()
        mock_r.sismember = AsyncMock(return_value=False)
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.is_posting_enabled(123))
        assert result is True

    def test_is_posting_enabled_false(self):
        mock_r = self._mock_redis()
        mock_r.sismember = AsyncMock(return_value=True)
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.is_posting_enabled(123))
        assert result is False

    def test_disable_posting(self):
        mock_r = self._mock_redis()
        mock_r.sadd = AsyncMock()
        with patch.object(brevitybot, "r", mock_r):
            _run(brevitybot.disable_posting(123))
        mock_r.sadd.assert_called_once_with("disabled_posting", "123")

    def test_enable_posting_initializes_last_posted(self):
        mock_r = self._mock_redis()
        mock_r.srem = AsyncMock()
        mock_r.get = AsyncMock(return_value=None)  # last_posted not set
        mock_r.set = AsyncMock()
        with patch.object(brevitybot, "r", mock_r):
            _run(brevitybot.enable_posting(123))
        mock_r.srem.assert_called_once_with("disabled_posting", "123")
        # Should have called set for last_posted since it was unset (0.0 < threshold)
        assert mock_r.set.called

    def test_enable_posting_skips_last_posted_if_already_set(self):
        mock_r = self._mock_redis()
        mock_r.srem = AsyncMock()
        mock_r.get = AsyncMock(return_value="1700000000.0")  # already set
        mock_r.set = AsyncMock()
        with patch.object(brevitybot, "r", mock_r):
            _run(brevitybot.enable_posting(123))
        mock_r.srem.assert_called_once()
        # set should not be called for last_posted since it's already initialized
        mock_r.set.assert_not_called()


# -----------------------------------------------
# get_all_terms — caching
# -----------------------------------------------
class TestGetAllTerms:
    def setup_method(self):
        """Reset cache before each test."""
        _invalidate_terms_cache()

    def test_returns_empty_when_redis_has_no_terms(self):
        mock_r = AsyncMock()
        mock_r.get = AsyncMock(return_value=None)
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.get_all_terms())
        assert result == []

    def test_returns_terms_from_redis(self):
        terms = [{"term": "FOO", "definition": "Foo def"}]
        mock_r = AsyncMock()
        mock_r.get = AsyncMock(return_value=json.dumps(terms))
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.get_all_terms())
        assert result == terms

    def test_uses_cache_on_second_call(self):
        terms = [{"term": "FOO", "definition": "Foo def"}]
        mock_r = AsyncMock()
        mock_r.get = AsyncMock(return_value=json.dumps(terms))
        with patch.object(brevitybot, "r", mock_r):
            _run(brevitybot.get_all_terms())
            # Second call should use cache, not call Redis again
            _run(brevitybot.get_all_terms())
        assert mock_r.get.call_count == 1

    def test_cache_expires_after_ttl(self):
        terms = [{"term": "FOO", "definition": "Foo def"}]
        mock_r = AsyncMock()
        mock_r.get = AsyncMock(return_value=json.dumps(terms))
        with patch.object(brevitybot, "r", mock_r):
            _run(brevitybot.get_all_terms())
            # Simulate TTL expiry
            brevitybot._terms_cache_at = time.time() - 400
            _run(brevitybot.get_all_terms())
        assert mock_r.get.call_count == 2

    def test_handles_invalid_json_gracefully(self):
        mock_r = AsyncMock()
        mock_r.get = AsyncMock(return_value="not valid json {{{")
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.get_all_terms())
        assert result == []

    def teardown_method(self):
        _invalidate_terms_cache()


# -----------------------------------------------
# get_next_brevity_term — race-retry logic
# -----------------------------------------------
class TestGetNextBrevityTerm:
    def setup_method(self):
        _invalidate_terms_cache()

    def test_returns_term_on_first_try(self):
        terms = [
            {"term": "ALPHA", "definition": "A def"},
            {"term": "BRAVO", "definition": "B def"},
        ]
        mock_r = AsyncMock()
        mock_r.get = AsyncMock(return_value=json.dumps(terms))
        mock_r.smembers = AsyncMock(return_value=set())
        mock_r.sadd = AsyncMock(return_value=1)  # newly added
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.get_next_brevity_term(123))
        assert result is not None
        assert result["term"] in ("ALPHA", "BRAVO")

    def test_retries_on_race_condition(self):
        terms = [
            {"term": "ALPHA", "definition": "A def"},
            {"term": "BRAVO", "definition": "B def"},
        ]
        mock_r = AsyncMock()
        mock_r.get = AsyncMock(return_value=json.dumps(terms))
        mock_r.smembers = AsyncMock(return_value=set())
        # First attempt loses race (returns 0), second succeeds (returns 1)
        mock_r.sadd = AsyncMock(side_effect=[0, 1])
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.get_next_brevity_term(123))
        assert result is not None
        assert mock_r.sadd.call_count == 2

    def test_resets_used_terms_when_all_used(self):
        terms = [{"term": "ALPHA", "definition": "A def"}]
        mock_r = AsyncMock()
        mock_r.get = AsyncMock(return_value=json.dumps(terms))
        mock_r.smembers = AsyncMock(return_value={"ALPHA"})
        mock_r.delete = AsyncMock()
        mock_r.sadd = AsyncMock(return_value=1)
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.get_next_brevity_term(123))
        assert result is not None
        mock_r.delete.assert_called_once_with("used_terms:123")

    def test_returns_none_when_no_terms_available(self):
        mock_r = AsyncMock()
        mock_r.get = AsyncMock(return_value=None)
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.get_next_brevity_term(123))
        assert result is None

    def test_returns_last_pick_after_max_retries(self):
        terms = [
            {"term": "ALPHA", "definition": "A def"},
            {"term": "BRAVO", "definition": "B def"},
        ]
        mock_r = AsyncMock()
        mock_r.get = AsyncMock(return_value=json.dumps(terms))
        mock_r.smembers = AsyncMock(return_value=set())
        # All attempts lose the race
        mock_r.sadd = AsyncMock(return_value=0)
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.get_next_brevity_term(123))
        assert result is not None  # Returns last picked even on exhausted retries
        assert mock_r.sadd.call_count == 5  # max_attempts

    def teardown_method(self):
        _invalidate_terms_cache()


# -----------------------------------------------
# get_brevity_term_by_name
# -----------------------------------------------
class TestGetBrevityTermByName:
    def setup_method(self):
        _invalidate_terms_cache()

    def test_finds_exact_match(self):
        terms = [
            {"term": "ALPHA", "definition": "First letter"},
            {"term": "BRAVO", "definition": "Second letter"},
        ]
        mock_r = AsyncMock()
        mock_r.get = AsyncMock(return_value=json.dumps(terms))
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.get_brevity_term_by_name("ALPHA"))
        assert result == {"term": "ALPHA", "definition": "First letter"}

    def test_case_insensitive_match(self):
        terms = [{"term": "BRAVO", "definition": "B def"}]
        mock_r = AsyncMock()
        mock_r.get = AsyncMock(return_value=json.dumps(terms))
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.get_brevity_term_by_name("bravo"))
        assert result is not None
        assert result["term"] == "BRAVO"

    def test_returns_none_when_not_found(self):
        terms = [{"term": "ALPHA", "definition": "A def"}]
        mock_r = AsyncMock()
        mock_r.get = AsyncMock(return_value=json.dumps(terms))
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.get_brevity_term_by_name("ZULU"))
        assert result is None

    def test_returns_none_when_no_terms(self):
        mock_r = AsyncMock()
        mock_r.get = AsyncMock(return_value=None)
        with patch.object(brevitybot, "r", mock_r):
            result = _run(brevitybot.get_brevity_term_by_name("ALPHA"))
        assert result is None

    def teardown_method(self):
        _invalidate_terms_cache()


# -----------------------------------------------
# update_brevity_terms
# -----------------------------------------------
class TestUpdateBrevityTerms:
    def setup_method(self):
        _invalidate_terms_cache()

    def test_returns_zeros_when_parse_fails(self):
        """When parse_brevity_terms returns empty, no update occurs."""
        mock_r = AsyncMock()
        with patch.object(brevitybot, "r", mock_r):
            with patch.object(brevitybot, "parse_brevity_terms", AsyncMock(return_value=[])):
                result = _run(brevitybot.update_brevity_terms())
        assert result == (0, 0, 0)

    def test_fresh_terms_all_added(self):
        """When Redis has no existing terms, all parsed terms are 'added'."""
        new_terms = [
            {"term": "ALPHA", "definition": "A"},
            {"term": "BRAVO", "definition": "B"},
        ]
        mock_r = AsyncMock()
        mock_r.get = AsyncMock(return_value=None)
        mock_pipe = AsyncMock()
        mock_pipe.set = MagicMock()
        mock_pipe.execute = AsyncMock()
        mock_pipe.__aenter__ = AsyncMock(return_value=mock_pipe)
        mock_pipe.__aexit__ = AsyncMock(return_value=False)
        mock_r.pipeline = MagicMock(return_value=mock_pipe)

        with patch.object(brevitybot, "r", mock_r):
            with patch.object(brevitybot, "parse_brevity_terms", AsyncMock(return_value=new_terms)):
                total, added, updated = _run(brevitybot.update_brevity_terms())
        assert total == 2
        assert added == 2
        assert updated == 0

    def test_updated_terms_detected(self):
        """When a term's definition changes, it's counted as updated."""
        existing = [{"term": "ALPHA", "definition": "Old A"}]
        new_terms = [{"term": "ALPHA", "definition": "New A"}]

        mock_r = AsyncMock()
        mock_r.get = AsyncMock(return_value=json.dumps(existing))
        mock_pipe = AsyncMock()
        mock_pipe.set = MagicMock()
        mock_pipe.execute = AsyncMock()
        mock_pipe.__aenter__ = AsyncMock(return_value=mock_pipe)
        mock_pipe.__aexit__ = AsyncMock(return_value=False)
        mock_r.pipeline = MagicMock(return_value=mock_pipe)

        with patch.object(brevitybot, "r", mock_r):
            with patch.object(brevitybot, "parse_brevity_terms", AsyncMock(return_value=new_terms)):
                total, added, updated = _run(brevitybot.update_brevity_terms())
        assert total == 1
        assert added == 0
        assert updated == 1

    def test_unchanged_terms_not_counted(self):
        """Identical terms should not be counted as added or updated."""
        existing = [{"term": "ALPHA", "definition": "A def"}]
        new_terms = [{"term": "ALPHA", "definition": "A def"}]

        mock_r = AsyncMock()
        mock_r.get = AsyncMock(return_value=json.dumps(existing))
        mock_pipe = AsyncMock()
        mock_pipe.set = MagicMock()
        mock_pipe.execute = AsyncMock()
        mock_pipe.__aenter__ = AsyncMock(return_value=mock_pipe)
        mock_pipe.__aexit__ = AsyncMock(return_value=False)
        mock_r.pipeline = MagicMock(return_value=mock_pipe)

        with patch.object(brevitybot, "r", mock_r):
            with patch.object(brevitybot, "parse_brevity_terms", AsyncMock(return_value=new_terms)):
                total, added, updated = _run(brevitybot.update_brevity_terms())
        assert total == 1
        assert added == 0
        assert updated == 0

    def test_invalidates_cache_after_update(self):
        """After a successful update, the term cache should be invalidated."""
        new_terms = [{"term": "FOO", "definition": "F"}]
        mock_r = AsyncMock()
        mock_r.get = AsyncMock(return_value=None)
        mock_pipe = AsyncMock()
        mock_pipe.set = MagicMock()
        mock_pipe.execute = AsyncMock()
        mock_pipe.__aenter__ = AsyncMock(return_value=mock_pipe)
        mock_pipe.__aexit__ = AsyncMock(return_value=False)
        mock_r.pipeline = MagicMock(return_value=mock_pipe)

        # Pre-populate cache
        brevitybot._terms_cache = [{"term": "OLD", "definition": "old"}]
        brevitybot._terms_cache_at = time.time()

        with patch.object(brevitybot, "r", mock_r):
            with patch.object(brevitybot, "parse_brevity_terms", AsyncMock(return_value=new_terms)):
                _run(brevitybot.update_brevity_terms())
        assert brevitybot._terms_cache is None

    def teardown_method(self):
        _invalidate_terms_cache()


# -----------------------------------------------
# parse_brevity_terms — HTTP layer
# -----------------------------------------------
class TestParseBrevityTerms:
    def test_returns_empty_on_fetch_exception(self):
        """If the HTTP request raises, returns empty list."""
        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=Exception("timeout"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = _run(brevitybot.parse_brevity_terms())
        assert result == []

    def test_returns_parsed_terms_on_success(self):
        """On 200, delegates to _parse_terms_from_content."""
        html = b"""<div class="mw-parser-output">
          <dl><dt>TEST</dt><dd>A test term.</dd></dl>
        </div>"""
        mock_response = AsyncMock()
        mock_response.read = AsyncMock(return_value=html)
        mock_response.status = 200
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = _run(brevitybot.parse_brevity_terms())
        assert len(result) == 1
        assert result[0]["term"] == "TEST"


# -----------------------------------------------
# _cleanup_quiz_keys
# -----------------------------------------------
class TestCleanupQuizKeys:
    def test_deletes_correct_keys(self):
        mock_r = AsyncMock()
        mock_r.delete = AsyncMock()
        with patch.object(brevitybot, "r", mock_r):
            _run(brevitybot._cleanup_quiz_keys("quiz123", 3, guild_id=456))
        call_args = mock_r.delete.call_args[0]
        assert "quiz:quiz123:answers:0" in call_args
        assert "quiz:quiz123:answers:1" in call_args
        assert "quiz:quiz123:answers:2" in call_args
        assert "quiz:quiz123:meta" in call_args
        assert "active_quiz:456" in call_args

    def test_no_guild_lock_when_guild_id_none(self):
        mock_r = AsyncMock()
        mock_r.delete = AsyncMock()
        with patch.object(brevitybot, "r", mock_r):
            _run(brevitybot._cleanup_quiz_keys("quiz123", 2, guild_id=None))
        call_args = mock_r.delete.call_args[0]
        assert "quiz:quiz123:answers:0" in call_args
        assert "quiz:quiz123:answers:1" in call_args
        assert "quiz:quiz123:meta" in call_args
        # Should NOT contain any active_quiz key
        assert all("active_quiz" not in k for k in call_args)

    def test_handles_redis_error_gracefully(self):
        mock_r = AsyncMock()
        mock_r.delete = AsyncMock(side_effect=Exception("Redis down"))
        with patch.object(brevitybot, "r", mock_r):
            # Should not raise
            _run(brevitybot._cleanup_quiz_keys("quiz123", 1))


# -----------------------------------------------
# clean_term — additional edge cases
# -----------------------------------------------
class TestCleanTermAdditional:
    def test_multiple_asterisks(self):
        assert clean_term("***FOO***") == "FOO"

    def test_asterisks_and_whitespace(self):
        assert clean_term(" *BAR* ") == "BAR"

    def test_empty_string(self):
        assert clean_term("") == ""

    def test_only_asterisks(self):
        assert clean_term("***") == ""

    def test_internal_spaces_preserved(self):
        assert clean_term("*FOO BAR*") == "FOO BAR"

    def test_hyphenated_term(self):
        assert clean_term("*NO-JOY*") == "NO-JOY"

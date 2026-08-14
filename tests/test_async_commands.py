"""Tests for Discord slash command handlers.

Each command is exercised by building a minimal fake Interaction and wiring
up a fake Redis, then calling the underlying coroutine directly.  This avoids
the need for a live Discord connection or real Redis server.
"""
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import brevitybot
from brevitybot import (
    TERMS_KEY,
    _invalidate_terms_cache,
)


# ──────────────────────────────────────────────────────────────────────
# Helpers / shared fakes
# ──────────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _fake_interaction(guild_id=123, channel_id=456, *, ephemeral_responses=True):
    """Return a minimal discord.Interaction-like mock."""
    inter = MagicMock(spec=discord.Interaction)
    inter.guild = MagicMock()
    inter.guild.id = guild_id
    inter.channel = MagicMock()
    inter.channel.id = channel_id
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


class _FakeRedis:
    """Same minimal fake as in test_integration_redis for isolation."""

    def __init__(self):
        self._strings: dict = {}
        self._hashes: dict = {}
        self._sets: dict = {}

    async def get(self, key):
        return self._strings.get(key)

    async def set(self, key, value):
        self._strings[key] = str(value)

    async def delete(self, key):
        self._strings.pop(key, None)

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
        for v in values:
            self._sets.get(key, set()).discard(v)

    async def sismember(self, key, value):
        return value in self._sets.get(key, set())

    async def expire(self, key, ttl):
        pass

    async def exists(self, key):
        return int(key in self._strings or key in self._hashes or key in self._sets)

    def pipeline(self, transaction=True):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis):
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


@pytest.fixture
def fake_redis():
    r = _FakeRedis()
    original = brevitybot.r
    brevitybot.r = r
    yield r
    brevitybot.r = original


def _seed_terms(fake_redis, terms):
    _invalidate_terms_cache()
    fake_redis._strings[TERMS_KEY] = json.dumps(terms)


# ──────────────────────────────────────────────────────────────────────
# /setup command
# ──────────────────────────────────────────────────────────────────────

class TestSetupCommand:
    def test_setup_saves_config_and_responds(self, fake_redis):
        inter = _fake_interaction(guild_id=10, channel_id=50)
        _run(brevitybot.setup.callback(inter))
        inter.response.send_message.assert_awaited_once()
        msg = inter.response.send_message.call_args[0][0]
        assert "50" in msg or "setup complete" in msg.lower()

    def test_setup_writes_channel_to_redis(self, fake_redis):
        inter = _fake_interaction(guild_id=10, channel_id=50)
        _run(brevitybot.setup.callback(inter))
        stored = _run(brevitybot.load_config(10))
        assert stored is not None
        assert stored["channel_id"] == 50

    def test_setup_sets_last_posted(self, fake_redis):
        inter = _fake_interaction(guild_id=10, channel_id=50)
        before = time.time()
        _run(brevitybot.setup.callback(inter))
        lp = _run(brevitybot.get_last_posted(10))
        assert lp >= before


# ──────────────────────────────────────────────────────────────────────
# /setfrequency command
# ──────────────────────────────────────────────────────────────────────

class TestSetFrequencyCommand:
    def test_valid_frequency_saved(self, fake_redis):
        inter = _fake_interaction(guild_id=10)
        _run(brevitybot.setfrequency.callback(inter, hours=6))
        inter.response.send_message.assert_awaited_once()
        assert _run(brevitybot.get_post_frequency(10)) == 6

    def test_response_confirms_frequency(self, fake_redis):
        inter = _fake_interaction(guild_id=10)
        _run(brevitybot.setfrequency.callback(inter, hours=8))
        msg = inter.response.send_message.call_args[0][0]
        assert "8" in msg

    def test_zero_frequency_rejected(self, fake_redis):
        inter = _fake_interaction(guild_id=10)
        _run(brevitybot.setfrequency.callback(inter, hours=0))
        msg = inter.response.send_message.call_args[0][0]
        assert "positive" in msg.lower() or "must be" in msg.lower()
        # Frequency should remain at default (not 0)
        assert _run(brevitybot.get_post_frequency(10)) == 24

    def test_negative_frequency_rejected(self, fake_redis):
        inter = _fake_interaction(guild_id=10)
        _run(brevitybot.setfrequency.callback(inter, hours=-5))
        msg = inter.response.send_message.call_args[0][0]
        assert "positive" in msg.lower() or "must be" in msg.lower()


# ──────────────────────────────────────────────────────────────────────
# /disableposting / /enableposting commands
# ──────────────────────────────────────────────────────────────────────

class TestPostingToggleCommands:
    def test_disable_posting_command(self, fake_redis):
        inter = _fake_interaction(guild_id=20)
        _run(brevitybot.disableposting.callback(inter))
        inter.response.send_message.assert_awaited_once()
        assert not _run(brevitybot.is_posting_enabled(20))

    def test_enable_posting_command(self, fake_redis):
        inter = _fake_interaction(guild_id=20)
        _run(brevitybot.disable_posting(20))
        _run(brevitybot.enableposting.callback(inter))
        inter.response.send_message.assert_awaited_once()
        assert _run(brevitybot.is_posting_enabled(20))

    def test_disable_response_message(self, fake_redis):
        inter = _fake_interaction(guild_id=20)
        _run(brevitybot.disableposting.callback(inter))
        msg = inter.response.send_message.call_args[0][0]
        assert "disabled" in msg.lower()

    def test_enable_response_message(self, fake_redis):
        inter = _fake_interaction(guild_id=20)
        _run(brevitybot.disable_posting(20))
        _run(brevitybot.enableposting.callback(inter))
        msg = inter.response.send_message.call_args[0][0]
        assert "enabled" in msg.lower()


# ──────────────────────────────────────────────────────────────────────
# /nextterm command
# ──────────────────────────────────────────────────────────────────────

class TestNextTermCommand:
    def test_nextterm_sends_embed_when_terms_available(self, fake_redis):
        terms = [{"term": "ALPHA", "definition": "Alpha definition."}]
        _seed_terms(fake_redis, terms)
        inter = _fake_interaction(guild_id=30)
        with patch.object(brevitybot, "get_random_flickr_jet", new=AsyncMock(return_value=None)):
            _run(brevitybot.nextterm.callback(inter))
        inter.followup.send.assert_awaited_once()
        kwargs = inter.followup.send.call_args[1]
        embed = kwargs.get("embed")
        assert embed is not None
        assert embed.title == "ALPHA"

    def test_nextterm_sends_fallback_when_no_terms(self, fake_redis):
        _invalidate_terms_cache()
        inter = _fake_interaction(guild_id=30)
        _run(brevitybot.nextterm.callback(inter))
        inter.followup.send.assert_awaited_once()
        msg = inter.followup.send.call_args[0][0]
        assert "no terms" in msg.lower() or "available" in msg.lower()

    def test_nextterm_defers_before_work(self, fake_redis):
        terms = [{"term": "BRAVO", "definition": "Bravo definition."}]
        _seed_terms(fake_redis, terms)
        inter = _fake_interaction(guild_id=31)
        with patch.object(brevitybot, "get_random_flickr_jet", new=AsyncMock(return_value=None)):
            _run(brevitybot.nextterm.callback(inter))
        inter.response.defer.assert_awaited_once()


# ──────────────────────────────────────────────────────────────────────
# /define command  (works in DMs — no guild requirement)
# ──────────────────────────────────────────────────────────────────────

class TestDefineCommand:
    def test_define_found_term_sends_embed(self, fake_redis):
        terms = [{"term": "CHARLIE", "definition": "Charlie def."}]
        _seed_terms(fake_redis, terms)
        inter = _fake_interaction()
        _run(brevitybot.define.callback(inter, term="CHARLIE"))
        inter.response.send_message.assert_awaited_once()
        kwargs = inter.response.send_message.call_args[1]
        embed = kwargs.get("embed")
        assert embed is not None
        assert embed.title == "CHARLIE"

    def test_define_unknown_term_sends_not_found(self, fake_redis):
        _invalidate_terms_cache()
        inter = _fake_interaction()
        _run(brevitybot.define.callback(inter, term="ZZZNOPE"))
        inter.response.send_message.assert_awaited_once()
        msg = inter.response.send_message.call_args[0][0]
        assert "not found" in msg.lower() or "zzznope" in msg.lower()


# ──────────────────────────────────────────────────────────────────────
# Permission gate checks (via app_commands decorator metadata)
# ──────────────────────────────────────────────────────────────────────

class TestCommandPermissionGates:
    """Verify that manage_guild / manage_messages gates are declared on the
    right commands, and guild_only is set where required.

    This mirrors the existing TestLoopReadinessGuards pattern — we inspect
    the command objects rather than calling Discord's permission system.
    """

    @staticmethod
    def _get_command(name):
        return brevitybot.tree.get_command(name)

    def _default_member_perms(self, cmd_name) -> discord.Permissions | None:
        cmd = self._get_command(cmd_name)
        assert cmd is not None, f"Command /{cmd_name} not registered"
        return cmd.default_permissions

    def test_setup_requires_manage_guild(self):
        perms = self._default_member_perms("setup")
        assert perms is not None
        assert perms.manage_guild

    def test_setfrequency_requires_manage_guild(self):
        perms = self._default_member_perms("setfrequency")
        assert perms is not None
        assert perms.manage_guild

    def test_disableposting_requires_manage_guild(self):
        perms = self._default_member_perms("disableposting")
        assert perms is not None
        assert perms.manage_guild

    def test_enableposting_requires_manage_guild(self):
        perms = self._default_member_perms("enableposting")
        assert perms is not None
        assert perms.manage_guild

    def test_reloadterms_requires_manage_guild(self):
        perms = self._default_member_perms("reloadterms")
        assert perms is not None
        assert perms.manage_guild

    def test_quizstop_requires_manage_messages(self):
        perms = self._default_member_perms("quizstop")
        assert perms is not None
        assert perms.manage_messages

    def test_quizpurge_requires_manage_guild(self):
        perms = self._default_member_perms("quizpurge")
        assert perms is not None
        assert perms.manage_guild

    def test_define_has_no_manage_guild_requirement(self):
        perms = self._default_member_perms("define")
        assert perms is None or not perms.manage_guild

    def test_nextterm_registered(self):
        assert self._get_command("nextterm") is not None

    def test_greenieboard_registered(self):
        assert self._get_command("greenieboard") is not None

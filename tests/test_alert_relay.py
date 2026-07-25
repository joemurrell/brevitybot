"""Tests for alert_relay.py's pure parsing/filtering/formatting helpers.

No network, no aiohttp server — these exercise the functions that decide
what counts as an alert and how it gets shaped into a Discord payload.
"""
import json

from alert_relay import (
    ALERT_LEVELS,
    MAX_EMBEDS_PER_ALERT,
    build_discord_embed,
    build_discord_payloads,
    extract_level,
    parse_log_payload,
    should_alert,
)


class TestParseLogPayload:
    def test_parses_json_array(self):
        body = json.dumps([{"message": "a"}, {"message": "b"}]).encode()
        assert parse_log_payload(body) == [{"message": "a"}, {"message": "b"}]

    def test_parses_single_json_object(self):
        body = json.dumps({"message": "a"}).encode()
        assert parse_log_payload(body) == [{"message": "a"}]

    def test_parses_json_lines(self):
        body = b'{"message": "a"}\n{"message": "b"}\n'
        assert parse_log_payload(body) == [{"message": "a"}, {"message": "b"}]

    def test_skips_unparseable_lines_in_jsonl(self):
        body = b'{"message": "a"}\nnot json\n{"message": "b"}\n'
        assert parse_log_payload(body) == [{"message": "a"}, {"message": "b"}]

    def test_empty_body_returns_empty_list(self):
        assert parse_log_payload(b"") == []
        assert parse_log_payload(b"   ") == []

    def test_non_dict_array_entries_are_dropped(self):
        body = json.dumps([{"message": "a"}, "not a dict", 42]).encode()
        assert parse_log_payload(body) == [{"message": "a"}]


class TestExtractLevel:
    def test_extracts_and_uppercases_level(self):
        assert extract_level({"level": "warning"}) == "WARNING"
        assert extract_level({"level": "Error"}) == "ERROR"

    def test_missing_level_returns_none(self):
        assert extract_level({"message": "no level field"}) is None

    def test_blank_level_returns_none(self):
        assert extract_level({"level": "  "}) is None

    def test_does_not_fall_back_to_severity(self):
        # `severity` is Railway's own (unreliable for unstructured apps)
        # classification, not the app-reported level. Must not be used.
        assert extract_level({"severity": "error", "message": "x"}) is None


class TestShouldAlert:
    def test_warning_and_above_alert(self):
        for level in ("WARNING", "WARN", "ERROR", "ERR", "CRITICAL", "FATAL"):
            assert should_alert({"level": level}) is True

    def test_info_and_debug_do_not_alert(self):
        for level in ("INFO", "DEBUG", "TRACE"):
            assert should_alert({"level": level}) is False

    def test_missing_level_does_not_alert(self):
        assert should_alert({"message": "no level"}) is False

    def test_all_alert_levels_recognized(self):
        assert ALERT_LEVELS == {"WARNING", "WARN", "ERROR", "ERR", "CRITICAL", "FATAL"}


class TestBuildDiscordEmbed:
    def test_basic_fields(self):
        embed = build_discord_embed({"level": "error", "message": "boom"})
        assert embed["title"] == "ERROR"
        assert embed["description"] == "boom"
        assert embed["color"] != 0

    def test_empty_message_placeholder(self):
        embed = build_discord_embed({"level": "warning", "message": ""})
        assert embed["description"] == "(empty message)"

    def test_long_message_is_truncated(self):
        long_message = "x" * 5000
        embed = build_discord_embed({"level": "error", "message": long_message})
        assert len(embed["description"]) < 5000
        assert embed["description"].endswith("(truncated)")

    def test_metadata_becomes_fields(self):
        embed = build_discord_embed(
            {
                "level": "error",
                "message": "boom",
                "logger": "brevitybot",
                "_metadata": {"service_name": "discord bot", "environment_name": "production"},
            }
        )
        field_names = {f["name"] for f in embed["fields"]}
        assert field_names == {"Service", "Environment", "Logger"}

    def test_no_metadata_means_no_fields(self):
        embed = build_discord_embed({"level": "error", "message": "boom"})
        assert embed["fields"] == []


class TestBuildDiscordPayloads:
    def test_no_alertable_logs_returns_empty(self):
        logs = [{"level": "info", "message": "fine"}, {"level": "debug", "message": "also fine"}]
        assert build_discord_payloads(logs) == []

    def test_single_alert_becomes_one_payload(self):
        logs = [{"level": "warning", "message": "uh oh"}]
        payloads = build_discord_payloads(logs)
        assert len(payloads) == 1
        assert len(payloads[0]["embeds"]) == 1

    def test_mixed_levels_only_alerts_forwarded(self):
        logs = [
            {"level": "info", "message": "fine"},
            {"level": "error", "message": "bad"},
            {"level": "debug", "message": "fine too"},
            {"level": "warning", "message": "also bad"},
        ]
        payloads = build_discord_payloads(logs)
        assert len(payloads) == 1
        assert len(payloads[0]["embeds"]) == 2

    def test_large_batch_is_chunked_and_summarized(self):
        logs = [{"level": "error", "message": f"err {i}"} for i in range(MAX_EMBEDS_PER_ALERT + 3)]
        payloads = build_discord_payloads(logs)
        # ceil((N+3)/10) chunk payloads + 1 summary payload
        embed_payloads = [p for p in payloads if "embeds" in p]
        summary_payloads = [p for p in payloads if "content" in p]
        assert sum(len(p["embeds"]) for p in embed_payloads) == MAX_EMBEDS_PER_ALERT + 3
        assert all(len(p["embeds"]) <= MAX_EMBEDS_PER_ALERT for p in embed_payloads)
        assert len(summary_payloads) == 1
        assert str(MAX_EMBEDS_PER_ALERT + 3) in summary_payloads[0]["content"]

    def test_exactly_max_embeds_no_summary(self):
        logs = [{"level": "error", "message": f"err {i}"} for i in range(MAX_EMBEDS_PER_ALERT)]
        payloads = build_discord_payloads(logs)
        assert len(payloads) == 1
        assert len(payloads[0]["embeds"]) == MAX_EMBEDS_PER_ALERT

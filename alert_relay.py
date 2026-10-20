"""Discord alert relay for Locomotive (github.com/brody192/locomotive).

Locomotive streams Railway logs to a webhook URL as raw JSON, but has no
Discord-compatible output mode — Discord's webhook API expects a specific
{content, embeds} schema and rejects arbitrary JSON. This service sits
between the two: it receives Locomotive's JSON/JSONL payload, keeps only
WARNING-and-above log lines, and reformats them into Discord messages.

Severity filtering relies on the `level` attribute Railway attaches to each
log line, which is only populated when the source app emits structured logs
(LOG_FORMAT=json on brevitybot.py) — plain text logs are all reported at
Railway's default "info" severity regardless of their real Python log level.

Run standalone: python alert_relay.py
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sys
from typing import Any, Optional

from aiohttp import web, ClientError, ClientSession, ClientTimeout

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("alert_relay")

PORT = int(os.getenv("PORT", "8081"))
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
RELAY_SHARED_SECRET = os.getenv("RELAY_SHARED_SECRET")

ALERT_LEVELS = {"WARNING", "WARN", "ERROR", "ERR", "CRITICAL", "FATAL"}
LEVEL_COLORS = {
    "WARNING": 0xF1C40F,
    "WARN": 0xF1C40F,
    "ERROR": 0xE74C3C,
    "ERR": 0xE74C3C,
    "CRITICAL": 0x992D22,
    "FATAL": 0x992D22,
}
MAX_MESSAGE_CHARS = 1500
MAX_EMBEDS_PER_ALERT = 10  # Discord's hard cap on embeds in one webhook message
# Discord also caps the combined text of all embeds in one message at 6000 chars.
MAX_CHARS_PER_PAYLOAD = 5500  # headroom under 6000 for titles/field names


def parse_log_payload(raw_body: bytes) -> list[dict[str, Any]]:
    """Parse a Locomotive webhook body: a JSON array, or JSON Lines."""
    text = raw_body.decode("utf-8").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [obj for obj in parsed if isinstance(obj, dict)]
        if isinstance(parsed, dict):
            return [parsed]
    except json.JSONDecodeError:
        pass

    objects = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping unparseable log line: %.200s", line)
            continue
        if isinstance(obj, dict):
            objects.append(obj)
    return objects


def extract_level(log: dict[str, Any]) -> Optional[str]:
    """Pull the app-reported level out of a reconstructed log object.

    Locomotive copies Railway's per-line attributes onto the JSON object
    verbatim, so brevitybot's JSONFormatter `level` field (when
    LOG_FORMAT=json) shows up as a top-level `level` key here. The top-level
    `severity` field is Railway's own platform classification, which is only
    reliable for apps that already emit structured/leveled output — it is
    NOT a substitute for `level` on an unstructured-text app.
    """
    level = log.get("level")
    if isinstance(level, str) and level.strip():
        return level.strip().upper()
    return None


def should_alert(log: dict[str, Any]) -> bool:
    level = extract_level(log)
    return level is not None and level in ALERT_LEVELS


def build_discord_embed(log: dict[str, Any]) -> dict[str, Any]:
    level = extract_level(log) or "UNKNOWN"
    message = str(log.get("message", "")).strip()
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[:MAX_MESSAGE_CHARS] + "… (truncated)"

    metadata = log.get("_metadata") or {}
    fields = []
    for key, label in (
        ("service_name", "Service"),
        ("environment_name", "Environment"),
    ):
        value = metadata.get(key)
        if value:
            fields.append({"name": label, "value": str(value), "inline": True})

    logger_name = log.get("logger")
    if logger_name:
        fields.append({"name": "Logger", "value": str(logger_name), "inline": True})

    timestamp = log.get("time") or log.get("timestamp")

    embed: dict[str, Any] = {
        "title": f"{level}",
        "description": message or "(empty message)",
        "color": LEVEL_COLORS.get(level, 0x95A5A6),
        "fields": fields,
    }
    if isinstance(timestamp, str):
        embed["timestamp"] = timestamp
    return embed


def _embed_chars(embed: dict[str, Any]) -> int:
    """Characters Discord counts toward the 6000-per-message embed limit."""
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    for field in embed.get("fields", []):
        total += len(field["name"]) + len(field["value"])
    return total


def build_discord_payloads(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group alertable logs into one or more Discord webhook payloads.

    Discord allows at most 10 embeds per message, so a burst gets split into
    multiple payloads plus a rolled-up summary line if it's large enough that
    sending every one individually would itself be spammy/rate-limited.
    """
    alertable = [log for log in logs if should_alert(log)]
    if not alertable:
        return []

    payloads: list[dict[str, Any]] = []
    chunk: list[dict[str, Any]] = []
    chunk_chars = 0
    for log in alertable:
        embed = build_discord_embed(log)
        size = _embed_chars(embed)
        if chunk and (
            len(chunk) >= MAX_EMBEDS_PER_ALERT or chunk_chars + size > MAX_CHARS_PER_PAYLOAD
        ):
            payloads.append({"embeds": chunk})
            chunk, chunk_chars = [], 0
        chunk.append(embed)
        chunk_chars += size
    if chunk:
        payloads.append({"embeds": chunk})

    if len(payloads) > 1:
        payloads.append(
            {"content": f"⚠️ {len(alertable)} warning/error log lines received in this batch."}
        )

    return payloads


async def send_to_discord(session: ClientSession, payload: dict[str, Any]) -> None:
    """Post one payload; retries once on a 429. Never raises.

    Failures are logged, not propagated: a 500 back to Locomotive would make
    it retry the whole batch and re-post chunks that already went through.
    """
    if not DISCORD_WEBHOOK_URL:
        logger.error("DISCORD_WEBHOOK_URL is not set; dropping alert: %s", payload)
        return
    for attempt in range(2):
        try:
            async with session.post(DISCORD_WEBHOOK_URL, json=payload) as resp:
                if resp.status == 429 and attempt == 0:
                    try:
                        delay = float(resp.headers.get("Retry-After", "1"))
                    except ValueError:
                        delay = 1.0
                    await asyncio.sleep(min(delay, 10))
                    continue
                if resp.status >= 300:
                    body = await resp.text()
                    logger.error("Discord webhook returned %s: %.500s", resp.status, body)
                return
        except (ClientError, asyncio.TimeoutError) as exc:
            logger.error("Discord webhook request failed: %s", exc)
            return


async def handle_webhook(request: web.Request) -> web.Response:
    provided = request.headers.get("X-Relay-Secret", "")
    if not RELAY_SHARED_SECRET or not hmac.compare_digest(
        provided.encode(), RELAY_SHARED_SECRET.encode()
    ):
        return web.Response(status=401, text="unauthorized")

    raw_body = await request.read()
    logs = parse_log_payload(raw_body)
    payloads = build_discord_payloads(logs)

    if payloads:
        session: ClientSession = request.app["client_session"]
        for payload in payloads:
            await send_to_discord(session, payload)
        logger.info("Forwarded %d alert(s) from a batch of %d log line(s).", len(payloads), len(logs))

    return web.Response(status=204)


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(status=200, text="ok")


async def on_startup(app: web.Application) -> None:
    app["client_session"] = ClientSession(timeout=ClientTimeout(total=15))


async def on_cleanup(app: web.Application) -> None:
    await app["client_session"].close()


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/webhook", handle_webhook)
    app.router.add_get("/health", handle_health)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    if not DISCORD_WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK_URL is not set at startup; alerts will be dropped until it is.")
    if not RELAY_SHARED_SECRET:
        sys.exit("RELAY_SHARED_SECRET is required; refusing to start with an open /webhook.")
    web.run_app(build_app(), port=PORT)

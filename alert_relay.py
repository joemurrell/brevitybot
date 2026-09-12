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

import json
import logging
import os
import sys
from typing import Any, Optional

from aiohttp import web, ClientSession, ClientTimeout

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


def build_discord_payloads(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group alertable logs into one or more Discord webhook payloads.

    Discord allows at most 10 embeds per message, so a burst gets split into
    multiple payloads plus a rolled-up summary line if it's large enough that
    sending every one individually would itself be spammy/rate-limited.
    """
    alertable = [log for log in logs if should_alert(log)]
    if not alertable:
        return []

    payloads = []
    for i in range(0, len(alertable), MAX_EMBEDS_PER_ALERT):
        chunk = alertable[i : i + MAX_EMBEDS_PER_ALERT]
        payloads.append({"embeds": [build_discord_embed(log) for log in chunk]})

    if len(alertable) > MAX_EMBEDS_PER_ALERT:
        payloads.append(
            {"content": f"⚠️ {len(alertable)} warning/error log lines received in this batch."}
        )

    return payloads


async def send_to_discord(session: ClientSession, payload: dict[str, Any]) -> None:
    if not DISCORD_WEBHOOK_URL:
        logger.error("DISCORD_WEBHOOK_URL is not set; dropping alert: %s", payload)
        return
    async with session.post(DISCORD_WEBHOOK_URL, json=payload) as resp:
        if resp.status >= 300:
            body = await resp.text()
            logger.error("Discord webhook returned %s: %.500s", resp.status, body)


async def handle_webhook(request: web.Request) -> web.Response:
    if RELAY_SHARED_SECRET:
        if request.headers.get("X-Relay-Secret") != RELAY_SHARED_SECRET:
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
        logger.warning("RELAY_SHARED_SECRET is not set; /webhook is unauthenticated.")
    web.run_app(build_app(), port=PORT)

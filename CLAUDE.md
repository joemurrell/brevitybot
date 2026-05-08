# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the bot
python brevitybot.py

# Run tests (test files are gitignored with pattern test_*)
pytest

# Run a single test file
pytest test_myfile.py

# Run a single test function
pytest test_myfile.py::test_function_name
```

Required environment variables (in `.env`):
- `DISCORD_BOT_TOKEN` — Discord bot token (required, bot won't start without it)
- `REDIS_URL` — Redis connection URL (required, bot won't start without it)
- `FLICKR_API_KEY` — Flickr API key for jet images in term posts (optional)
- `LOG_LEVEL` — Logging verbosity, default `INFO`
- `HEALTH_CHECK_PORT` — HTTP health check port, default `8080`
- `STARTUP_DELAY` — Seconds to wait before connecting, default `0` (used on Railway to prevent rapid-restart rate limits)
- `USER_AGENT` — Custom UA for Wikipedia scraping (optional)

## Architecture

The entire bot is a single file: `brevitybot.py`. There are no modules, packages, or subdirectories.

### Core components

**Discord client** — Uses `discord.Client` + `app_commands.CommandTree` (not `commands.Bot`). All slash commands are registered directly on the global `tree` object using `@tree.command(...)`. The client is initialized with `intents.message_content = True` and `intents.guilds = True`.

**Redis** — All state is stored in Redis. The `r` global is `None` at import time and initialized to an `aioredis` connection in `on_ready()`. Every function that touches Redis must run after `on_ready`.

**Health check server** — An `aiohttp.web` HTTP server starts in `on_ready()` on `HEALTH_CHECK_PORT`, serving `GET /health` and `GET /`. Used by Railway for deployment health checks.

**Background tasks** — Three `@tasks.loop()` tasks started in `on_ready()`:
- `post_brevity_term` — runs every 5 minutes, checks all configured guilds for overdue posts
- `refresh_terms_daily` — runs every 24 hours, re-scrapes Wikipedia
- `log_bot_stats` — runs every hour, logs server count and command usage

**Term scraping** — `parse_brevity_terms()` fetches the Wikipedia multiservice tactical brevity code page, parses `<dt>`/`<dd>` tag pairs into term/definition dicts, and returns a list. `update_brevity_terms()` diffs against stored terms and saves to Redis.

**Quiz system** — Two modes:
- *Private*: sequential async question flow using nested `ask_question()` coroutine, ephemeral messages, in-process score tracking
- *Public*: all questions posted at once as channel messages with `PublicQuizView` button components; answers stored per-question in Redis; results posted after `duration` minutes via `asyncio.create_task(close_and_summarize())`

### Redis key schema

| Key | Type | Description |
|-----|------|-------------|
| `brevity_terms` | string (JSON) | Full list of `{term, definition}` dicts |
| `brevity_terms_backup` | string (JSON) | Previous terms snapshot before each update |
| `post_channels` | hash | `guild_id` → `channel_id` |
| `post_freq:{guild_id}` | string | Posting interval in hours (default 24) |
| `last_posted:{guild_id}` | string | Unix timestamp of last post |
| `disabled_posting` | set | Guild IDs with posting disabled |
| `used_terms:{guild_id}` | set | Term strings already posted in this guild |
| `greenie:{guild_id}:{user_id}` | list | Last 10 quiz results as JSON `{correct, total, ts}` |
| `quiz:{quiz_id}:answers:{q_idx}` | hash | `user_id` → answer index for public quiz scoring |
| `last_command_sync` | string | Unix timestamp of last slash command sync (1-hour cooldown) |
| `reloadterms_cooldown` | string (TTL) | Sentinel key with TTL — global cooldown for `/reloadterms` |

### Slash command sync

Commands are synced globally in `on_ready()` with a 1-hour cooldown enforced via Redis (`last_command_sync` key) to avoid Discord rate limits. The in-memory `_commands_synced` flag prevents double-syncs within a single session.

### Multi-guild design

Every data access function takes a `guild_id` parameter. The `post_brevity_term` background task iterates all configured guilds from the `post_channels` hash. The scheduling window is ±5 minutes (the task loops every 5 minutes and posts if `current_time >= next_post_time - 300`).

## Key conventions

- All I/O is async/await — never use blocking calls (`requests`, `time.sleep`) inside coroutines except at startup before `client.run()`.
- Always `defer` interactions before any `await` that could take more than ~2 seconds:
  ```python
  await interaction.response.defer(thinking=True)  # or ephemeral=True
  # ... do work ...
  await interaction.followup.send(...)
  ```
- Guild-scoped operations: always pass `guild_id` (as int) when reading/writing Redis; never mix up `str`/`int` guild IDs (Redis stores them as strings, code converts at boundaries).
- Logging uses the custom `brevitybot` logger (`logger = logging.getLogger("brevitybot")`). The `CustomFormatter` strips timestamps and level tags — Railway captures structured output separately.
- Tests live in `tests/` and cover the pure helpers (`clean_term`, `pick_single_definition`, `sanitize_definition_for_quiz`, `_parse_terms_from_content`, `_truncate_code_block`, plus module-surface checks). Run with `pytest tests/`. Root-level throwaway test files matching `/test_*` are still gitignored for local scratch use.

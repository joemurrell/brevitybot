# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the bot
python brevitybot.py

# Run the test suite
pytest tests/

# Run a single test class or test
pytest tests/test_pure.py::TestParseTerms
pytest tests/test_pure.py::TestParseTerms::test_nested_ul_no_duplication
```

GitHub Actions runs `pytest tests/` on every PR to `main` and every push to `main` via `.github/workflows/tests.yml`.

Required environment variables (in `.env`):
- `DISCORD_BOT_TOKEN` — Discord bot token (required, bot won't start without it)
- `REDIS_URL` — Redis connection URL (required, bot won't start without it)
- `FLICKR_API_KEY` — Flickr API key for jet images in term posts (optional)
- `LOG_LEVEL` — Logging verbosity, default `INFO`
- `LOG_FORMAT` — `text` (default, plain message) or `json` (one JSON object per line with `extra={...}` fields)
- `HEALTH_CHECK_PORT` — HTTP health check port, default `8080`
- `STARTUP_DELAY` — Seconds to wait before connecting, default `0` (used on Railway to prevent rapid-restart rate limits)
- `USER_AGENT` — Custom UA for Wikipedia scraping (optional)

## Architecture

The entire bot is a single file: `brevitybot.py`. There are no modules, packages, or subdirectories.

### Core components

**Discord client** — Custom subclass `BrevityClient(discord.Client)` + `app_commands.CommandTree` (not `commands.Bot`). All slash commands are registered directly on the global `tree` object using `@tree.command(...)`. Intents: `message_content = True`, `guilds = True`. One-time async init lives in `BrevityClient.setup_hook()` (which fires exactly once); `on_ready` is reconnect-safe and only logs.

**Redis** — All state is stored in Redis. The `r` global is `None` at import time and initialized to an `aioredis` connection in `setup_hook()` with `max_connections=20` and `health_check_interval=30` so half-closed sockets on Railway get detected and reconnected. Every function that touches Redis must run after `setup_hook`.

**In-memory term cache** — `get_all_terms()` caches the parsed `brevity_terms` list in the module global `_terms_cache` with a 5-minute TTL backstop (multi-instance safety). `update_brevity_terms()` invalidates the cache locally on a successful write via `_invalidate_terms_cache()`. The cache is primed in `setup_hook` so the first interaction post-deploy doesn't pay the cold-cache cost. Speeds up autocomplete (fires per keystroke) dramatically.

**Health check server** — An `aiohttp.web` HTTP server starts in `setup_hook()` on `HEALTH_CHECK_PORT`, serving `GET /health` and `GET /`. Used by Railway for deployment health checks. Bound in `setup_hook` rather than `on_ready` so a websocket reconnect doesn't try to rebind the port.

**Background tasks** — Three `@tasks.loop()` tasks started in `setup_hook()`:
- `post_brevity_term` — runs every 5 minutes; reads channel map + disabled set + freq/last-posted in batched calls (`HGETALL` + `SMEMBERS` + 2× `MGET` = 4 calls regardless of guild count), then iterates the batched data to decide who's due.
- `refresh_terms_daily` — runs every 24 hours, calls `update_brevity_terms()` (which wraps `parse_brevity_terms()` + atomic backup/replace + cache invalidation).
- `log_bot_stats` — runs every hour, logs server count.

**Term scraping** — `parse_brevity_terms()` fetches the Wikipedia multi-service tactical brevity code page; the actual parsing is in `_parse_terms_from_content(content)`, a pure helper (no I/O) that's fully unit-testable. It walks only top-level `<dl>` blocks, iterating their direct `<dt>/<dd>` children; nested `<ul>/<ol>/<dl>` are extracted from `<dd>`s before `get_text()` so bullets aren't duplicated, and stray lists outside any `<dl>` can't be glued onto the previous term. `update_brevity_terms()` diffs against stored terms, atomically backups + writes via a `MULTI/EXEC` pipeline, then invalidates the cache.

**Quiz system** — Both modes share `build_quiz_question(current, all_terms, *, title, footer, with_timestamp=False)`, which returns `(embed, options, correct_idx, question_type)`. Each mode supplies its own embed styling.

- *Private*: ephemeral, single-user. `ask_question(q_idx, parent_interaction=None)` threads each button-click's interaction forward so subsequent questions use a fresh 15-minute token (not the original slash-command interaction). Per-question view timeout = 300 s.
- *Public*: persistent. Buttons are `QuizButton(discord.ui.DynamicItem)` instances; Discord routes clicks by custom_id pattern `q:{quiz_id}:{q_idx}:{answer_idx}`, so they keep working after a bot restart. Full quiz state is persisted to `quiz:{quiz_id}:meta` (JSON). `close_and_summarize(quiz_id)` is a top-level async function that reads everything from Redis — `setup_hook` scans for active quizzes and reschedules summaries after a restart. Per-guild concurrency lock (`active_quiz:{guild_id}`, atomic `SET NX EX`) prevents two simultaneous public quizzes. Per-user 60 s cooldown is **checked** for every `/quiz` invocation but only **set** after a successful public-quiz start (private mode self-throttles via sequential interaction), so a user who starts a public quiz can't immediately start another in either mode. `/quizstop` (Manage Messages) cancels in-flight; `/quizpurge` (Manage Server) clears a stale lock but refuses if meta still exists.

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
| `active_quiz:{guild_id}` | string (TTL) | Per-guild concurrency lock (acquired via `SET NX EX`) for public quizzes |
| `quiz_user_cooldown:{user_id}` | string (TTL) | Per-user 60s cooldown after starting a quiz |
| `quiz:{quiz_id}:meta` | string JSON (TTL) | Quiz state for restart-resumable summaries — guild_id, channel_id, deadline, options, correct indices, message ids |

### Slash command sync

Commands are synced globally in `setup_hook()` with a 1-hour cooldown enforced via Redis (`last_command_sync` key) to avoid Discord rate limits. The in-memory `_commands_synced` flag prevents double-syncs within a single session, and the Redis timestamp is also written on rate-limited paths so a crash-loop restart honors the cooldown.

When a deploy changes command signatures or permission gates and you want the change to appear in the slash picker immediately (not after the 1-hour cooldown elapses): `redis-cli DEL last_command_sync` before restart.

### Multi-guild design

Every data access function takes a `guild_id` parameter (typed `int`). The `post_brevity_term` background task iterates all configured guilds from the `post_channels` hash. The scheduling window is ±5 minutes (the task loops every 5 minutes and posts if `current_time >= next_post_time - 300`). All per-guild Redis reads inside the tick are batched (`HGETALL` + `SMEMBERS` + 2× `MGET`), so the per-tick cost is constant in the number of guilds, not 3N.

`get_next_brevity_term(guild_id)` is race-aware: it uses `SADD`'s return value to detect when another caller (e.g. concurrent `/nextterm` + scheduled post, or a multi-instance deploy) claimed the same term, and retries up to 5 × with a refreshed used-set.

## Key conventions

- All I/O is async/await — never use blocking calls (`requests`, `time.sleep`) inside coroutines except at startup before `client.run()`.
- `defer` early in any command that does non-trivial work — a Wikipedia scrape, a quiz build, anything that might exceed Discord's 3-second response deadline. Defer is the first `await`, before validation or Redis reads, so the deadline can never fire mid-work. Currently the commands that defer are `/nextterm`, `/reloadterms`, `/quiz`, `/quizstop`, `/quizpurge`; the rest (`/setup`, `/define`, `/setfrequency`, `/enableposting`, `/disableposting`, `/greenieboard`, `/checkperms`) finish in a single Redis hit or less and respond directly. Add `defer` to any new command that's not in that fast group:
  ```python
  await interaction.response.defer(ephemeral=True, thinking=True)
  # ... validate, read Redis, do work ...
  await interaction.followup.send(...)
  ```
- One-time async init goes in `BrevityClient.setup_hook()`, not `on_ready` (which can fire on every reconnect). `setup_hook` runs exactly once after login but before the websocket connects.
- Guild-scoped operations: always pass `guild_id` (as int) when reading/writing Redis; never mix up `str`/`int` guild IDs (Redis stores them as strings, code converts at boundaries).
- Persistent UI components extend `discord.ui.DynamicItem` so Discord can route clicks by `custom_id` regex after a bot restart — no in-memory view registry needed. See `QuizButton` for the pattern.
- Type hints: `from __future__ import annotations` at the top of the file; `Term`, `QuizOption`, `QuizMeta` are `TypedDict`s. Annotate new public helpers; runtime behavior of TypedDict literals is "just a dict".
- Multi-step Redis writes that must be observed together use `r.pipeline(transaction=True)` (`MULTI/EXEC`) — e.g. backup + replace in `update_brevity_terms`, `LPUSH` + `LTRIM` per-greenie. Iteration over Redis keyspaces uses `r.scan_iter(match=..., count=100)` — never `KEYS`.
- Logging uses the custom `brevitybot` logger (`logger = logging.getLogger("brevitybot")`). `LOG_FORMAT=text` (default) uses `CustomFormatter` — plain message, Railway captures timestamps and level separately. `LOG_FORMAT=json` switches to `JSONFormatter` which emits one JSON object per line including any `extra={...}` fields (e.g. `extra={"guild_id":..., "quiz_id":...}`).
- Slash-command permission gates are enforced server-side by Discord via `@app_commands.default_permissions(...)` decorators: `manage_guild` for config-mutating commands (`/setup`, `/setfrequency`, `/disableposting`, `/enableposting`, `/reloadterms`, `/quizpurge`), `manage_messages` for moderation (`/quizstop`). `@app_commands.guild_only()` is on every command except `/define` (which works in DMs since it has no guild dependency).
- Tests live in `tests/` and cover the pure helpers (`clean_term`, `pick_single_definition`, `sanitize_definition_for_quiz`, `_parse_terms_from_content`, `_truncate_code_block`, `build_quiz_question`, `JSONFormatter`, the `Term`/`QuizOption`/`QuizMeta` TypedDicts) plus module-surface and admin-gate checks. `tests/conftest.py` sets `DISCORD_BOT_TOKEN` + `REDIS_URL` env vars before importing brevitybot (which validates them at import time) and stubs `discord.Client.run`. Run with `pytest tests/`. Root-level throwaway test files matching `/test_*` are still gitignored for local scratch use.

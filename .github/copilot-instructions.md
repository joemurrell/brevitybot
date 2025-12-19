# Copilot Instructions for Brevity Bot

## Project Overview

Brevity Bot is a Discord bot that posts tactical brevity codes used in military aviation. The bot fetches terms from Wikipedia, stores configuration in Redis, and optionally includes images from Flickr. This is a Python-based asynchronous application using discord.py.

## Technology Stack

- **Language**: Python 3.9+
- **Framework**: discord.py 2.5.2
- **Database**: Redis (async client via redis.asyncio)
- **Web Scraping**: BeautifulSoup4, aiohttp
- **Environment**: python-dotenv for configuration
- **Testing**: pytest with pytest-asyncio
- **APIs**: Discord Bot API, Flickr API (optional)

## Code Style and Conventions

### General Guidelines

- Use async/await patterns consistently for all I/O operations
- Follow PEP 8 style guidelines for Python code
- Use type hints where beneficial for clarity
- Keep functions focused and single-purpose
- Use descriptive variable names

### Logging

- Use the custom logging setup defined at the top of brevitybot.py
- Log levels: INFO for stdout, WARNING+ for stderr
- The CustomFormatter strips timestamps and levels for Railway deployment
- Log format: `[timestamp] [LEVEL] name: message`

### Discord Bot Patterns

- All Discord commands use the `@app_commands` decorator pattern
- Commands should have clear descriptions for Discord's UI
- Use `interaction.response.defer()` for long-running operations
- Always handle errors gracefully with user-friendly messages
- Use Discord embeds for rich content presentation

### Redis Data Patterns

- Guild configurations: `config:{guild_id}` (JSON)
- Used terms: `used_terms:{guild_id}` (set)
- Posting state: `posting_enabled:{guild_id}` (string "true"/"false")
- Post frequency: `post_frequency:{guild_id}` (hours as string)
- Last posted time: `last_posted:{guild_id}` (Unix timestamp)
- Quiz scores: `quiz_scores:{guild_id}:{user_id}` (list)
- Cached terms: `cached_terms` (JSON list)

### Error Handling

- Always validate environment variables on startup
- Check Redis connectivity before bot operations
- Handle Wikipedia scraping failures gracefully
- Provide clear error messages to users via Discord interactions
- Log errors with appropriate context

## Development Workflow

### Environment Setup

Required environment variables (set in `.env` file):
- `DISCORD_BOT_TOKEN`: Your Discord bot token (required)
- `REDIS_URL`: Redis connection URL (required)
- `FLICKR_API_KEY`: Flickr API key for images (optional)
- `LOG_LEVEL`: Logging level (default: INFO)

### Testing

- Tests use pytest and pytest-asyncio
- Test files are ignored by `.gitignore` (pattern: `test_*`)
- Run tests with: `pytest`
- Focus on testing business logic functions separately from Discord interactions

### Dependencies

- Install with: `pip install -r requirements.txt`
- Keep requirements.txt up to date
- Pin major versions for stability
- Use aiohttp for all HTTP requests (async)

## Key Features to Understand

### Term Management

- Terms are scraped from Wikipedia's multiservice tactical brevity code page
- Terms are cached in Redis to minimize API calls
- Each guild tracks its own used terms to avoid repetition
- Terms can be reloaded manually via `/reloadterms`

### Scheduled Posting

- Uses discord.py's `@tasks.loop()` decorator for scheduled tasks
- Each guild can set a custom posting frequency (default: 24 hours)
- Posting can be enabled/disabled per guild
- The bot checks all guilds periodically for due posts

### Quiz System

- Multiple choice quizzes using Discord UI components (discord.ui.View)
- Questions are randomly generated from cached terms
- Scores are tracked per user per guild in Redis
- Greenie board displays last 10 quiz results per user

## Important Notes

- The bot is multi-guild capable - always consider guild_id in operations
- All Discord interactions must respond within 3 seconds or defer
- Redis operations should handle connection failures gracefully
- Flickr integration is optional - bot works without it
- The sanitize_definition_for_quiz function removes hints for quiz questions

## When Making Changes

1. Preserve async/await patterns throughout
2. Test Redis operations if modifying data access
3. Ensure Discord command descriptions are clear
4. Update this file if introducing new patterns or conventions
5. Consider multi-guild implications for new features
6. Handle errors gracefully with user-facing messages
7. Use the existing logging infrastructure
8. Keep the single-file structure unless there's a compelling reason to split

## Security Considerations

- Never commit `.env` file or expose tokens
- Validate and sanitize user inputs in commands
- Use environment variables for all secrets
- Redis URLs may contain passwords - handle securely
- Rate-limit considerations for Wikipedia and Flickr APIs

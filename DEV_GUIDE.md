# Local Development Guide

## Setup

1. **Install Python dependencies** (if not already installed):
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure your test bot**:
   - Edit `.env` file
   - Add your test bot token: `DISCORD_BOT_TOKEN=your_token_here`
   - Use Railway Redis URL or install Redis locally

3. **Invite your test bot** to a Discord server:
   - Go to Discord Developer Portal: https://discord.com/developers/applications
   - Select your test bot application
   - OAuth2 → URL Generator
   - Select scopes: `bot`, `applications.commands`
   - Select permissions: `Send Messages`, `Embed Links`, `Use Slash Commands`, `Read Message History`
   - Copy and visit the generated URL to invite to your test server

## Running Locally

```bash
python brevitybot.py
```

The bot will:
- Connect to Discord using your test bot token
- Load commands (you'll see "Logged in as...")
- Sync slash commands (first run may take a moment)

## Testing Commands

In your Discord test server, use:
- `/setup` - Configure the channel for posts
- `/nextterm` - Get a random brevity term
- `/quiz` - Test quiz functionality
- `/define <term>` - Look up a term
- `/checkperms` - Verify bot permissions

## Redis Options

### Option 1: Use Railway Redis (Recommended)
- Copy the `REDIS_URL` from Railway environment variables
- Paste it in your `.env` file
- Your local bot will use the same Redis as production (different guilds = isolated data)

### Option 2: Local Redis
- Install Redis: `winget install Redis.Redis` (Windows) or use Docker
- Use: `REDIS_URL=redis://localhost:6379`

## Tips

- Set `LOG_LEVEL=DEBUG` in `.env` for verbose logging
- Changes to code require restarting the bot (Ctrl+C, then `python brevitybot.py`)
- Slash commands may take 1-2 minutes to appear in Discord after first sync
- Test in a private server to avoid spamming production servers

## Common Issues

**"Redis connection failed"**
- Check your `REDIS_URL` in `.env`
- Ensure Redis is running (if using local)

**"Slash commands not appearing"**
- Wait 1-2 minutes after first run
- Check bot has `applications.commands` scope
- Try `/` in Discord to trigger command list refresh

**"Rate limited"**
- Use your test bot, not production bot
- Wait a few minutes between restarts
- Normal during development, won't affect users

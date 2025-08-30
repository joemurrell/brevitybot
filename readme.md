# Brevity Bot

**Brevity Bot** is a Discord bot that posts tactical brevity codes used in military aviation, complete with definitions and optional images. It automatically fetches terms from Wikipedia and posts them on a custom schedule per server.

## Features

- `/setup` — Set the current channel for automatic daily posting
- `/nextterm` — Manually post the next brevity term
- `/define <term>` — Look up a term's definition without marking it as used
- `/reloadterms` — Refresh the term list from Wikipedia
- `/setfrequency <hours>` — Set posting frequency per server in hours (any positive number, default 24 if not set)
- `/enableposting` — Enable automatic daily posting of brevity terms in the current channel.
- `/disableposting` — Disable automatic daily posting of brevity terms in the current channel.
- `/quiz` — Take a multiple choice quiz in a channel with friends or by yourself to test your knowledge of brevity terms.
- `/greenieboard` — Shows each user's last 10 quiz results as a greenie board-esque timeline with average score.

## Don't want to self-host? Easily invite an existing bot to your Discord server
https://discord.com/oauth2/authorize?client_id=1359029668547924098

## How It Works

- Pulls and caches terms from [Wikipedia](https://en.wikipedia.org/wiki/Multiservice_tactical_brevity_code)
- Stores config and usage in a Redis database, including:
  - Last posted time per guild
  - Used term history per guild
  - Custom post frequencies
- Optionally fetches militiary aviation images from Flickr
- Posts are sent via scheduled tasks and can be triggered manually

## Requirements

- Python 3.9 or newer
- Discord bot token
- Redis database (local or hosted)
- Flickr API key (optional, used for image embeds)

## Environment Variables

These should be set in a `.env` file or your hosting environment:

```env
DISCORD_BOT_TOKEN=your-discord-token
REDIS_URL=redis://default:password@host:port
FLICKR_API_KEY=your-flickr-api-key
```

# Brevity Bot

**Brevity Bot** is a Discord bot that posts tactical brevity codes used in military aviation, complete with definitions and optional images. It automatically fetches terms from Wikipedia and posts them on a custom schedule per server.

## Features

- `/setup` — Set the current channel for automatic daily posting
- `/nextterm` — Manually post the next brevity term
- `/define <term>` — Look up a term's definition without marking it as used
- `/reloadterms` — Refresh the term list from Wikipedia
- `/setfrequency <1-24>` — Set posting frequency per server (in hours)

## How It Works

- Pulls and caches terms from [Wikipedia](https://en.wikipedia.org/wiki/Multiservice_tactical_brevity_code)
- Stores config and usage in Redis, including:
  - Last posted time per guild
  - Used term history per guild
  - Custom post frequencies
- Optionally fetches jet images from Flickr
- Posts are sent via scheduled tasks and can be triggered manually

## Requirements

- Python 3.9 or newer
- Discord bot token
- Redis database (local or hosted)
- Flickr API key (optional, used for image embeds)

## Environment Variables

These should be set in Railway, a `.env` file, or your hosting environment:

```env
DISCORD_BOT_TOKEN=your-discord-token
REDIS_URL=redis://default:password@host:port
FLICKR_API_KEY=your-flickr-api-key

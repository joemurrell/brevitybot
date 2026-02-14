# BrevityBot

A Discord bot that teaches tactical brevity codes used in military aviation—complete with definitions, quizzes, and optional images. Automatically fetches 500+ terms from Wikipedia and posts them on a customizable schedule per server.

## ✨ Features

### 📚 Term Management
- **Automatic Daily Posts** — Schedule brevity terms to post automatically at custom intervals
- **Manual Posting** — Request the next term on demand
- **Smart Rotation** — Never repeats terms until all have been posted
- **Wikipedia Integration** — Automatically fetches and caches 500+ terms from official sources
- **Term Lookup** — Search and define any term without marking it as used

### 🎯 Interactive Quizzes
- **Multiple Choice Quizzes** — Test your knowledge with randomly generated questions
- **Two Modes:**
  - **Public Mode** — Compete with friends in a timed channel poll
  - **Private Mode** — Take quizzes solo with ephemeral responses
- **Greenie Board** — Track your last 10 quiz scores with a naval aviation-style performance board

### ⚙️ Server Configuration
- **Flexible Scheduling** — Set post frequency to any interval (default: 24 hours)
- **Per-Server Settings** — Each Discord server maintains independent configuration
- **Enable/Disable Posting** — Pause and resume automatic posts anytime
- **Channel Assignment** — Choose which channel receives the daily posts

## 🚀 Quick Start

**Don't want to self-host?** Invite the public bot to your server:

👉 **[Add BrevityBot to Discord](https://discord.com/oauth2/authorize?client_id=1359029668547924098)**

Then run `/setup` in your desired channel to get started!

## 📋 Commands

| Command | Description |
|---------|-------------|
| `/setup` | Configure the current channel for automatic posting |
| `/nextterm` | Manually post the next brevity term |
| `/define <term>` | Look up a term's definition (with autocomplete) |
| `/quiz [questions] [mode] [duration]` | Start a quiz (1-10 questions, public or private) |
| `/greenieboard` | View quiz leaderboard with last 10 results per user |
| `/setfrequency <hours>` | Set posting interval (any positive number) |
| `/enableposting` | Resume automatic posting |
| `/disableposting` | Pause automatic posting |
| `/reloadterms` | Manually refresh terms from Wikipedia |
| `/checkperms` | Verify bot permissions in the current channel |

## 🛠️ How It Works

1. **Term Fetching** — Scrapes [Wikipedia's Multiservice Tactical Brevity Code](https://en.wikipedia.org/wiki/Multi-service_tactical_brevity_code) page and parses 500+ terms with definitions
2. **Caching** — Terms are cached in Redis to minimize API calls and improve performance
3. **Per-Guild Tracking** — Each Discord server maintains:
   - Used/unused term history
   - Posting schedule and frequency
   - Channel configuration
   - Individual user quiz scores
4. **Scheduled Tasks** — Background tasks handle:
   - Automatic term posting based on server schedules
   - Daily term refresh from Wikipedia
   - Health monitoring and stats logging
5. **Quiz Generation** — Creates multiple-choice questions by masking the term from its definition and adding distractor answers

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.


## 🆘 Support

Having issues? Check the logs for detailed error messages. Common issues:

- **Bot not responding** — Verify the bot has proper permissions in your server
- **Terms not posting** — Check that posting is enabled with `/enableposting`

For bugs or feature requests, please [open an issue](https://github.com/joemurrell/brevitybot/issues).

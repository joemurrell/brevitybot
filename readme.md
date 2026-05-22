# BrevityBot

A Discord bot that teaches the tactical brevity codes used in military aviation — short, standardized radio terms like *SHACKLE*, *BOGEY*, and *FOX TWO*. BrevityBot posts terms on a schedule, lets you look up definitions on demand, and quizzes you to help the codes stick.

The terms come from the multi-service brevity standard maintained by the [Air Land Sea Space Application (ALSSA) Center](https://www.alssa.mil/) — *Multi-Service Tactics, Techniques, and Procedures for Multi-Service Brevity Codes* (ATP 1-02.1 / MCRP 3-30B.1 / NTTP 6-02.1 / AFTTP 3-2.5 / STTP 3-9002).

<p align="center">
  <img src="assets/brevity_cover.png" alt="ALSSA Multi-Service Brevity Codes (April 2025) cover" width="320">
</p>

## What it does

BrevityBot drops a brevity term into your channel on whatever schedule you set, cycling through the full list without repeats. You can also pull up any definition instantly with `/define`, and test yourself (or compete with the server) using the built-in quizzes.

<p align="center">
  <img src="assets/discord_embed.png" alt="Example BrevityBot term post in Discord" width="480">
</p>

## Features

- **Scheduled posts** — A new term is posted automatically at an interval you choose (default: every 24 hours), rotating through all terms before repeating.
- **On-demand lookup** — `/define` searches and explains any term, with autocomplete, without consuming it from the rotation.
- **Manual posting** — Grab the next term whenever you want with `/nextterm`.
- **Quizzes** — Multiple-choice questions generated from the term definitions, in a public timed poll or a private solo run.
- **Greenie board** — Tracks each user's last 10 quiz scores, naval-aviation style.
- **Per-server config** — Every server has its own channel, schedule, rotation state, and scores.

## Quick start

Invite the bot to your server:

**[➕ Add BrevityBot to Discord](https://discord.com/oauth2/authorize?client_id=1359029668547924098)**

Then run `/setup` in the channel where you want terms posted.

## Commands

| Command | Description |
|---------|-------------|
| `/setup` | Configure the current channel for automatic posting |
| `/nextterm` | Manually post the next brevity term |
| `/define <term>` | Look up a term's definition (with autocomplete) |
| `/quiz [questions] [mode] [duration]` | Start a quiz (1–10 questions, public or private) |
| `/greenieboard` | View the quiz leaderboard (last 10 results per user) |
| `/setfrequency <hours>` | Set the posting interval |
| `/enableposting` | Resume automatic posting |
| `/disableposting` | Pause automatic posting |
| `/reloadterms` | Refresh the term list |
| `/checkperms` | Verify the bot's permissions in the current channel |

## How it works

Terms and definitions are sourced from [Wikipedia's Multi-service tactical brevity code](https://en.wikipedia.org/wiki/Multi-service_tactical_brevity_code) page (which mirrors the ALSSA publication) and cached in Redis. Each server tracks its own rotation, schedule, and quiz scores independently. Background tasks handle scheduled posting, periodic term refreshes, and health logging. Quizzes are built by masking the term out of its own definition and adding distractor answers.

## Contributing

Contributions are welcome — feel free to open a pull request.

## Support

Common issues:

- **Bot not responding** — Confirm the bot has the right permissions (`/checkperms`).
- **Terms not posting** — Make sure posting is enabled (`/enableposting`).

For bugs or feature requests, [open an issue](https://github.com/joemurrell/brevitybot/issues).

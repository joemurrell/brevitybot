"""Test bootstrap.

`brevitybot` validates DISCORD_BOT_TOKEN and REDIS_URL at import time, so we
must set those env vars before any test module imports it. We also stub
`discord.Client.run` so an accidental import that triggers `__main__` paths
won't try to talk to Discord.
"""
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

import discord  # noqa: E402

discord.Client.run = lambda self, *a, **kw: None

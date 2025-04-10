# brevity_discord_bot.py
import os
import json
import random
import requests
from discord.ext import commands, tasks
from discord import Intents
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
CACHE_FILE = "brevity.json"
WIKI_API_URL = (
    "https://en.wikipedia.org/w/api.php?"
    "action=query&list=search&srsearch=Multiservice%20tactical%20brevity%20code&format=json"
)

# Initialize bot
intents = Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def fetch_brevity_terms():
    try:
        response = requests.get(WIKI_API_URL)
        data = response.json()
        search_results = data["query"]["search"]
        return list({entry["title"] for entry in search_results})
    except Exception as e:
        print(f"Error fetching terms: {e}")
        return []


def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    return {"all_terms": [], "used_terms": []}


def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)


def get_next_term(cache):
    all_terms = cache["all_terms"]
    used_terms = set(cache["used_terms"])
    remaining_terms = list(set(all_terms) - used_terms)

    if not remaining_terms:
        cache["used_terms"] = []
        remaining_terms = all_terms[:]

    next_term = random.choice(remaining_terms)
    cache["used_terms"].append(next_term)
    return next_term, cache


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    daily_brevity.start()


@tasks.loop(hours=24)
async def daily_brevity():
    cache = load_cache()
    new_terms = fetch_brevity_terms()

    if set(new_terms) != set(cache["all_terms"]):
        cache["all_terms"] = new_terms
        cache["used_terms"] = []

    if not cache["all_terms"]:
        print("No brevity terms available.")
        return

    next_term, cache = get_next_term(cache)
    save_cache(cache)

    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        await channel.send(f"**Brevity of the Day:** {next_term}")
    else:
        print(f"Channel with ID {CHANNEL_ID} not found.")


@bot.command()
async def brevity(ctx):
    cache = load_cache()
    new_terms = fetch_brevity_terms()

    if set(new_terms) != set(cache["all_terms"]):
        cache["all_terms"] = new_terms
        cache["used_terms"] = []

    next_term, cache = get_next_term(cache)
    save_cache(cache)
    await ctx.send(f"**Brevity of the Day:** {next_term}")


bot.run(TOKEN)

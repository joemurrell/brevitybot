import discord
from discord.ext import tasks
from discord import app_commands
import os
import random
import requests
import html
from bs4 import BeautifulSoup
import re
import logging
import redis
import json
from dotenv import load_dotenv
from urllib.parse import urlparse

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)8s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
# Suppress noisy third-party libraries
logging.getLogger("discord").setLevel(logging.ERROR)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# -------------------------------
# REDIS CONFIGURATION
# -------------------------------

redis_url = os.getenv("REDIS_URL")
if not redis_url:
    raise ValueError("❌ REDIS_URL environment variable not set!")
parsed_url = urlparse(redis_url)
r = redis.Redis(
    host=parsed_url.hostname,
    port=parsed_url.port,
    password=parsed_url.password,
    decode_responses=True
)

# -------------------------------
# CONFIGURATION
# -------------------------------

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
FLICKR_API_KEY = os.getenv("FLICKR_API_KEY")
TERMS_KEY = "brevity_terms"
CHANNEL_MAP_KEY = "post_channels"

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# -------------------------------
# UTILITIES
# -------------------------------

def clean_term(term):
    cleaned = re.sub(r"\s*\[.*?\]", "", term)
    cleaned = cleaned.replace("*", "").strip()
    return cleaned.upper()

def load_used_terms(guild_id):
    return list(r.smembers(f"used_terms:{guild_id}"))

def save_used_term(guild_id, term):
    r.sadd(f"used_terms:{guild_id}", term)

def save_config(guild_id, channel_id):
    r.hset(CHANNEL_MAP_KEY, str(guild_id), channel_id)

def load_config(guild_id=None):
    if guild_id:
        channel_id = r.hget(CHANNEL_MAP_KEY, str(guild_id))
        return {"channel_id": int(channel_id)} if channel_id else None
    else:
        all_configs = r.hgetall(CHANNEL_MAP_KEY)
        return {gid: {"channel_id": int(cid)} for gid, cid in all_configs.items()}

def get_random_flickr_jet(api_key):
    flickr_url = "https://www.flickr.com/services/rest/"
    params = {
        "method": "flickr.photos.search",
        "api_key": api_key,
        "group_id": "38653945@N00",
        "format": "json",
        "nojsoncallback": 1,
        "per_page": 50,
        "page": random.randint(1, 10),
        "sort": "relevance",
        "content_type": 1,
        "media": "photos",
        "safe_search": 1,
        "license": "1,2,4,5,7,9,10"
    }
    try:
        response = requests.get(flickr_url, params=params)
        data = response.json()
        photos = data.get("photos", {}).get("photo", [])
        if not photos:
            print("No photos found.")
            return None
        photo = random.choice(photos)
        return f"https://farm{photo['farm']}.staticflickr.com/{photo['server']}/{photo['id']}_{photo['secret']}_b.jpg"
    except Exception as e:
        print(f"❌ Flickr image fetch failed: {e}")
        return None

def parse_brevity_terms():
    url = "https://en.wikipedia.org/wiki/Multiservice_tactical_brevity_code"
    response = requests.get(url)
    soup = BeautifulSoup(response.content, "html.parser")
    content_div = soup.find("div", class_="mw-parser-output")
    terms = []
    capture = False
    current_term = None
    for tag in content_div.find_all(["h2", "dt", "dd"]):
        if tag.name == "h2":
            span = tag.find("span", class_="mw-headline")
            if span and span.get("id") == "See_also":
                capture = False
                break
        if tag.name == "dt":
            term_text = tag.get_text(" ", strip=True)
            if term_text == "Aborting/Abort/Aborted":
                capture = True
            current_term = term_text if capture else None
        elif tag.name == "dd" and capture and current_term:
            definition_text = tag.get_text(" ", strip=True)
            terms.append({"term": clean_term(current_term), "definition": definition_text})
            current_term = None
    return terms

def update_brevity_terms():
    existing = r.get(TERMS_KEY)
    if existing:
        existing = json.loads(existing)
        existing_terms_set = {t["term"] for t in existing}
    else:
        existing = []
        existing_terms_set = set()
    new_terms = parse_brevity_terms()
    new_unique = [t for t in new_terms if t["term"] not in existing_terms_set]
    if new_unique:
        print(f"Found {len(new_unique)} new terms. Adding...")
        existing.extend(new_unique)
        r.set(TERMS_KEY, json.dumps(existing))
    else:
        print("No new terms found.")

def get_all_terms():
    terms_data = r.get(TERMS_KEY)
    if not terms_data:
        return []
    return json.loads(terms_data)

def get_next_brevity_term(guild_id):
    all_terms = get_all_terms()
    if not all_terms:
        print("❌ No brevity terms available in Redis.")
        return None
    used_terms = load_used_terms(guild_id)
    unused_terms = [t for t in all_terms if t["term"] not in used_terms]
    if not unused_terms:
        print(f"All terms used for guild {guild_id} — resetting list.")
        unused_terms = all_terms
        r.delete(f"used_terms:{guild_id}")
    chosen = random.choice(unused_terms)
    save_used_term(guild_id, chosen["term"])
    return chosen

def get_brevity_term_by_name(term_name):
    all_terms = get_all_terms()
    for entry in all_terms:
        if entry["term"].lower() == term_name.lower():
            return entry
    return None

# -------------------------------
# SLASH COMMANDS
# -------------------------------

@tree.command(name="setup", description="Set the current channel for daily brevity posts.")
async def setup(interaction: discord.Interaction):
    save_config(interaction.guild.id, interaction.channel.id)
    await interaction.response.send_message(f"Setup complete! I’ll post daily here in <#{interaction.channel.id}>.", ephemeral=True)

@tree.command(name="newterm", description="Send a new brevity term immediately.")
async def newterm(interaction: discord.Interaction):
    term = get_next_brevity_term(interaction.guild.id)
    if not term:
        await interaction.response.send_message("No terms available. Please check the brevity terms.", ephemeral=True)
        return
    image_url = get_random_flickr_jet(FLICKR_API_KEY)
    letter = term['term'][0].upper()
    wiki_url = f"https://en.wikipedia.org/wiki/Multiservice_tactical_brevity_code#{letter}"
    embed = discord.Embed(
        title=term['term'],
        url=wiki_url,
        description=term['definition'],
        color=discord.Color.blue()
    )
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(text="From Wikipedia – Multiservice Tactical Brevity Code")
    await interaction.response.send_message(embed=embed)
    print(f"Manually sent to {interaction.guild.name}: {term['term']}")

@tree.command(name="define", description="Look up the definition of a brevity term (without tracking it).")
@app_commands.describe(term="The brevity term to define")
async def define(interaction: discord.Interaction, term: str):
    entry = get_brevity_term_by_name(term)
    if not entry:
        await interaction.response.send_message(f"No definition found for '{term}'.", ephemeral=True)
        return
    letter = entry['term'][0].upper()
    wiki_url = f"https://en.wikipedia.org/wiki/Multiservice_tactical_brevity_code#{letter}"
    embed = discord.Embed(
        title=entry['term'],
        url=wiki_url,
        description=entry['definition'],
        color=discord.Color.green()
    )
    embed.set_footer(text="Queried from Wikipedia – Multiservice Tactical Brevity Code")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="reloadterms", description="Manually refresh brevity terms from Wikipedia.")
async def reloadterms(interaction: discord.Interaction):
    update_brevity_terms()
    await interaction.response.send_message("✅ Brevity terms reloaded from Wikipedia.", ephemeral=True)

# -------------------------------
# DAILY POSTING LOOP
# -------------------------------

@tasks.loop(hours=24)
async def post_brevity_term():
    all_configs = load_config()
    for guild_id_str, config in all_configs.items():
        guild_id = int(guild_id_str)
        channel = client.get_channel(config["channel_id"])
        if channel is None:
            print(f"❌ Channel {config['channel_id']} not found for guild {guild_id}.")
            continue
        term = get_next_brevity_term(guild_id)
        if not term:
            continue
        image_url = get_random_flickr_jet(FLICKR_API_KEY)
        letter = term['term'][0].upper()
        wiki_url = f"https://en.wikipedia.org/wiki/Multiservice_tactical_brevity_code#{letter}"
        await channel.send("Brevity Term of the Day")
        embed = discord.Embed(
            title=term['term'],
            url=wiki_url,
            description=term['definition'],
            color=discord.Color.blue()
        )
        if image_url:
            embed.set_image(url=image_url)
        embed.set_footer(text="From Wikipedia – Multiservice Tactical Brevity Code")
        await channel.send(embed=embed)
        print(f"Sent to guild {guild_id}: {term['term']}")

@tasks.loop(hours=24)
async def refresh_terms_daily():
    update_brevity_terms()

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    await tree.sync()
    if not post_brevity_term.is_running():
        post_brevity_term.start()
    if not refresh_terms_daily.is_running():
        refresh_terms_daily.start()

if __name__ == "__main__":
    client.run(DISCORD_BOT_TOKEN)

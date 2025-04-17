import discord
from discord.ext import tasks
from discord import app_commands
import json
import os
import random
import requests
import html
from bs4 import BeautifulSoup
import re
import logging
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configure logging: suppress external libs but keep INFO logs for own app
logging.basicConfig(level=logging.INFO)
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# -------------------------------
# CONFIGURATION
# -------------------------------

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
FLICKR_API_KEY = os.getenv("FLICKR_API_KEY")
CONFIG_FILE = "config.json"
USED_TERMS_FILE = "used_terms.json"
TERMS_FILE = "brevity_terms.json"

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
    return cleaned

def load_used_terms(guild_id):
    if not os.path.exists(USED_TERMS_FILE):
        return []
    with open(USED_TERMS_FILE, "r") as f:
        data = json.load(f)
        return data.get(str(guild_id), [])

def save_used_term(guild_id, term):
    data = {}
    if os.path.exists(USED_TERMS_FILE):
        with open(USED_TERMS_FILE, "r") as f:
            data = json.load(f)
    terms = data.get(str(guild_id), [])
    terms.append(term)
    data[str(guild_id)] = terms
    with open(USED_TERMS_FILE, "w") as f:
        json.dump(data, f)

def save_config(guild_id, channel_id):
    config = {}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
    config[str(guild_id)] = {"channel_id": channel_id}
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f)

def load_config(guild_id=None):
    if not os.path.exists(CONFIG_FILE):
        return {} if guild_id is None else None
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
    return config if guild_id is None else config.get(str(guild_id))

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
    try:
        with open(TERMS_FILE, "r", encoding="utf-8") as f:
            existing = json.load(f)
    except FileNotFoundError:
        existing = []
    existing_terms_set = {t["term"] for t in existing}
    new_terms = parse_brevity_terms()
    new_unique = [t for t in new_terms if t["term"] not in existing_terms_set]
    if new_unique:
        print(f"Found {len(new_unique)} new terms. Adding...")
        existing.extend(new_unique)
        with open(TERMS_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    else:
        print("No new terms found.")

def get_next_brevity_term(guild_id):
    try:
        with open(TERMS_FILE, "r", encoding="utf-8") as f:
            all_terms = json.load(f)
    except FileNotFoundError:
        print("❌ brevity_terms.json not found.")
        return None
    used_terms = load_used_terms(guild_id)
    unused_terms = [t for t in all_terms if t["term"] not in used_terms]
    if not unused_terms:
        print(f"All terms used for guild {guild_id} — resetting list.")
        unused_terms = all_terms
        with open(USED_TERMS_FILE, "w") as f:
            json.dump({}, f)
    chosen = random.choice(unused_terms)
    save_used_term(guild_id, chosen["term"])
    return chosen

def get_brevity_term_by_name(term_name):
    try:
        with open(TERMS_FILE, "r", encoding="utf-8") as f:
            all_terms = json.load(f)
        for entry in all_terms:
            if entry["term"].lower() == term_name.lower():
                return entry
    except FileNotFoundError:
        print("❌ brevity_terms.json not found.")
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
        await interaction.response.send_message("No terms available. Please check the brevity terms file.", ephemeral=True)
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
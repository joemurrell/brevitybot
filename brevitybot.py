import discord
from discord.ext import commands, tasks
import json
import os
import random
import requests
import asyncio
import html
from bs4 import BeautifulSoup
import re
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# -------------------------------
# CONFIGURATION
# -------------------------------

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
FLICKR_API_KEY = os.getenv("FLICKR_API_KEY")
CONFIG_FILE = "/data/config.json"
USED_TERMS_FILE = "/data/used_terms.json"
TERMS_FILE = "brevity_terms.json"

bot = commands.Bot(command_prefix="!bb", intents=discord.Intents.all())

# -------------------------------
# UTILITIES
# -------------------------------

def clean_term(term):
# Remove square-bracketed content and asterisks from a term
    cleaned = re.sub(r"\s*\[.*?\]", "", term)
    cleaned = cleaned.replace("*", "").strip()
    return cleaned

def load_used_terms():
    # Load list of previously used brevity terms
    if not os.path.exists(USED_TERMS_FILE):
        return []
    with open(USED_TERMS_FILE, "r") as f:
        data = json.load(f)
        return data if isinstance(data, list) else []

def save_used_term(term):
    # Append a used term to the tracking file
    used = load_used_terms()
    used.append(term)
    with open(USED_TERMS_FILE, "w") as f:
        json.dump(used, f)

def save_config(channel_id):
    # Save the channel ID where posts should be sent
    with open(CONFIG_FILE, "w") as f:
        json.dump({"channel_id": channel_id}, f)

def load_config():
    # Load the saved config (channel ID)
    if not os.path.exists(CONFIG_FILE):
        return None
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def get_random_flickr_jet(api_key):
    # Fetch a random fighter jet image from a specific Flickr group
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
        farm = photo["farm"]
        server = photo["server"]
        photo_id = photo["id"]
        secret = photo["secret"]

        return f"https://farm{farm}.staticflickr.com/{server}/{photo_id}_{secret}_b.jpg"

    except Exception as e:
        print(f"❌ Flickr image fetch failed: {e}")
        return None

# -------------------------------
# PARSER + UPDATER
# -------------------------------

def parse_brevity_terms():
    # Scrape brevity terms from the Wikipedia page
    url = "https://en.wikipedia.org/wiki/Multiservice_tactical_brevity_code"
    response = requests.get(url)
    soup = BeautifulSoup(response.content, "html.parser")

    content_div = soup.find("div", class_="mw-parser-output")
    terms = []
    capture = False

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
                current_term = term_text
            elif capture:
                current_term = term_text

        elif tag.name == "dd" and capture:
            definition_text = tag.get_text(" ", strip=True)
            if current_term:
                terms.append({
                    "term": clean_term(current_term),
                    "definition": definition_text
                })
                current_term = None

    return terms

def update_brevity_terms():
    # Merge newly scraped terms into the local brevity_terms.json file
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

# -------------------------------
# DISCORD COMMANDS
# -------------------------------

@bot.command()
async def setup(ctx):
    # Allow user to configure which channel to post to
    channel_id = ctx.channel.id
    save_config(channel_id)
    await ctx.send(f"Setup complete! I’ll post daily here in <#{channel_id}>.")

@bot.command()
async def newterm(ctx):
    # Manually send a new brevity term to the channel
    term = get_next_brevity_term()
    if not term:
        await ctx.send("No terms available. Please check the brevity terms file.")
        return

    image_url = get_random_flickr_jet(FLICKR_API_KEY)

    # Link to appropriate letter section of the wiki page
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

    await ctx.send(embed=embed)
    print(f"Manually sent: {term['term']}")

# -------------------------------
# GET NEXT TERM
# -------------------------------

def get_next_brevity_term():
    # Select a random unused brevity term, or reset if all used
    try:
        with open(TERMS_FILE, "r", encoding="utf-8") as f:
            all_terms = json.load(f)
    except FileNotFoundError:
        print("❌ brevity_terms.json not found.")
        return None

    used_terms = load_used_terms()
    unused_terms = [t for t in all_terms if t["term"] not in used_terms]

    if not unused_terms:
        print("All terms used — resetting list.")
        unused_terms = all_terms
        with open(USED_TERMS_FILE, "w") as f:
            json.dump([], f)

    chosen = random.choice(unused_terms)
    save_used_term(chosen["term"])
    return chosen

# -------------------------------
# DAILY POSTING LOOP
# -------------------------------

@tasks.loop(hours=24)
async def post_brevity_term():
    # Post a new brevity term and random image to the configured channel
    config = load_config()
    if not config or "channel_id" not in config:
        print("❌ No channel configured. Use !bbsetup in a channel.")
        return

    channel = bot.get_channel(config["channel_id"])
    if channel is None:
        print("❌ Channel not found or inaccessible.")
        return

    term = get_next_brevity_term()
    if not term:
        return

    image_url = get_random_flickr_jet(FLICKR_API_KEY)

    # Link to appropriate letter section of the wiki page
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
    print(f"Sent to #{channel.name}: {term['term']}")

# -------------------------------
# DAILY REFRESH LOOP
# -------------------------------

@tasks.loop(hours=24)
async def refresh_terms_daily():
    # Run daily refresh to fetch and save any new brevity terms
    print("Running daily term updater...")
    update_brevity_terms()

# -------------------------------
# BOT READY
# -------------------------------

@bot.event
async def on_ready():
    # Bot startup: begin background loops for posting and refreshing
    print(f"Logged in as {bot.user}")
    post_brevity_term.start()
    refresh_terms_daily.start()

# -------------------------------
# RUN THE BOT
# -------------------------------

bot.run(DISCORD_BOT_TOKEN)

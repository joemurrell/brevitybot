import discord
from discord.ext import tasks
from discord import app_commands
import os
import random
import requests
from bs4 import BeautifulSoup
import re
import logging
import redis  # type: ignore
import json
from dotenv import load_dotenv
from urllib.parse import urlparse
import time
import sys

# -------------------------------
# LOGGING
# -------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

class MaxLevelFilter(logging.Filter):
    def __init__(self, level):
        self.level = level
    def filter(self, record):
        return record.levelno <= self.level

# Formatter for consistent, readable logs
log_format = "[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s"
formatter = logging.Formatter(log_format)

stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)
stdout_handler.addFilter(MaxLevelFilter(logging.INFO))

stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setLevel(logging.WARNING)

class CustomFormatter(logging.Formatter):
    def format(self, record):
        # For Railway, remove all [LEVEL] tags and timestamps from all logs
        return f"{record.getMessage()}"

formatter = CustomFormatter()

stdout_handler.setFormatter(formatter)
stderr_handler.setFormatter(formatter)

# Remove all handlers from root logger and discord logger
for logger_name in (None, "discord", "brevitybot"):
    log = logging.getLogger(logger_name)
    log.handlers.clear()
    log.setLevel(LOG_LEVEL)
    log.propagate = False
    log.addHandler(stdout_handler)
    log.addHandler(stderr_handler)

# Suppress discord.client warnings (e.g., PyNaCl not installed)
discord_client_logger = logging.getLogger("discord.client")
discord_client_logger.setLevel(logging.ERROR)
discord_client_logger.handlers.clear()
discord_client_logger.addHandler(stdout_handler)
discord_client_logger.addHandler(stderr_handler)

# Remove all handlers from discord.gateway and set formatter (to catch any direct logging)
discord_gateway_logger = logging.getLogger("discord.gateway")
discord_gateway_logger.handlers.clear()
discord_gateway_logger.setLevel(LOG_LEVEL)
discord_gateway_logger.propagate = False
discord_gateway_logger.addHandler(stdout_handler)
discord_gateway_logger.addHandler(stderr_handler)

logger = logging.getLogger("brevitybot")
logger.setLevel(LOG_LEVEL)

# To enable debug logs, set LOG_LEVEL=DEBUG in your environment.

# After setting up handlers, print all handlers for debugging
for logger_name in (None, "discord", "brevitybot", "discord.client"):
    log = logging.getLogger(logger_name)
    logger.info(f"Logger '{logger_name}': handlers: {log.handlers}")

# -------------------------------
# ENVIRONMENT VARIABLES
# -------------------------------
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
FLICKR_API_KEY = os.getenv("FLICKR_API_KEY")
redis_url = os.getenv("REDIS_URL")

if not DISCORD_BOT_TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN is not set in environment!")
if not redis_url:
    raise ValueError("REDIS_URL environment variable not set!")

# -------------------------------
# REDIS CONFIGURATION
# -------------------------------
parsed_url = urlparse(redis_url)
r = redis.Redis(
    host=parsed_url.hostname,
    port=parsed_url.port,
    password=parsed_url.password,
    decode_responses=True
)
logger.info("Connected to Redis at %s:%s", parsed_url.hostname, parsed_url.port)

# -------------------------------
# CONSTANTS
# -------------------------------
TERMS_KEY = "brevity_terms"
CHANNEL_MAP_KEY = "post_channels"
FREQ_KEY_PREFIX = "post_freq:"
LAST_POSTED_KEY_PREFIX = "last_posted:"
DISABLED_GUILDS_KEY = "disabled_posting"

# -------------------------------
# DISCORD CLIENT
# -------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# -------------------------------
# UTILITIES
# -------------------------------
def clean_term(term):
    # Retain square brackets for terms but remove them for definitions
    cleaned = term.replace("*", "").strip()
    return cleaned

def load_used_terms(guild_id):
    return list(r.smembers(f"used_terms:{guild_id}"))

def save_used_term(guild_id, term):
    r.sadd(f"used_terms:{guild_id}", term)
    logger.info("Saved used term '%s' for guild %s", term, guild_id)

def save_config(guild_id, channel_id):
    r.hset(CHANNEL_MAP_KEY, str(guild_id), channel_id)
    logger.info("Saved config: guild %s -> channel %s", guild_id, channel_id)

def load_config(guild_id=None):
    if guild_id:
        channel_id = r.hget(CHANNEL_MAP_KEY, str(guild_id))
        return {"channel_id": int(channel_id)} if channel_id else None
    else:
        all_configs = r.hgetall(CHANNEL_MAP_KEY)
        return {gid: {"channel_id": int(cid)} for gid, cid in all_configs.items()}

def set_post_frequency(guild_id, hours):
    r.set(f"{FREQ_KEY_PREFIX}{guild_id}", hours)
    logger.info("Set post frequency for guild %s to %s hours", guild_id, hours)

def get_post_frequency(guild_id):
    return int(r.get(f"{FREQ_KEY_PREFIX}{guild_id}") or 24)

def set_last_posted(guild_id, timestamp):
    r.set(f"{LAST_POSTED_KEY_PREFIX}{guild_id}", str(timestamp))

def get_last_posted(guild_id):
    ts = r.get(f"{LAST_POSTED_KEY_PREFIX}{guild_id}")
    return float(ts) if ts else 0.0

def is_posting_enabled(guild_id):
    return not r.sismember(DISABLED_GUILDS_KEY, str(guild_id))

def enable_posting(guild_id):
    r.srem(DISABLED_GUILDS_KEY, str(guild_id))
    logger.info("Posting enabled for guild %s", guild_id)

def disable_posting(guild_id):
    r.sadd(DISABLED_GUILDS_KEY, str(guild_id))
    logger.info("Posting disabled for guild %s", guild_id)


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
            logger.warning("No Flickr photos found.")
            return None
        photo = random.choice(photos)
        return f"https://farm{photo['farm']}.staticflickr.com/{photo['server']}/{photo['id']}_{photo['secret']}_b.jpg"
    except Exception as e:
        logger.error("Flickr fetch error: %s", e)
        return None
    
def parse_brevity_terms():

    url = "https://en.wikipedia.org/wiki/Multi-service_tactical_brevity_code"
    response = requests.get(url)
    soup = BeautifulSoup(response.content, "html.parser")
    content_div = soup.find("div", class_="mw-parser-output")
    terms = []

    if not content_div:
        logger.warning("Couldn't find Wikipedia content container.")
        return terms

    tags = list(content_div.find_all(["h2", "dt", "dd", "ul", "ol"]))
    current_term = None
    current_definition_parts = []

    def flush_term():
        nonlocal current_term, current_definition_parts
        if current_term and current_definition_parts:
            definition = "\n".join(current_definition_parts).strip()
            cleaned_term = clean_term(current_term)  # Use the updated clean_term function
            terms.append({
                "term": cleaned_term,
                "definition": definition
            })
        current_term = None
        current_definition_parts = []

    for tag in tags:
        if tag.name == "h2":
            heading_text = tag.get_text(" ", strip=True).lower()
            if any(x in heading_text for x in ["see also", "references", "footnotes", "sources"]):
                logger.debug("Stopping parse at section: %s", heading_text)
                break
        elif tag.name == "dt":
            flush_term()
            for sup in tag.find_all("sup"):  # Remove [citation needed], etc.
                sup.decompose()
            current_term = tag.get_text(" ", strip=True)
            current_definition_parts = []
        elif tag.name == "dd" and current_term:
            for sup in tag.find_all("sup"):
                sup.decompose()
            for span in tag.find_all("span"):
                span.decompose()
            text = tag.get_text(" ", strip=True)
            text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)  # Clean [[wiki|link]]
            current_definition_parts.append(text)
        elif tag.name in ["ul", "ol"] and current_term:
            bullets = [f"- {li.get_text(' ', strip=True)}" for li in tag.find_all("li")]
            current_definition_parts.extend(bullets)

    flush_term()
    logger.info("Parsed %d brevity terms from HTML.", len(terms))
    return terms


def update_brevity_terms():
    logger.info("Refreshing brevity terms from Wikipedia...")
    new_terms = parse_brevity_terms()

    if not new_terms:
        logger.warning("No brevity terms parsed. Keeping existing terms.")
        return 0, 0, 0 

    logger.info("Parsed %d brevity terms. Comparing with existing terms.", len(new_terms))

    # Load existing terms
    existing_raw = r.get(TERMS_KEY)
    existing_terms = json.loads(existing_raw) if existing_raw else []

    # Create dictionaries for comparison
    existing_terms_dict = {term['term']: term for term in existing_terms}
    new_terms_dict = {term['term']: term for term in new_terms}

    # Determine added, updated, and unchanged terms
    added_terms = [term for term in new_terms if term['term'] not in existing_terms_dict]
    updated_terms = [term for term in new_terms if term['term'] in existing_terms_dict and term != existing_terms_dict[term['term']]]
    unchanged_terms = [term for term in new_terms if term['term'] in existing_terms_dict and term == existing_terms_dict[term['term']]]

    # Backup current terms
    if existing_raw:
        r.set(f"{TERMS_KEY}_backup", existing_raw)
        logger.info("Backed up existing terms to TERMS_KEY_backup.")

    # Update Redis with new terms
    r.set(TERMS_KEY, json.dumps(new_terms))

    logger.info(
        "Successfully loaded %d brevity terms. Added: %d, Updated: %d, Unchanged: %d.",
        len(new_terms), len(added_terms), len(updated_terms), len(unchanged_terms)
    )

    return len(new_terms), len(added_terms), len(updated_terms)

def get_all_terms():
    terms_data = r.get(TERMS_KEY)
    if not terms_data:
        return []

    try:
        return json.loads(terms_data)
    except json.JSONDecodeError as e:
        logger.error("Failed to decode terms data from Redis: %s", e)
        return []

def get_next_brevity_term(guild_id):
    all_terms = get_all_terms()
    if not all_terms:
        logger.warning("No brevity terms available.")
        return None

    used_terms = load_used_terms(guild_id)
    unused_terms = [t for t in all_terms if t["term"] not in used_terms]

    if not unused_terms:
        # All terms have been used — reset
        logger.info("All terms used for guild %s, resetting used terms.", guild_id)
        r.delete(f"used_terms:{guild_id}")
        used_terms = []
        unused_terms = all_terms  # now full list again

    chosen = random.choice(unused_terms)
    save_used_term(guild_id, chosen["term"])
    return chosen


def get_brevity_term_by_name(name):
    for term in get_all_terms():
        if term["term"].lower() == name.lower():
            return term
    logger.info("No match found for brevity term: '%s'", name)
    return None


# -------------------------------
# SLASH COMMANDS
# -------------------------------
@tree.command(name="setup", description="Set the current channel for daily brevity posts.")
async def setup(interaction: discord.Interaction):
    save_config(interaction.guild.id, interaction.channel.id)
    enable_posting(interaction.guild.id)
    await interaction.response.send_message(f"Setup complete for <#{interaction.channel.id}>.", ephemeral=True)

@tree.command(name="nextterm", description="Send a new brevity term immediately.")
async def nextterm(interaction: discord.Interaction):
    try:
        await interaction.response.defer(thinking=True)  # <-- always defer immediately
    except discord.NotFound:
        logger.warning("Interaction expired before defer could happen.")
        return

    term = get_next_brevity_term(interaction.guild.id)
    if not term:
        await interaction.followup.send("No terms available.", ephemeral=True)
        return
    embed = discord.Embed(
        title=term['term'],
        description=term['definition'],
        color=discord.Color.blue(),
        url=f"https://en.wikipedia.org/wiki/Multi-service_tactical_brevity_code#{term['term'][0]}"
    )
    image_url = get_random_flickr_jet(FLICKR_API_KEY)
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(text="From Wikipedia – Multi-service Tactical Brevity Code")
    await interaction.followup.send(embed=embed)

@tree.command(name="reloadterms", description="Manually refresh brevity terms from Wikipedia.")
async def reloadterms(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)  # <-- defer immediately
    except discord.NotFound:
        logger.warning("Interaction expired before defer could happen.")
        return

    total, added, updated = update_brevity_terms()
    await interaction.followup.send(
        f"Terms synced from Wiki. Added: {added}. Updated: {updated}. Total: {total}.", ephemeral=True
    )
    logger.info("Manual reload triggered by guild %s", interaction.guild.id)


async def autocomplete_terms(interaction: discord.Interaction, current: str):
    all_terms = get_all_terms()
    if current.strip():
        # Filter terms based on user input
        filtered_terms = [
            app_commands.Choice(name=term["term"], value=term["term"])
            for term in all_terms if current.lower() in term["term"].lower()
        ]
        return filtered_terms[:25]  # Limit to 25 choices
    else:
        # Return a random set of 25 terms if no input is provided
        random_terms = random.sample(all_terms, min(25, len(all_terms)))
        return [app_commands.Choice(name=term["term"], value=term["term"]) for term in random_terms]

@tree.command(name="define", description="Look up the definition of a brevity term.")
@app_commands.describe(term="The brevity term to define")
@app_commands.autocomplete(term=autocomplete_terms)
async def define(interaction: discord.Interaction, term: str):
    entry = get_brevity_term_by_name(term)
    if not entry:
        await interaction.response.send_message(f"No definition found for '{term}'.", ephemeral=True)
        return
    embed = discord.Embed(
        title=entry['term'],
        description=entry['definition'],
        color=discord.Color.green(),
        url=f"https://en.wikipedia.org/wiki/Multi-service_tactical_brevity_code#{entry['term'][0]}"
    )
    await interaction.response.send_message(embed=embed)

@tree.command(name="setfrequency", description="Set the post frequency in hours for this server.")
@app_commands.describe(hours="Number of hours between posts")
async def setfrequency(interaction: discord.Interaction, hours: int):
    if hours <= 0:
        await interaction.response.send_message("Frequency must be a positive integer.", ephemeral=True)
        return
    set_post_frequency(interaction.guild.id, hours)
    await interaction.response.send_message(f"Frequency set to {hours} hour(s).", ephemeral=True)

@tree.command(name="disableposting", description="Stop scheduled brevity term posts in this server.")
async def disableposting(interaction: discord.Interaction):
    disable_posting(interaction.guild.id)
    await interaction.response.send_message("Scheduled posting disabled.", ephemeral=True)

@tree.command(name="enableposting", description="Resume scheduled brevity term posts in this server.")
async def enableposting(interaction: discord.Interaction):
    enable_posting(interaction.guild.id)
    await interaction.response.send_message("Scheduled posting enabled.", ephemeral=True)

@tree.command(name="quiz", description="Start a multiple-choice quiz on brevity terms")
@app_commands.describe(questions="Number of questions to answer")
async def quiz(interaction: discord.Interaction, questions: int = 1):
    all_terms = get_all_terms()
    max_questions = len(all_terms)
    if max_questions < 4:
        await interaction.response.send_message("Not enough terms to generate a quiz.", ephemeral=True)
        return
    if questions < 1 or questions > max_questions:
        await interaction.response.send_message(f"Number of questions must be between 1 and {max_questions}.", ephemeral=True)
        return

    # Shuffle and pick unique terms for questions
    quiz_terms = random.sample(all_terms, questions)
    score = {"correct": 0, "total": questions}

    async def ask_question(q_idx):
        current = quiz_terms[q_idx]
        correct_term = current["term"]
        def pick_single_definition(defn):
            defn = defn.strip()
            if re.match(r"^1\. ", defn):
                parts = re.split(r"\d+\. ", defn)
                for part in parts:
                    if part.strip():
                        return part.strip()
            for line in defn.split("\n"):
                if line.strip():
                    return line.strip()
            return defn
        correct_def = pick_single_definition(current["definition"])
        # Pick 3 incorrect answers
        incorrect_terms = [t for t in all_terms if t["term"] != correct_term]
        incorrect_choices = random.sample(incorrect_terms, 3)
        options = []
        for t in incorrect_choices:
            d = pick_single_definition(t["definition"])
            options.append({"term": t["term"], "definition": d, "is_correct": False})
        options.append({"term": correct_term, "definition": correct_def, "is_correct": True})
        random.shuffle(options)
        embed = discord.Embed(
            title=f"Question {q_idx+1}/{questions}: What is the correct definition for: {correct_term}?",
            color=discord.Color.orange()
        )
        option_labels = ["A", "B", "C", "D"]
        for idx, opt in enumerate(options):
            embed.add_field(name=f"{option_labels[idx]}", value=opt["definition"], inline=False)
        class QuizView(discord.ui.View):
            def __init__(self, options):
                super().__init__(timeout=60)
                self.options = options
            @discord.ui.button(label="A", style=discord.ButtonStyle.primary)
            async def optionA(self, interaction_btn: discord.Interaction, button: discord.ui.Button):
                await self.handle_answer(interaction_btn, 0)
            @discord.ui.button(label="B", style=discord.ButtonStyle.primary)
            async def optionB(self, interaction_btn: discord.Interaction, button: discord.ui.Button):
                await self.handle_answer(interaction_btn, 1)
            @discord.ui.button(label="C", style=discord.ButtonStyle.primary)
            async def optionC(self, interaction_btn: discord.Interaction, button: discord.ui.Button):
                await self.handle_answer(interaction_btn, 2)
            @discord.ui.button(label="D", style=discord.ButtonStyle.primary)
            async def optionD(self, interaction_btn: discord.Interaction, button: discord.ui.Button):
                await self.handle_answer(interaction_btn, 3)
            async def handle_answer(self, interaction_btn, idx):
                if interaction_btn.user.id != interaction.user.id:
                    await interaction_btn.response.send_message("This quiz is not for you!", ephemeral=True)
                    return
                if self.options[idx]["is_correct"]:
                    msg = f"✅ Correct! {option_labels[idx]}. {self.options[idx]['definition']}"
                    score["correct"] += 1
                else:
                    correct_idx = next(i for i, o in enumerate(self.options) if o["is_correct"])
                    msg = f"❌ Incorrect. The correct answer was {option_labels[correct_idx]}: {self.options[correct_idx]['definition']}"
                await interaction_btn.response.send_message(msg, ephemeral=True)
                self.stop()
                # Ask next question or finish
                if q_idx + 1 < questions:
                    await ask_question(q_idx + 1)
                else:
                    await interaction.followup.send(f"Quiz complete! You got {score['correct']} out of {score['total']} correct.", ephemeral=True)
        if q_idx == 0:
            await interaction.response.send_message(embed=embed, view=QuizView(options), ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, view=QuizView(options), ephemeral=True)
    await ask_question(0)
# -------------------------------
# BACKGROUND TASKS
# -------------------------------
@tasks.loop(minutes=5)
async def post_brevity_term():
    all_configs = load_config()
    for guild_id_str, config in all_configs.items():
        try:
            guild_id = int(guild_id_str)
            if not is_posting_enabled(guild_id):
                continue

            freq_hours = get_post_frequency(guild_id)
            last_posted = get_last_posted(guild_id)
            next_post_time = last_posted + (freq_hours * 3600)

            # Check if it's time to post (allowing a ±5-minute window)
            current_time = time.time()
            if current_time < next_post_time - 300 or current_time > next_post_time + 300:
                continue

            channel = client.get_channel(config["channel_id"])
            if not channel:
                logger.warning("Channel %s not found for guild %s. Removing stale config.", config['channel_id'], guild_id)
                r.hdel(CHANNEL_MAP_KEY, str(guild_id))
                continue

            term = get_next_brevity_term(guild_id)
            if not term:
                continue

            embed = discord.Embed(
                title=term['term'],
                description=term['definition'],
                color=discord.Color.blue(),
                url=f"https://en.wikipedia.org/wiki/Multi-service_tactical_brevity_code#{term['term'][0]}"
            )
            image_url = get_random_flickr_jet(FLICKR_API_KEY)
            if image_url:
                embed.set_image(url=image_url)
            embed.set_footer(text="From Wikipedia – Multi-service Tactical Brevity Code")

            await channel.send(embed=embed)
            set_last_posted(guild_id, next_post_time)  # Set the next post time based on the fixed schedule
            logger.info("Posted term '%s' to guild %s (#%s)", term['term'], guild_id, config["channel_id"])

        except Exception as e:
            logger.error("Failed to post to guild %s: %s", guild_id_str, e)


@tasks.loop(hours=24)
async def refresh_terms_daily():
    update_brevity_terms()

@tasks.loop(hours=1)
async def log_bot_stats():
    num_servers = len(client.guilds)
    total_words = int(r.get("total_words") or 0)
    command_usage = json.loads(r.get("command_usage") or "{}")

    # Format command usage stats
    command_usage_stats = ", ".join([f"{cmd}: {count}" for cmd, count in command_usage.items()])

    # Log the stats to the console
    logger.info(
        "BrevityBot Statistics: Servers: %d, Words sent per day: %d, Slash command usage: %s",
        num_servers, total_words, command_usage_stats
    )

# -------------------------------
# BOT READY EVENT
# -------------------------------
@client.event
async def on_ready():
    logger.info("Logged in as %s (ID: %s)", client.user.name, client.user.id)
    await tree.sync()
    if not post_brevity_term.is_running():
        post_brevity_term.start()
    if not refresh_terms_daily.is_running():
        refresh_terms_daily.start()
    if not log_bot_stats.is_running():
        log_bot_stats.start()


if __name__ == "__main__":
    logger.info("Starting BrevityBot...")
    client.run(DISCORD_BOT_TOKEN)


@client.event
async def on_guild_remove(guild):
    guild_id = guild.id
    logger.info("Removed from guild %s (%s). Cleaning up Redis data...", guild_id, guild.name)
    r.hdel(CHANNEL_MAP_KEY, str(guild_id))
    r.delete(f"used_terms:{guild_id}")
    r.delete(f"{FREQ_KEY_PREFIX}{guild_id}")
    r.delete(f"{LAST_POSTED_KEY_PREFIX}{guild_id}")
    r.srem(DISABLED_GUILDS_KEY, str(guild_id))

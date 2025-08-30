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
from datetime import timedelta
import asyncio
import textwrap

# Load environment variables from a .env file before anything else
load_dotenv()

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
# load_dotenv() was called at import time to ensure LOG_LEVEL and other
# settings are available before configuration

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


def sanitize_definition_for_quiz(defn: str, term: str, min_len: int = 15) -> str:
    """Return a quiz-safe version of `defn` that masks literal mentions of `term`.
    Replaces quoted examples like "GADABOUT 25" and bare occurrences of the term
    with underscores (same length) or a short placeholder, while trying to keep
    surrounding context. Ensures multi-line blockquote formatting is preserved by
    returning text with original newlines intact.
    """
    if not defn:
        return defn

    s = defn
    # Remove quoted examples like "GADABOUT 25" or 'GADABOUT 16-24' -> [example]
    s = re.sub(rf'"{re.escape(term)}\s*\d+(?:-\d+)?"', '[example]', s, flags=re.IGNORECASE)
    # Remove bare examples like GADABOUT 25 or GADABOUT 16-24
    s = re.sub(rf'{re.escape(term)}\s*\d+(?:-\d+)?', '[example]', s, flags=re.IGNORECASE)

    # Replace standalone term occurrences with same-length underscores to mask
    def _mask(m):
        # Prefer a 6-8 underscore mask to avoid revealing exact length but keep a visible mask
        return '_' * max(6, min(8, len(m.group(0))))

    s = re.sub(rf'\b{re.escape(term)}\b', _mask, s, flags=re.IGNORECASE)

    # Collapse accidental double-spaces from removals
    # Preserve a single space before the mask if the term had leading whitespace
    s = re.sub(r'\s{2,}', ' ', s).strip()

    # If any existing short underscore masks (e.g. '_', '__', etc.) remain from
    # previous processing or raw data, normalize them to at least 6 underscores so
    # they don't reveal length clues and are visually consistent.
    s = re.sub(r'(?<!_)_{1,5}(?!_)', '______', s)

    # If the result is too short, try removing sentences that contain the term
    if len(s) < min_len:
        parts = re.split(r'(?<=[.!?])\s+', defn)
        kept = [p for p in parts if not re.search(rf'\b{re.escape(term)}\b', p, flags=re.IGNORECASE)]
        candidate = ' '.join(kept).strip()
        candidate = re.sub(r'\s{2,}', ' ', candidate)
        if len(candidate) >= min_len:
            return candidate
        # Fallback: return the first sentence with the term masked
        first = parts[0] if parts else defn
        first = re.sub(rf'\b{re.escape(term)}\b', lambda m: '_' * len(m.group(0)), first, flags=re.IGNORECASE).strip()
        return first if len(first) >= 5 else '[definition omitted for quiz]'

    return s
    
def parse_brevity_terms():

    url = "https://en.wikipedia.org/wiki/Multi-service_tactical_brevity_code"
    # Use a polite, identifiable User-Agent. Allow override via USER_AGENT env var.
    user_agent = os.getenv("USER_AGENT") or "BrevityBot/1.0 (+https://github.com/joemurrell/brevitybot)"
    headers = {"User-Agent": user_agent}
    try:
        response = requests.get(url, headers=headers, timeout=15)
    except Exception as e:
        logger.error("Failed to fetch brevity terms page: %s", e)
        return []
    # If Wikipedia rejected us with 403, do a single diagnostic retry with a common browser UA
    if getattr(response, 'status_code', None) == 403:
        logger.warning("Received 403 fetching brevity terms with User-Agent '%s'. Trying a diagnostic retry with a browser UA.", user_agent)
        fallback_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"}
        try:
            response = requests.get(url, headers=fallback_headers, timeout=15)
            logger.debug("Retry with browser User-Agent returned status %s", getattr(response, 'status_code', 'N/A'))
        except Exception as e:
            logger.error("Diagnostic retry failed: %s", e)
            return []
    # Log HTTP status and size for debugging
    try:
        content_len = len(response.content or b"")
    except Exception:
        content_len = 0
    logger.debug("Fetched %s (status=%s length=%s)", url, getattr(response, 'status_code', 'N/A'), content_len)
    if getattr(response, 'status_code', 200) != 200:
        logger.error("Non-200 response while fetching brevity terms: %s", getattr(response, 'status_code', None))
        # still attempt to parse body if present
    soup = BeautifulSoup(response.content, "html.parser")
    content_div = soup.find("div", class_="mw-parser-output")
    terms = []

    if not content_div:
        # Dump a short snippet of the page to logs to help debugging
        snippet = (response.text[:1000] + "...") if response is not None and response.text else ""
        logger.error("Couldn't find Wikipedia content container. Page snippet:\n%s", snippet)
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

class PublicQuizView(discord.ui.View):
    def __init__(self, question_idx, correct_idx, quiz_id, redis_conn, timeout=None):
        super().__init__(timeout=timeout)
        self.question_idx = question_idx
        self.correct_idx = correct_idx
        self.quiz_id = quiz_id
        self.redis_conn = redis_conn
        self.answered_users = set()

    async def handle_vote(self, interaction, answer_idx):
        user_id = str(interaction.user.id)
        if user_id in self.answered_users:
            await interaction.response.send_message("You have already answered this question.", ephemeral=True)
            return
        self.answered_users.add(user_id)
        # Store the user's answer in Redis
        self.redis_conn.hset(f"quiz:{self.quiz_id}:answers:{self.question_idx}", user_id, answer_idx)
        option_labels = ["A", "B", "C", "D"]
        q_number = self.question_idx + 1 if self.question_idx is not None else "?"
        selected_label = option_labels[answer_idx] if 0 <= answer_idx < len(option_labels) else str(answer_idx)
        await interaction.response.send_message(
            f"Q{q_number}: You selected {selected_label}",
            ephemeral=True
        )

    @discord.ui.button(label="A", style=discord.ButtonStyle.primary)
    async def button_a(self, interaction, button): await self.handle_vote(interaction, 0)

    @discord.ui.button(label="B", style=discord.ButtonStyle.primary)
    async def button_b(self, interaction, button): await self.handle_vote(interaction, 1)

    @discord.ui.button(label="C", style=discord.ButtonStyle.primary)
    async def button_c(self, interaction, button): await self.handle_vote(interaction, 2)

    @discord.ui.button(label="D", style=discord.ButtonStyle.primary)
    async def button_d(self, interaction, button): await self.handle_vote(interaction, 3)

@tree.command(name="quiz", description="Start a multiple-choice quiz on brevity terms")
@app_commands.describe(questions="Number of questions to answer", mode="Quiz mode: public (poll in channel) or private (ephemeral message)", duration="Total quiz duration in minutes (public mode only)")
async def quiz(
    interaction: discord.Interaction,
    questions: int = 1,
    mode: str = "private",
    duration: int = 2
):
    logger.info(f"/quiz invoked by user={interaction.user.id} guild={interaction.guild_id} questions={questions} mode={mode} duration={duration}")
    all_terms = get_all_terms()
    max_questions = len(all_terms)
    if max_questions < 4:
        await interaction.response.send_message("Not enough terms to generate a quiz.", ephemeral=True)
        return
    if questions < 1 or questions > max_questions:
        await interaction.response.send_message(f"Number of questions must be between 1 and {max_questions}.", ephemeral=True)
        return

    quiz_terms = random.sample(all_terms, questions)
    score = {"correct": 0, "total": questions}

    if mode == "private":
        async def ask_question(q_idx):
            current = quiz_terms[q_idx]
            correct_term = current["term"]
            def pick_single_definition(defn):
                defn = defn.strip()
                if re.match(r"^1\\. ", defn):
                    parts = re.split(r"\\d+\\. ", defn)
                    for part in parts:
                        if part.strip():
                            return part.strip()
                for line in defn.split("\n"):
                    if line.strip():
                        return line.strip()
                return defn
            correct_def = pick_single_definition(current["definition"])
            incorrect_terms = [t for t in all_terms if t["term"] != correct_term]
            incorrect_choices = random.sample(incorrect_terms, 3)
            options = []
            for t in incorrect_choices:
                d = pick_single_definition(t["definition"])
                options.append({"term": t["term"], "definition": d, "is_correct": False})
            options.append({"term": correct_term, "definition": correct_def, "is_correct": True})
            random.shuffle(options)
            option_labels = ["A", "B", "C", "D"]
            if mode == "private":
                # Use fields for each option for better readability
                embed = discord.Embed(
                    title=f"Question {q_idx+1}/{questions}",
                    description=f"What is the correct definition of: **{correct_term}**",
                    color=discord.Color.orange()
                )
                # build description (short one-line summaries)
                lines = []
                for idx, opt in enumerate(options):
                    short = textwrap.shorten(opt["definition"], width=200, placeholder="…")
                    lines.append(f"{option_labels[idx]} {short}")
                embed.description = embed.description + "\n".join(lines)

                # Render each option as an empty-name field with a blockquote value so
                # the label and definition appear on the same visible line. Use the
                # sanitizer to mask instances of the term in the displayed text.
                for idx, opt in enumerate(options):
                    raw_def = opt.get("definition", "")
                    # Only mask occurrences of the current quiz term (correct_term),
                    # not other brevity terms that may appear in option text.
                    display = sanitize_definition_for_quiz(raw_def, correct_term)
                    # ensure multi-line definitions stay inside the blockquote by
                    # prefixing each line with '> '
                    display_block = "\n".join(["> " + line for line in display.splitlines()])
                    # store display text for later reference in summaries if needed
                    opt["display"] = display
                    embed.add_field(name="", value=f"`{option_labels[idx]}` {display_block}", inline=False)
                embed.set_footer(text=f"Question {q_idx+1} of {questions}")
                embed.timestamp = discord.utils.utcnow()
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
                        if q_idx + 1 < questions:
                            await ask_question(q_idx + 1)
                        else:
                            await interaction.followup.send(f"Quiz complete! You got {score['correct']} out of {score['total']} correct.", ephemeral=True)
                if q_idx == 0:
                    await interaction.response.send_message(embed=embed, view=QuizView(options), ephemeral=True)
                else:
                    await interaction.followup.send(embed=embed, view=QuizView(options), ephemeral=True)
            else:
                await interaction.followup.send("Private quizzes are not supported in this version.", ephemeral=True)
        await ask_question(0)
        return

    # PUBLIC MODE: post all questions at once, keep open for total duration
    import time
    quiz_id = f"{interaction.guild_id or 'guild'}-{interaction.user.id}-{int(time.time())}"
    embeds = []
    views = []
    correct_indices = []
    used_options = []  # store the options used for each question (so summary matches options)
    messages = []
    option_labels = ["A", "B", "C", "D"]
    for q_idx, current in enumerate(quiz_terms):
        correct_term = current["term"]
        def pick_single_definition(defn):
            """Return a single definition string.
            If the definition contains numbered variants (e.g. "1. ... 2. ..."),
            split them and return a randomly chosen variant. Otherwise fall back
            to choosing a non-empty line or the whole definition.
            """
            defn = defn.strip()
            # Split numbered variants like '1. ... 2. ...' or '1) ... 2) ...'
            parts = re.split(r"\d+\.\s*|\d+\)\s*", defn)
            parts = [p.strip() for p in parts if p.strip()]
            # Clean and keep only parts that contain letters (avoid numeric-only fragments)
            good_parts = []
            for p in parts:
                # remove stray leading bullets or remaining digits, but KEEP leading '[' characters
                # (some definitions are bracketed like '[state system] ...' and the '[' is meaningful)
                cleaned = re.sub(r"^[\s\-\(\)\.]*\d*[\)\.\-]*\s*", "", p).strip()
                if re.search(r"[A-Za-z]", cleaned) and len(cleaned) >= 5:
                    good_parts.append(cleaned)
            if len(good_parts) > 1:
                chosen = random.choice(good_parts)
                logger.info(f"Picked numbered variant: {chosen[:80]}")
                return chosen
            # If we found exactly one reasonable part, use it
            if len(good_parts) == 1:
                return good_parts[0]
            # Fallback: split by non-empty lines and pick one that contains letters
            lines = [l.strip() for l in defn.splitlines() if l.strip()]
            good_lines = [l for l in lines if re.search(r"[A-Za-z]", l) and len(l) >= 5]
            if len(good_lines) >= 1:
                chosen = random.choice(good_lines)
                logger.info(f"Picked line-variant: {chosen[:80]}")
                return chosen
            return defn
        correct_def = pick_single_definition(current["definition"])
        incorrect_terms = [t for t in all_terms if t["term"] != correct_term]
        incorrect_choices = random.sample(incorrect_terms, 3)
        options = []
        for t in incorrect_choices:
            d = pick_single_definition(t["definition"])
            options.append({"term": t["term"], "definition": d, "is_correct": False})
        options.append({"term": correct_term, "definition": correct_def, "is_correct": True})
        random.shuffle(options)
        correct_idx = next(i for i, o in enumerate(options) if o["is_correct"])
        correct_indices.append(correct_idx)
        logger.info(f"Built question {q_idx+1}/{questions} term='{correct_term}' correct_idx={correct_idx}")
        # Build a cleaner embed with fields for each multiple-choice option
        embed = discord.Embed(
            title=f"Brevity Quiz!",
            description=f"What is the correct definition of:\n**{correct_term}**\n\n",
            color=discord.Color.orange()
        )
        # Format each option so the label and definition appear on one visible line.
        # Place the letter+definition into the field value as a blockquote and
        # leave the field name empty. Truncate very long definitions to stay
        # comfortably under Discord's embed limits.
        letter_labels = ["A", "B", "C", "D"]
        for idx, opt in enumerate(options):
            label = letter_labels[idx] if idx < len(letter_labels) else str(idx)
            raw_def = opt.get("definition", "")
            # Only mask occurrences of the current quiz term (correct_term),
            # not other brevity terms that may appear in option text.
            display = sanitize_definition_for_quiz(raw_def, correct_term)
            # prefix each line with '> ' so the entire multi-line definition stays inside the blockquote
            display_block = "\n".join([" " + line for line in display.splitlines()])
            opt["display"] = display
            embed.add_field(name="", value=f"**`{label}`** {display_block}", inline=False)
        embed.set_footer(text=f"Question {q_idx+1} of {questions} • Quiz initiated by {interaction.user.display_name}")
        view = PublicQuizView(q_idx, correct_idx, quiz_id, r, timeout=duration*60)
        embeds.append(embed)
        views.append(view)
        used_options.append(options)

    # Post all questions
    channel = interaction.channel
    # Defensive checks before sending to capture permission issues early
    if channel is None:
        logger.error("interaction.channel is None for guild=%s user=%s", interaction.guild_id, interaction.user.id)
        await interaction.response.send_message("Couldn't determine the channel to post the quiz in. Please run this command in a server text channel.", ephemeral=True)
        return

    # Find bot member for permission checks
    bot_member = None
    try:
        bot_member = interaction.guild.get_member(client.user.id) if interaction.guild else None
    except Exception:
        bot_member = None

    try:
        perms = channel.permissions_for(bot_member) if bot_member and hasattr(channel, 'permissions_for') else None
    except Exception:
        perms = None

    logger.debug("Posting quiz: guild=%s channel=%s bot_member=%s perms=%s", interaction.guild_id, getattr(channel, 'id', None), getattr(bot_member, 'id', None) if bot_member else None, perms)
    if perms and not perms.send_messages:
        logger.error("Bot missing send_messages permission in channel %s for guild %s", channel.id, interaction.guild_id)
        await interaction.response.send_message("I don't have permission to post in that channel (missing Send Messages). Please check my role and channel permissions.", ephemeral=True)
        return

    for embed, view in zip(embeds, views):
        try:
            msg = await channel.send(embed=embed, view=view)
        except Exception as e:
            # Log full context for diagnosis and return a helpful ephemeral message to the user
            try:
                channel_perms = channel.permissions_for(bot_member) if bot_member and hasattr(channel, 'permissions_for') else None
            except Exception:
                channel_perms = None
            logger.error("Failed to post quiz message to channel=%s guild=%s: %s", getattr(channel, 'id', None), interaction.guild_id, e)
            logger.error("Channel perms for bot: %s", channel_perms)

            # Detect discord Forbidden (missing access) when possible
            is_forbidden = False
            try:
                import discord as _discord
                is_forbidden = isinstance(e, _discord.Forbidden)
            except Exception:
                # Fallback by name if import detection fails
                is_forbidden = e.__class__.__name__ == 'Forbidden'

            if is_forbidden:
                likely = ["View Channels", "Send Messages", "Embed Links", "Read Message History"]
                user_msg = (
                    f"I couldn't post the quiz in <#{getattr(channel, 'id', 'this channel')}> because I lack access. "
                    f"Please ensure I have these permissions: {', '.join(likely)}. "
                    "Also check channel permission overrides and that my role can view/send messages."
                )
            else:
                user_msg = (
                    "I couldn't post the quiz messages due to an unexpected error. "
                    "Check my role and channel permissions, and see the bot logs for details."
                )

            # Try to inform the user via the interaction response or followup
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(user_msg, ephemeral=True)
                else:
                    await interaction.followup.send(user_msg, ephemeral=True)
            except Exception:
                logger.error("Also failed to send ephemeral response to the user about missing access.")
            return
        view.message = msg
        view.message_id = msg.id
        messages.append(msg)
        logger.info(f"Posted quiz message id={msg.id} quiz_id={quiz_id} q_index={view.question_idx}")

    # Announce the quiz as a polished embed instead of a plain message
    minute_label = "minute" if duration == 1 else "minutes"
    question_label = "question" if questions == 1 else "questions"
    start_embed = discord.Embed(
        title="Brevity quiz started!",
        description=f"You have **{duration}** {minute_label} to answer **{questions}** {question_label}.",
        color=discord.Color.orange()
    )
    start_embed.set_footer(text=f"Quiz initiated by {interaction.user.display_name}")
    await interaction.response.send_message(embed=start_embed, ephemeral=False)

    # Wait for the total duration, then collect and post results
    from datetime import datetime, timedelta
    async def close_and_summarize():
        logger.info(f"close_and_summarize sleeping until timeout for quiz_id={quiz_id}")
        await discord.utils.sleep_until(discord.utils.utcnow() + timedelta(seconds=duration*60))
        logger.info(f"close_and_summarize started for quiz_id={quiz_id}")
        # Build an embed for results instead of a long plain-text message
        results_embed = discord.Embed(title="Brevity Quiz Results", color=discord.Color.green())
        user_correct = {}  # user_id -> correct count for this quiz
        user_participation = set()
        for i, view in enumerate(views):
            answers = r.hgetall(f"quiz:{quiz_id}:answers:{i}")
            logger.info(f"Fetched answers for quiz_id={quiz_id} q_index={i} count={len(answers)}")
            correct_users = [uid for uid, idx in answers.items() if str(idx) == str(view.correct_idx)]
            for uid, idx in answers.items():
                user_participation.add(uid)
                if str(idx) == str(view.correct_idx):
                    user_correct[uid] = user_correct.get(uid, 0) + 1
            correct_mentions = ", ".join(f"<@{uid}>" for uid in correct_users) or "None"
            term = quiz_terms[i]["term"]
            # Use the option text used in the multiple choice for the correct definition
            opt = used_options[i][view.correct_idx]
            correct_def_short = opt["definition"]
            # Add a field per question to the results embed for clarity
            field_name = f"`Q{i+1}:` {term}"
            field_value = f"**`Answer:`**  **{option_labels[view.correct_idx]}** - {correct_def_short}\n **`Correct users:`** {correct_mentions}\n"
            results_embed.add_field(name=field_name, value=field_value, inline=False)

        # Save each user's result to Redis (capped at 10)
        total = len(views)
        for uid in user_participation:
            correct = user_correct.get(uid, 0)
            entry = json.dumps({"correct": correct, "total": total, "ts": int(time.time())})
            key = f"greenie:{interaction.guild_id}:{uid}"
            r.lpush(key, entry)
            r.ltrim(key, 0, 9)  # Keep only last 10
            logger.info(f"Saved greenie entry for guild={interaction.guild_id} user={uid} -> {correct}/{total}")

        # Per-quiz leaderboard -> add as an embed field
        if user_correct:
            leaderboard = sorted(user_correct.items(), key=lambda x: -x[1])
            leaderboard_lines = []
            for uid, correct in leaderboard:
                percent = int(100 * correct / total)
                leaderboard_lines.append(f"<@{uid}> got {correct}/{total} ({percent}%)")
            results_embed.add_field(name="Quiz Leaderboard", value="\n".join(leaderboard_lines), inline=False)
        else:
            results_embed.add_field(name="Quiz Leaderboard", value="No correct answers this round.", inline=False)

        # Greenie Board (last 10 quizzes, 10 most recent users) -> add as an embed field
        greenie_keys = r.keys(f"greenie:{interaction.guild_id}:*")
        user_greenies = []
        for key in greenie_keys:
            uid = key.split(":")[-1]
            entries = [json.loads(e) for e in r.lrange(key, 0, 9)]
            if entries:
                avg = sum(e["correct"] / e["total"] for e in entries) / len(entries)
                user_greenies.append((uid, entries, avg))
        user_greenies.sort(key=lambda x: (-x[2], -max(e["ts"] for e in x[1])))
        greenie_lines = []
        for user in user_greenies[:10]:
            uid, entries, avg = user
            row = ""
            for e in entries:
                pct = e["correct"] / e["total"] if e["total"] else 0
                if pct >= 0.8:
                    row += "🟢"
                elif pct >= 0.5:
                    row += "🟡"
                else:
                    row += "🔴"
            row = row.ljust(10, "◻")
            avg_pct = int(avg * 100)
            greenie_lines.append(f"<@{uid}>: {row}  {avg_pct}%")
        if greenie_lines:
            results_embed.add_field(name="Greenie Board (Last 10 quizzes)", value="\n".join(greenie_lines), inline=False)
        logger.info(f"Sending summary embed for quiz_id={quiz_id} to channel={interaction.channel.id}")
        await interaction.channel.send(embed=results_embed)
    # Schedule the summary task
    import asyncio
    asyncio.create_task(close_and_summarize())
# Greenie Board command
@tree.command(name="greenieboard", description="Show the Greenie Board for this server (last 10 quizzes per user)")
async def greenieboard(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    greenie_keys = r.keys(f"greenie:{guild_id}:*")
    user_greenies = []
    for key in greenie_keys:
        uid = key.split(":")[-1]
        entries = [json.loads(e) for e in r.lrange(key, 0, 9)]
        if entries:
            avg = sum(e["correct"] / e["total"] for e in entries) / len(entries)
            user_greenies.append((uid, entries, avg))
    user_greenies.sort(key=lambda x: (-x[2], -max(e["ts"] for e in x[1])))
    board = "**Greenie Board (Last 10 Quizzes):**\n"
    for user in user_greenies[:10]:
        uid, entries, avg = user
        row = ""
        for e in entries:
            pct = e["correct"] / e["total"] if e["total"] else 0
            if pct >= 0.8:
                row += "🟢"
            elif pct >= 0.5:
                row += "🟡"
            else:
                row += "🔴"
            # ljust requires a single character fill; use single-codepoint '▫' instead of '▫️'
        row = row.ljust(10, "◻")
        avg_pct = int(avg * 100)
        board += f"<@{uid}>: {row}  {avg_pct}% average\n"
    # Send as a code block for fixed-width alignment
    board_code = "```" + "\n" + board + "```"
    await interaction.response.send_message(board_code, ephemeral=False)

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
            # Record the actual post time so future scheduling is based on when
            # the term was sent, not when it was supposed to be sent.
            set_last_posted(guild_id, time.time())
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


class PublicQuizView(discord.ui.View):
    def __init__(self, message_id, options, correct_idx, timeout=60):
        super().__init__(timeout=timeout)
        self.message_id = message_id
        self.options = options
        self.correct_idx = correct_idx
        self.votes = {}  # user_id -> selected_index
        self.message = None

    async def on_timeout(self):
        correct_users = [uid for uid, idx in self.votes.items() if idx == self.correct_idx]
        key = f"quiz_results:{self.message_id}"
        r.set(key, json.dumps({
            "correct_idx": self.correct_idx,
            "votes": self.votes
        }))
        r.expire(key, 6 * 3600)

        for uid, idx in self.votes.items():
            score_key = f"user_score:{uid}"
            prev = json.loads(r.get(score_key) or '{"correct": 0, "total": 0}')
            prev["correct"] += int(idx == self.correct_idx)
            prev["total"] += 1
            r.set(score_key, json.dumps(prev))

        correct_mentions = ", ".join(f"<@{uid}>" for uid in correct_users) or "None"
        answer_text = (
            f"The correct answer was **{['A', 'B', 'C', 'D'][self.correct_idx]}**: {self.options[self.correct_idx]['definition']}\n"
            f"✅ Correct users: {correct_mentions}"
        )

        await self.message.channel.send(answer_text)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id not in self.votes

    async def handle_vote(self, interaction, idx):
        self.votes[interaction.user.id] = idx
        await interaction.response.send_message(f"Vote recorded: {['A', 'B', 'C', 'D'][idx]}", ephemeral=True)

    @discord.ui.button(label="A", style=discord.ButtonStyle.primary)
    async def button_a(self, interaction, button): await self.handle_vote(interaction, 0)

    @discord.ui.button(label="B", style=discord.ButtonStyle.primary)
    async def button_b(self, interaction, button): await self.handle_vote(interaction, 1)

    @discord.ui.button(label="C", style=discord.ButtonStyle.primary)
    async def button_c(self, interaction, button): await self.handle_vote(interaction, 2)

    @discord.ui.button(label="D", style=discord.ButtonStyle.primary)
    async def button_d(self, interaction, button): await self.handle_vote(interaction, 3)

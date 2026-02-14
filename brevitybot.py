import discord
from discord.ext import tasks
from discord import app_commands
import os
import random
import aiohttp
from bs4 import BeautifulSoup
import re
import logging
import redis.asyncio as aioredis
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
# Redis client will be initialized in on_ready
r = None
logger.info("Redis URL configured for %s:%s", parsed_url.hostname, parsed_url.port)

# -------------------------------
# HEALTH CHECK SERVER
# -------------------------------
HEALTH_CHECK_PORT = int(os.getenv("HEALTH_CHECK_PORT", "8080"))

async def health_check_handler(request):
    """HTTP health check endpoint. Returns 200 if the bot is connected to Discord, 503 otherwise."""
    if client.is_ready() and not client.is_closed():
        latency_ms = round(client.latency * 1000)
        return aiohttp.web.json_response({
            "status": "healthy",
            "bot": str(client.user),
            "guilds": len(client.guilds),
            "latency_ms": latency_ms,
        })
    return aiohttp.web.json_response(
        {"status": "unhealthy", "reason": "Bot not connected to Discord"},
        status=503,
    )

async def start_health_check_server():
    """Start a lightweight HTTP server for Railway health checks."""
    app = aiohttp.web.Application()
    app.router.add_get("/health", health_check_handler)
    app.router.add_get("/", health_check_handler)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", HEALTH_CHECK_PORT)
    await site.start()
    logger.info("Health check server running on port %d", HEALTH_CHECK_PORT)

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

async def load_used_terms(guild_id):
    return list(await r.smembers(f"used_terms:{guild_id}"))

async def save_used_term(guild_id, term):
    await r.sadd(f"used_terms:{guild_id}", term)
    logger.info("Saved used term '%s' for guild %s", term, guild_id)

async def save_config(guild_id, channel_id):
    await r.hset(CHANNEL_MAP_KEY, str(guild_id), channel_id)
    logger.info("Saved config: guild %s -> channel %s", guild_id, channel_id)

async def load_config(guild_id=None):
    if guild_id:
        channel_id = await r.hget(CHANNEL_MAP_KEY, str(guild_id))
        return {"channel_id": int(channel_id)} if channel_id else None
    else:
        all_configs = await r.hgetall(CHANNEL_MAP_KEY)
        return {gid: {"channel_id": int(cid)} for gid, cid in all_configs.items()}

async def set_post_frequency(guild_id, hours):
    await r.set(f"{FREQ_KEY_PREFIX}{guild_id}", hours)
    logger.info("Set post frequency for guild %s to %s hours", guild_id, hours)

async def get_post_frequency(guild_id):
    return int(await r.get(f"{FREQ_KEY_PREFIX}{guild_id}") or 24)

async def set_last_posted(guild_id, timestamp):
    await r.set(f"{LAST_POSTED_KEY_PREFIX}{guild_id}", str(timestamp))

async def get_last_posted(guild_id):
    ts = await r.get(f"{LAST_POSTED_KEY_PREFIX}{guild_id}")
    return float(ts) if ts else 0.0

async def is_posting_enabled(guild_id):
    return not await r.sismember(DISABLED_GUILDS_KEY, str(guild_id))

async def enable_posting(guild_id):
    await r.srem(DISABLED_GUILDS_KEY, str(guild_id))
    logger.info("Posting enabled for guild %s", guild_id)

async def disable_posting(guild_id):
    await r.sadd(DISABLED_GUILDS_KEY, str(guild_id))
    logger.info("Posting disabled for guild %s", guild_id)


async def get_random_flickr_jet(api_key):
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
        async with aiohttp.ClientSession() as session:
            async with session.get(flickr_url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
                data = await response.json()
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

    # Collapse accidental double-spaces from removals (trim edges but keep internal spacing)
    s = re.sub(r'\s{2,}', ' ', s).strip()

    # Normalize any underscore runs to a visible, non-revealing mask.
    # Ensure masks are at least 6 underscores and prefix them with two spaces so
    # they stand out visually (e.g. '  ______'). This catches both newly-created
    # masks and any short underscore runs from source data.
    def _normalize_underscores(match):
        run = match.group(0)
        length = max(6, len(run))
        return '  ' + ('_' * length)

    s = re.sub(r'_{1,}', _normalize_underscores, s)

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
        # Mask occurrences in the fallback first sentence then normalize underscore masks
        first = re.sub(rf'\b{re.escape(term)}\b', lambda m: '_' * max(6, len(m.group(0))), first, flags=re.IGNORECASE).strip()
        first = re.sub(r'_{1,}', _normalize_underscores, first)
        return first if len(first) >= 5 else '[definition omitted for quiz]'

    return s
    
async def parse_brevity_terms():

    url = "https://en.wikipedia.org/wiki/Multi-service_tactical_brevity_code"
    # Use a polite, identifiable User-Agent. Allow override via USER_AGENT env var.
    user_agent = os.getenv("USER_AGENT") or "BrevityBot/1.0 (+https://github.com/joemurrell/brevitybot)"
    headers = {"User-Agent": user_agent}
    status = None
    content = None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as response:
                content = await response.read()
                status = response.status
    except Exception as e:
        logger.error("Failed to fetch brevity terms page: %s", e)
        return []
    # If Wikipedia rejected us with 403, do a single diagnostic retry with a common browser UA
    if status == 403:
        logger.warning("Received 403 fetching brevity terms with User-Agent '%s'. Trying a diagnostic retry with a browser UA.", user_agent)
        fallback_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=fallback_headers, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    content = await response.read()
                    status = response.status
                    logger.debug("Retry with browser User-Agent returned status %s", status)
        except Exception as e:
            logger.error("Diagnostic retry failed: %s", e)
            return []
    # Log HTTP status and size for debugging
    try:
        content_len = len(content or b"")
    except Exception:
        content_len = 0
    logger.debug("Fetched %s (status=%s length=%s)", url, status, content_len)
    if status != 200:
        logger.error("Non-200 response while fetching brevity terms: %s", status)
        # still attempt to parse body if present
    soup = BeautifulSoup(content, "html.parser")
    content_div = soup.find("div", class_="mw-parser-output")
    terms = []

    if not content_div:
        # Dump a short snippet of the page to logs to help debugging
        snippet = (content.decode('utf-8')[:1000] + "...") if content else ""
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
        # Yield control periodically in long loops
        await asyncio.sleep(0)

    flush_term()
    logger.info("Parsed %d brevity terms from HTML.", len(terms))
    return terms


async def update_brevity_terms():
    logger.info("Refreshing brevity terms from Wikipedia...")
    new_terms = await parse_brevity_terms()

    if not new_terms:
        logger.warning("No brevity terms parsed. Keeping existing terms.")
        return 0, 0, 0 

    logger.info("Parsed %d brevity terms. Comparing with existing terms.", len(new_terms))

    # Load existing terms
    existing_raw = await r.get(TERMS_KEY)
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
        await r.set(f"{TERMS_KEY}_backup", existing_raw)
        logger.info("Backed up existing terms to TERMS_KEY_backup.")

    # Update Redis with new terms
    await r.set(TERMS_KEY, json.dumps(new_terms))

    logger.info(
        "Successfully loaded %d brevity terms. Added: %d, Updated: %d, Unchanged: %d.",
        len(new_terms), len(added_terms), len(updated_terms), len(unchanged_terms)
    )

    return len(new_terms), len(added_terms), len(updated_terms)

async def get_all_terms():
    terms_data = await r.get(TERMS_KEY)
    if not terms_data:
        return []
    
    try:
        return json.loads(terms_data)
    except json.JSONDecodeError as e:
        logger.error("Failed to decode terms data from Redis: %s", e)
        return []


async def build_greenie_board_text(guild, user_greenies, name_col: int = 14, as_field: bool = False):
    """Return the Greenie Board text.
    If `as_field` is False (default) returns a decorated message with header + code block suitable
    for sending as a normal message. If `as_field` is True, returns only a code-block string that
    can be used as an embed field value (no external header).
    `user_greenies` should be a list of tuples (uid, entries, avg) as used elsewhere.
    """
    lines = []
    
    # Parallelize member fetching for all users
    async def fetch_member_name(uid):
        name = None
        member = None
        try:
            if guild:
                member = guild.get_member(int(uid))
                if not member:
                    try:
                        member = await guild.fetch_member(int(uid))
                    except Exception:
                        member = None
            if member:
                return member.display_name
        except Exception:
            pass

        try:
            user_obj = await client.fetch_user(int(uid))
            return getattr(user_obj, 'name', str(uid))
        except Exception:
            return str(uid)
    
    # Fetch all names in parallel
    user_ids = [user[0] for user in user_greenies[:10]]
    names = await asyncio.gather(*[fetch_member_name(uid) for uid in user_ids], return_exceptions=True)
    
    for idx, user in enumerate(user_greenies[:10]):
        uid, entries, avg = user
        name = names[idx] if not isinstance(names[idx], Exception) else str(uid)
        
        safe_name = name.replace("\n", " ")
        if len(safe_name) > 12:
            username = safe_name[:11] + "…"
        else:
            username = safe_name

        # Build emoji row (10 slots) with NO spaces between emojis
        row_emojis = []
        for e in entries:
            pct = e["correct"] / e["total"] if e["total"] else 0
            if pct >= 0.8:
                row_emojis.append("🟢")
            elif pct >= 0.5:
                row_emojis.append("🟡")
            else:
                row_emojis.append("🔴")
        while len(row_emojis) < 10:
            row_emojis.append("⬜")
        row = "".join(row_emojis)
        avg_pct = int(avg * 100)

        if len(username) > name_col:
            left = username[: name_col - 1] + "…"
        else:
            left = username.ljust(name_col)

        results = row
        pct = f"{avg_pct:>3}%"
        lines.append(f"{left} | {results} | {pct} avg")

    board = "\n".join(lines)
    code_block = f"```\n{board}\n```"
    if as_field:
        return code_block
    header = "**Greenie Board (Last 10 Quizzes):**"
    board_code = f"{header}\n{code_block}"
    return board_code

async def get_next_brevity_term(guild_id):
    all_terms = await get_all_terms()
    if not all_terms:
        logger.warning("No brevity terms available.")
        return None

    used_terms = await load_used_terms(guild_id)
    unused_terms = [t for t in all_terms if t["term"] not in used_terms]

    if not unused_terms:
        # All terms have been used — reset
        logger.info("All terms used for guild %s, resetting used terms.", guild_id)
        await r.delete(f"used_terms:{guild_id}")
        used_terms = []
        unused_terms = all_terms  # now full list again

    chosen = random.choice(unused_terms)
    await save_used_term(guild_id, chosen["term"])
    return chosen


async def get_brevity_term_by_name(name):
    for term in await get_all_terms():
        if term["term"].lower() == name.lower():
            return term
    logger.info("No match found for brevity term: '%s'", name)
    return None


# -------------------------------
# SLASH COMMANDS
# -------------------------------
@tree.command(name="setup", description="Set the current channel for daily brevity posts.")
async def setup(interaction: discord.Interaction):
    await save_config(interaction.guild.id, interaction.channel.id)
    await enable_posting(interaction.guild.id)
    await interaction.response.send_message(f"Setup complete for <#{interaction.channel.id}>.", ephemeral=True)

@tree.command(name="nextterm", description="Send a new brevity term immediately.")
async def nextterm(interaction: discord.Interaction):
    try:
        await interaction.response.defer(thinking=True)  # <-- always defer immediately
    except discord.NotFound:
        logger.warning("Interaction expired before defer could happen.")
        return

    term = await get_next_brevity_term(interaction.guild.id)
    if not term:
        await interaction.followup.send("No terms available.", ephemeral=True)
        return
    embed = discord.Embed(
        title=term['term'],
        description=term['definition'],
        color=discord.Color.blue(),
        url=f"https://en.wikipedia.org/wiki/Multi-service_tactical_brevity_code#{term['term'][0]}"
    )
    image_url = await get_random_flickr_jet(FLICKR_API_KEY)
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

    total, added, updated = await update_brevity_terms()
    await interaction.followup.send(
        f"Terms synced from Wiki. Added: {added}. Updated: {updated}. Total: {total}.", ephemeral=True
    )
    logger.info("Manual reload triggered by guild %s", interaction.guild.id)


async def autocomplete_terms(interaction: discord.Interaction, current: str):
    all_terms = await get_all_terms()
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
    entry = await get_brevity_term_by_name(term)
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
    await set_post_frequency(interaction.guild.id, hours)
    await interaction.response.send_message(f"Frequency set to {hours} hour(s).", ephemeral=True)

@tree.command(name="disableposting", description="Stop scheduled brevity term posts in this server.")
async def disableposting(interaction: discord.Interaction):
    await disable_posting(interaction.guild.id)
    await interaction.response.send_message("Scheduled posting disabled.", ephemeral=True)

@tree.command(name="enableposting", description="Resume scheduled brevity term posts in this server.")
async def enableposting(interaction: discord.Interaction):
    await enable_posting(interaction.guild.id)
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
        # Allow users to change their vote: always write the latest selection to Redis.
        try:
            await self.redis_conn.hset(f"quiz:{self.quiz_id}:answers:{self.question_idx}", user_id, answer_idx)
        except Exception:
            # Best-effort: ignore storage errors so we don't block the interaction
            logger.exception("Failed to record vote for quiz %s q=%s user=%s", self.quiz_id, self.question_idx, user_id)
        # Remember that this user has participated
        self.answered_users.add(user_id)
        option_labels = ["A", "B", "C", "D"]
        q_number = self.question_idx + 1 if self.question_idx is not None else "?"
        selected_label = option_labels[answer_idx] if 0 <= answer_idx < len(option_labels) else str(answer_idx)
        # Acknowledge the selection. Components must respond; send an ephemeral confirmation.
        try:
            await interaction.response.send_message(f"Q{q_number}: You selected {selected_label}", ephemeral=True)
        except Exception:
            # If the component interaction was already responded to for some reason, try a followup and otherwise ignore
            try:
                await interaction.followup.send(f"Q{q_number}: You selected {selected_label}", ephemeral=True)
            except Exception:
                logger.debug("Could not acknowledge vote response for user %s (quiz %s q=%s)", user_id, self.quiz_id, self.question_idx)

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
    all_terms = await get_all_terms()
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
            
            # Randomly choose question type: "term_to_definition" or "definition_to_term" 
            question_type = random.choice(["term_to_definition", "definition_to_term"])
            
            incorrect_terms = [t for t in all_terms if t["term"] != correct_term]
            incorrect_choices = random.sample(incorrect_terms, 3)
            options = []
            
            if question_type == "term_to_definition":
                # Traditional format: show term, pick definition
                for t in incorrect_choices:
                    d = pick_single_definition(t["definition"])
                    options.append({"term": t["term"], "definition": d, "is_correct": False})
                options.append({"term": correct_term, "definition": correct_def, "is_correct": True})
                random.shuffle(options)
                option_labels = ["A", "B", "C", "D"]
                
                # Use fields for each option for better readability
                embed = discord.Embed(
                    title=f"Question {q_idx+1}/{questions}",
                    description=f"What is the correct definition of: **{correct_term}**",
                    color=discord.Color.orange()
                )
            else:
                # Inverse format: show definition, pick term
                for t in incorrect_choices:
                    options.append({"term": t["term"], "definition": t["definition"], "is_correct": False})
                options.append({"term": correct_term, "definition": correct_def, "is_correct": True})
                random.shuffle(options)
                option_labels = ["A", "B", "C", "D"]
                
                # Show definition as question, terms as options
                embed = discord.Embed(
                    title=f"Question {q_idx+1}/{questions}",
                    description=f"Which brevity term matches this definition:\n\n**{correct_def}**",
                    color=discord.Color.orange()
                )
            
            # Render each option as an empty-name field with a blockquote value so
            # the label and content appear on the same visible line.
            for idx, opt in enumerate(options):
                if question_type == "term_to_definition":
                    # Traditional format: show definitions as options
                    raw_def = opt.get("definition", "")
                    # Only mask occurrences of the current quiz term (correct_term),
                    # not other brevity terms that may appear in option text.
                    display = sanitize_definition_for_quiz(raw_def, correct_term)
                    # ensure multi-line definitions stay inside the blockquote by
                    # prefixing each line with '> '
                    display_block = "\n".join([" " + line for line in display.splitlines()])
                    # store display text for later reference in summaries if needed
                    opt["display"] = display
                    embed.add_field(name="", value=f"**`{option_labels[idx]}`** {display_block}", inline=False)
                else:
                    # Inverse format: show terms as options
                    term = opt.get("term", "")
                    opt["display"] = term  # store for later reference
                    embed.add_field(name="", value=f"**`{option_labels[idx]}`** {term}", inline=False)
            
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
                        if question_type == "term_to_definition":
                            msg = f"✅ Correct! {option_labels[idx]}. {self.options[idx]['definition']}"
                        else:
                            msg = f"✅ Correct! {option_labels[idx]}. {self.options[idx]['term']}"
                        score["correct"] += 1
                    else:
                        correct_idx = next(i for i, o in enumerate(self.options) if o["is_correct"])
                        if question_type == "term_to_definition":
                            msg = f"❌ Incorrect. The correct answer was {option_labels[correct_idx]}: {self.options[correct_idx]['definition']}"
                        else:
                            msg = f"❌ Incorrect. The correct answer was {option_labels[correct_idx]}: {self.options[correct_idx]['term']}"
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
        await ask_question(0)
        return

    # PUBLIC MODE: post all questions at once, keep open for total duration
    import time
    quiz_id = f"{interaction.guild_id or 'guild'}-{interaction.user.id}-{int(time.time())}"
    embeds = []
    views = []
    correct_indices = []
    used_options = []  # store the options used for each question (so summary matches options)
    question_types = []  # store the question type for each question for summary display
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
        
        # Randomly choose question type: "term_to_definition" or "definition_to_term" 
        question_type = random.choice(["term_to_definition", "definition_to_term"])
        
        incorrect_terms = [t for t in all_terms if t["term"] != correct_term]
        incorrect_choices = random.sample(incorrect_terms, 3)
        options = []
        
        if question_type == "term_to_definition":
            # Traditional format: show term, pick definition
            for t in incorrect_choices:
                d = pick_single_definition(t["definition"])
                options.append({"term": t["term"], "definition": d, "is_correct": False})
            options.append({"term": correct_term, "definition": correct_def, "is_correct": True})
            random.shuffle(options)
            correct_idx = next(i for i, o in enumerate(options) if o["is_correct"])
            correct_indices.append(correct_idx)
            logger.info(f"Built question {q_idx+1}/{questions} term='{correct_term}' correct_idx={correct_idx} type=term_to_definition")
            
            # Build a cleaner embed with fields for each multiple-choice option
            embed = discord.Embed(
                title=f"Brevity Quiz!",
                description=f"What is the correct definition of:\n**{correct_term}**\n\n",
                color=discord.Color.orange()
            )
        else:
            # Inverse format: show definition, pick term
            for t in incorrect_choices:
                options.append({"term": t["term"], "definition": t["definition"], "is_correct": False})
            options.append({"term": correct_term, "definition": correct_def, "is_correct": True})
            random.shuffle(options)
            correct_idx = next(i for i, o in enumerate(options) if o["is_correct"])
            correct_indices.append(correct_idx)
            logger.info(f"Built question {q_idx+1}/{questions} term='{correct_term}' correct_idx={correct_idx} type=definition_to_term")
            
            # Build embed showing definition as question, terms as options
            embed = discord.Embed(
                title=f"Brevity Quiz!",
                description=f"Which brevity term matches this definition:\n\n**{correct_def}**\n\n",
                color=discord.Color.orange()
            )
        # Format each option appropriately based on question type
        letter_labels = ["A", "B", "C", "D"]
        for idx, opt in enumerate(options):
            label = letter_labels[idx] if idx < len(letter_labels) else str(idx)
            
            if question_type == "term_to_definition":
                # Traditional format: show definitions as options
                raw_def = opt.get("definition", "")
                # Only mask occurrences of the current quiz term (correct_term),
                # not other brevity terms that may appear in option text.
                display = sanitize_definition_for_quiz(raw_def, correct_term)
                # prefix each line with '> ' so the entire multi-line definition stays inside the blockquote
                display_block = "\n".join([" " + line for line in display.splitlines()])
                opt["display"] = display
                embed.add_field(name="", value=f"**`{label}`** {display_block}", inline=False)
            else:
                # Inverse format: show terms as options
                term = opt.get("term", "")
                opt["display"] = term
                embed.add_field(name="", value=f"**`{label}`** {term}", inline=False)
        embed.set_footer(text=f"Question {q_idx+1} of {questions} • Quiz initiated by {interaction.user.display_name}")
        view = PublicQuizView(q_idx, correct_idx, quiz_id, r, timeout=duration*60)
        embeds.append(embed)
        views.append(view)
        used_options.append(options)
        question_types.append(question_type)  # Store question type for summary display

    # Post all questions
    channel = interaction.channel
    # Defensive checks before sending to capture permission issues early
    if channel is None:
        logger.error("interaction.channel is None for guild=%s user=%s", interaction.guild_id, interaction.user.id)
        await interaction.response.send_message("Couldn't determine the channel to post the quiz in. Please run this command in a server text channel.", ephemeral=True)
        return

    # We'll attempt to announce and post followups; rely on try/except to surface permission errors

    # Announce the quiz first via the interaction response (so followups are allowed)
    minute_label = "minute" if duration == 1 else "minutes"
    question_label = "question" if questions == 1 else "questions"
    start_embed = discord.Embed(
        title="Brevity quiz started!",
        description=f"You have **{duration}** {minute_label} to answer **{questions}** {question_label}.",
        color=discord.Color.orange()
    )
    start_embed.set_footer(text=f"Quiz initiated by {interaction.user.display_name}")

    # Send the start embed as the interaction response so we can post followups
    try:
        await interaction.response.send_message(embed=start_embed, ephemeral=False)
    except Exception as e:
        logger.error("Failed to send start embed response: %s", e)
        # Fall back to attempting to send as a followup (if response used elsewhere)
        try:
            await interaction.followup.send(embed=start_embed, ephemeral=False)
        except Exception as e2:
            logger.error("Also failed to send start embed as followup: %s", e2)
            await interaction.response.send_message("Couldn't announce the quiz; check my permissions.", ephemeral=True)
            return

    for idx, (embed, view) in enumerate(zip(embeds, views)):
        # Add a small delay between messages to avoid webhook rate limits (except for first message)
        if idx > 0:
            await asyncio.sleep(0.5)
        
        try:
            msg = await interaction.followup.send(embed=embed, view=view)
        except Exception as e:
            # Log full context for diagnosis and return a helpful ephemeral message to the user
            try:
                # Resolve bot_member lazily in case it wasn't defined earlier
                try:
                    bot_member = interaction.guild.get_member(client.user.id) if interaction.guild else None
                except Exception:
                    bot_member = None
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
        
        # Store message reference for this view
        view.message = msg
        view.message_id = msg.id
        messages.append(msg)
        logger.info(f"Posted quiz message id={msg.id} quiz_id={quiz_id} q_index={view.question_idx}")

    # Start embed already sent above to allow followups; don't respond again here.

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
            answers = await r.hgetall(f"quiz:{quiz_id}:answers:{i}")
            logger.info(f"Fetched answers for quiz_id={quiz_id} q_index={i} count={len(answers)}")
            correct_users = [uid for uid, idx in answers.items() if str(idx) == str(view.correct_idx)]
            for uid, idx in answers.items():
                user_participation.add(uid)
                if str(idx) == str(view.correct_idx):
                    user_correct[uid] = user_correct.get(uid, 0) + 1
            correct_mentions = ", ".join(f"<@{uid}>" for uid in correct_users) or "None"
            term = quiz_terms[i]["term"]
            # Use the option text used in the multiple choice for the correct answer display
            opt = used_options[i][view.correct_idx]
            question_type = question_types[i]
            
            if question_type == "term_to_definition":
                # Traditional format: question was term, answer was definition
                correct_answer_text = opt["definition"]
                field_name = f"**Q{i+1}:** {term}"
            else:
                # Inverse format: question was definition, answer was term
                correct_answer_text = opt["term"]
                definition_short = quiz_terms[i]["definition"][:100] + "..." if len(quiz_terms[i]["definition"]) > 100 else quiz_terms[i]["definition"]
                field_name = f"**Q{i+1}:** {definition_short}"
                
            field_value = f"**Answer:**  **`{option_labels[view.correct_idx]}`** - {correct_answer_text}\n **Correct users:** {correct_mentions}\n"
            results_embed.add_field(name=field_name, value=field_value, inline=False)
            # Yield control periodically
            await asyncio.sleep(0)

        # Save each user's result to Redis (capped at 10)
        total = len(views)
        for uid in user_participation:
            correct = user_correct.get(uid, 0)
            entry = json.dumps({"correct": correct, "total": total, "ts": int(time.time())})
            key = f"greenie:{interaction.guild_id}:{uid}"
            await r.lpush(key, entry)
            await r.ltrim(key, 0, 9)  # Keep only last 10
            logger.info(f"Saved greenie entry for guild={interaction.guild_id} user={uid} -> {correct}/{total}")
            await asyncio.sleep(0)

        # Per-quiz leaderboard -> add as an embed field
        if user_correct:
            leaderboard = sorted(user_correct.items(), key=lambda x: -x[1])
            leaderboard_lines = []
            for uid, correct in leaderboard:
                percent = int(100 * correct / total)
                leaderboard_lines.append(f"<@{uid}> got {correct}/{total} ({percent}%)")
            # add a blank field to visually separate question list from leaderboard
            results_embed.add_field(name="\u200b", value="\u200b", inline=False)
            results_embed.add_field(name="Quiz Leaderboard", value="\n".join(leaderboard_lines), inline=False)
        else:
            results_embed.add_field(name="\u200b", value="\u200b", inline=False)
            results_embed.add_field(name="Quiz Leaderboard", value="No correct answers this round.", inline=False)

        # Greenie Board (last 10 quizzes, 10 most recent users) -> add as an embed field
        greenie_keys = await r.keys(f"greenie:{interaction.guild_id}:*")
        user_greenies = []
        for key in greenie_keys:
            uid = key.split(":")[-1]
            entries = [json.loads(e) for e in await r.lrange(key, 0, 9)]
            if entries:
                avg = sum(e["correct"] / e["total"] for e in entries) / len(entries)
                user_greenies.append((uid, entries, avg))
            await asyncio.sleep(0)
        user_greenies.sort(key=lambda x: (-x[2], -max(e["ts"] for e in x[1])))
        # Use the shared builder so the output matches /greenieboard exactly.
        # Insert the board INTO the results embed as a code-block field.
        try:
            board_field = await build_greenie_board_text(interaction.guild, user_greenies, as_field=True)
            # Embed fields must be <= 1024 characters
            if len(board_field) > 1024:
                board_field = board_field[:1021] + "..."
            # add a blank field to visually separate leaderboard from greenie board
            results_embed.add_field(name="\u200b", value="\u200b", inline=False)
            results_embed.add_field(name="Greenie Board (Last 10 quizzes)", value=board_field, inline=False)
        except Exception:
            logger.exception("Failed to build greenie board during quiz summary")
        logger.info(f"Sending summary embed for quiz_id={quiz_id} to channel={interaction.channel.id}")
        
        # Send the results embed with error handling for permission issues
        try:
            await interaction.channel.send(embed=results_embed)
            logger.info(f"Successfully posted quiz results for quiz_id={quiz_id}")
        except Exception as e:
            # Log the error with context
            logger.error("Failed to post quiz results to channel=%s guild=%s quiz_id=%s: %s", 
                        getattr(interaction.channel, 'id', None), 
                        getattr(interaction, 'guild_id', None),
                        quiz_id, e)
            
            # Detect if it's a Forbidden (missing access) error
            is_forbidden = False
            try:
                is_forbidden = isinstance(e, discord.Forbidden)
            except Exception:
                is_forbidden = e.__class__.__name__ == 'Forbidden'
            
            if is_forbidden:
                logger.error("Bot lacks permissions to post quiz results. Required permissions: View Channels, Send Messages, Embed Links")
            else:
                logger.exception("Unexpected error posting quiz results")
    # Schedule the summary task
    asyncio.create_task(close_and_summarize())
# Greenie Board command
@tree.command(name="greenieboard", description="Show the Greenie Board for this server (last 10 quizzes per user)")
async def greenieboard(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    greenie_keys = await r.keys(f"greenie:{guild_id}:*")
    user_greenies = []
    for key in greenie_keys:
        uid = key.split(":")[-1]
        entries = [json.loads(e) for e in await r.lrange(key, 0, 9)]
        if entries:
            avg = sum(e["correct"] / e["total"] for e in entries) / len(entries)
            user_greenies.append((uid, entries, avg))
        await asyncio.sleep(0)
    user_greenies.sort(key=lambda x: (-x[2], -max(e["ts"] for e in x[1])))
    try:
        board_text = await build_greenie_board_text(interaction.guild, user_greenies)
        await interaction.response.send_message(board_text, ephemeral=False)
    except Exception:
        logger.exception("Failed to build/send greenie board for command")
        await interaction.response.send_message("Could not build Greenie Board.", ephemeral=True)


@tree.command(name="checkperms", description="Show the bot's effective permissions in this channel")
async def checkperms(interaction: discord.Interaction):
    channel = interaction.channel
    if not channel:
        await interaction.response.send_message("This command must be used in a guild text channel.", ephemeral=True)
        return

    guild = interaction.guild
    bot_member = guild.get_member(client.user.id) if guild else None
    if not bot_member:
        await interaction.response.send_message("I don't appear to be a member of this guild (unexpected).", ephemeral=True)
        return

    perms = channel.permissions_for(bot_member)

    required = {
        "view_channel": "View Channels",
        "send_messages": "Send Messages",
        "embed_links": "Embed Links",
        "read_message_history": "Read Message History",
    }

    missing = [name for attr, name in required.items() if not getattr(perms, attr, False)]

    embed = discord.Embed(title="Bot Permissions — Channel Diagnostic", color=discord.Color.blue())
    embed.add_field(name="Channel", value=f"<#{channel.id}>", inline=False)
    perm_lines = []
    # Show the specific required permissions first
    for attr, name in required.items():
        ok = getattr(perms, attr, False)
        check = "✅" if ok else "❌"
        perm_lines.append(f"{check} {name} ({attr})")
    embed.add_field(name="Required permissions", value="\n".join(perm_lines), inline=False)

    # Attempt to iterate all permission flags from the Permissions object for a full dump
    all_perm_lines = []
    try:
        # discord.Permissions supports iteration yielding (name, value)
        for name, value in perms:
            check = "✅" if value else "❌"
            all_perm_lines.append(f"{check} {name}")
    except Exception:
        # Fallback: use a conservative known list of permission attribute names
        perm_attr_list = [
            "create_instant_invite", "kick_members", "ban_members", "administrator",
            "manage_channels", "manage_guild", "add_reactions", "view_audit_log",
            "priority_speaker", "stream", "view_channel", "send_messages", "send_tts_messages",
            "manage_messages", "embed_links", "attach_files", "read_message_history", "mention_everyone",
            "use_external_emojis", "manage_nicknames", "manage_roles", "manage_webhooks", "manage_emojis_and_stickers",
            "connect", "speak", "mute_members", "deafen_members", "move_members", "use_vad",
            "change_nickname", "moderate_members", "request_to_speak", "manage_threads", "create_public_threads",
            "create_private_threads", "send_messages_in_threads"
        ]
        for name in perm_attr_list:
            val = getattr(perms, name, None)
            check = "✅" if val else "❌"
            all_perm_lines.append(f"{check} {name}")

    # Add a field showing all permission flags
    # Truncate if too long for an embed field
    all_perm_text = "\n".join(all_perm_lines)
    if len(all_perm_text) > 1000:
        all_perm_text = all_perm_text[:997] + "..."
    embed.add_field(name="All permission flags (bot) ", value=all_perm_text, inline=False)

    # Channel-specific overwrites for the bot (explicit allow/deny)
    try:
        ow = channel.overwrites_for(bot_member)
        ow_lines = []
        for name, value in ow:
            if value is None:
                state = "(unset)"
            elif value:
                state = "Allow"
            else:
                state = "Deny"
            ow_lines.append(f"{name}: {state}")
        ow_text = "\n".join(ow_lines) if ow_lines else "No channel-specific overwrites for bot_member."
    except Exception:
        ow_text = "Could not inspect channel overwrites."
    # Ensure embed field length <= 1024
    if len(ow_text) > 1000:
        ow_text = ow_text[:997] + "..."
    embed.add_field(name="Channel overwrites for bot", value=ow_text, inline=False)

    # Role-by-role channel overwrites for the bot's roles
    role_overwrites_lines = []
    try:
        for role in bot_member.roles:
            if role.is_default():
                role_name = f"@everyone ({role.id})"
            else:
                role_name = f"{role.name} ({role.id})"
            ro = channel.overwrites_for(role)
            # Summarize overwrites
            entries = []
            for name, value in ro:
                if value is None:
                    continue
                entries.append(f"{name}={'Allow' if value else 'Deny'}")
            if entries:
                role_overwrites_lines.append(f"{role_name}: {', '.join(entries)}")
    except Exception:
        pass
    if role_overwrites_lines:
        role_ow_text = "\n".join(role_overwrites_lines)
        if len(role_ow_text) > 1000:
            role_ow_text = role_ow_text[:997] + "..."
        embed.add_field(name="Role-level overwrites in this channel", value=role_ow_text, inline=False)

    # Roles the bot has and their base permissions (for quick inspection)
    role_lines = []
    try:
        for role in bot_member.roles:
            role_lines.append(f"{role.name} ({role.id}) -> perms: {int(role.permissions.value)}")
    except Exception:
        role_lines.append("Could not enumerate roles/perms")
    if role_lines:
        role_text = "\n".join(role_lines)
        if len(role_text) > 1000:
            role_text = role_text[:997] + "..."
        embed.add_field(name="Bot roles & permission ints", value=role_text, inline=False)

    if missing:
        embed.color = discord.Color.red()
        embed.add_field(name="Missing", value=", ".join(missing), inline=False)
        embed.set_footer(text="Grant the bot View Channels + Send Messages (and Embed Links) or adjust channel overrides.")
    else:
        embed.color = discord.Color.green()
        embed.set_footer(text="All required permissions appear present for this channel.")

    await interaction.response.send_message(embed=embed, ephemeral=True)

# -------------------------------
# BACKGROUND TASKS
# -------------------------------
@tasks.loop(minutes=5)
async def post_brevity_term():
    all_configs = await load_config()
    for guild_id_str, config in all_configs.items():
        try:
            guild_id = int(guild_id_str)
            if not await is_posting_enabled(guild_id):
                continue

            freq_hours = await get_post_frequency(guild_id)
            last_posted = await get_last_posted(guild_id)
            next_post_time = last_posted + (freq_hours * 3600)

            # Check if it's time to post (allowing a ±5-minute window)
            current_time = time.time()
            if current_time < next_post_time - 300 or current_time > next_post_time + 300:
                continue

            channel = client.get_channel(config["channel_id"])
            if not channel:
                logger.warning("Channel %s not found for guild %s. Removing stale config.", config['channel_id'], guild_id)
                await r.hdel(CHANNEL_MAP_KEY, str(guild_id))
                continue

            term = await get_next_brevity_term(guild_id)
            if not term:
                continue

            embed = discord.Embed(
                title=term['term'],
                description=term['definition'],
                color=discord.Color.blue(),
                url=f"https://en.wikipedia.org/wiki/Multi-service_tactical_brevity_code#{term['term'][0]}"
            )
            image_url = await get_random_flickr_jet(FLICKR_API_KEY)
            if image_url:
                embed.set_image(url=image_url)
            embed.set_footer(text="From Wikipedia – Multi-service Tactical Brevity Code")

            await channel.send(embed=embed)
            # Record the actual post time so future scheduling is based on when
            # the term was sent, not when it was supposed to be sent.
            await set_last_posted(guild_id, time.time())
            logger.info("Posted term '%s' to guild %s (#%s)", term['term'], guild_id, config["channel_id"])

        except Exception as e:
            logger.error("Failed to post to guild %s: %s", guild_id_str, e)


@tasks.loop(hours=24)
async def refresh_terms_daily():
    await update_brevity_terms()

@tasks.loop(hours=1)
async def log_bot_stats():
    num_servers = len(client.guilds)
    total_words = int(await r.get("total_words") or 0)
    command_usage = json.loads(await r.get("command_usage") or "{}")

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
# Track if we've already synced commands to avoid rate limits
_commands_synced = False

@client.event
async def on_ready():
    global r, _commands_synced
    # Initialize async Redis connection
    r = await aioredis.from_url(
        redis_url,
        decode_responses=True
    )
    logger.info("Connected to Redis at %s:%s", parsed_url.hostname, parsed_url.port)
    
    logger.info("Logged in as %s (ID: %s)", client.user.name, client.user.id)
    
    # Only sync commands if needed, with rate limit protection
    # Check both in-memory flag and Redis timestamp to handle restarts
    last_sync_ts = await r.get("last_command_sync")
    current_time = time.time()
    
    # Only sync if: not synced this session AND (never synced OR >1 hour since last sync)
    should_sync = not _commands_synced and (
        last_sync_ts is None or 
        (current_time - float(last_sync_ts)) > 3600  # 1 hour cooldown
    )
    
    if should_sync:
        logger.info("Syncing slash commands...")
        try:
            await tree.sync()
            _commands_synced = True
            await r.set("last_command_sync", str(current_time))
            logger.info("Slash commands synced successfully")
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limited
                logger.warning("Command sync rate limited. Will retry on next restart (after cooldown).")
                _commands_synced = True  # Don't retry this session
            else:
                logger.error("Failed to sync commands: %s", e)
                _commands_synced = True  # Don't retry this session to avoid repeated errors
        except Exception as e:
            logger.error("Unexpected error syncing commands: %s", e)
            _commands_synced = True  # Don't retry this session
    else:
        if last_sync_ts:
            time_since = int(current_time - float(last_sync_ts))
            logger.info("Skipping command sync (last synced %d seconds ago)", time_since)
        else:
            logger.info("Skipping command sync (already synced this session)")
    
    if not post_brevity_term.is_running():
        post_brevity_term.start()
    if not refresh_terms_daily.is_running():
        refresh_terms_daily.start()
    if not log_bot_stats.is_running():
        log_bot_stats.start()

    # Start health check HTTP server for Railway
    await start_health_check_server()


if __name__ == "__main__":
    logger.info("Starting BrevityBot...")
    
    # Add a small startup delay to prevent rapid restart loops from triggering rate limits
    # This gives Railway/hosting platform time to stabilize before attempting Discord connection
    startup_delay = int(os.getenv("STARTUP_DELAY", "0"))
    if startup_delay > 0:
        logger.info("Waiting %d seconds before connecting (STARTUP_DELAY)...", startup_delay)
        time.sleep(startup_delay)
    
    client.run(DISCORD_BOT_TOKEN)


@client.event
async def on_guild_remove(guild):
    guild_id = guild.id
    logger.info("Removed from guild %s (%s). Cleaning up Redis data...", guild_id, guild.name)
    await r.hdel(CHANNEL_MAP_KEY, str(guild_id))
    await r.delete(f"used_terms:{guild_id}")
    await r.delete(f"{FREQ_KEY_PREFIX}{guild_id}")
    await r.delete(f"{LAST_POSTED_KEY_PREFIX}{guild_id}")
    await r.srem(DISABLED_GUILDS_KEY, str(guild_id))


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
        await r.set(key, json.dumps({
            "correct_idx": self.correct_idx,
            "votes": self.votes
        }))
        await r.expire(key, 6 * 3600)

        for uid, idx in self.votes.items():
            score_key = f"user_score:{uid}"
            prev_json = await r.get(score_key)
            prev = json.loads(prev_json or '{"correct": 0, "total": 0}')
            prev["correct"] += int(idx == self.correct_idx)
            prev["total"] += 1
            await r.set(score_key, json.dumps(prev))

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

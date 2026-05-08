import discord
from discord.ext import tasks
from discord import app_commands
import os
import random
import aiohttp
import aiohttp.web
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
WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/Multi-service_tactical_brevity_code"
RELOAD_COOLDOWN_KEY = "reloadterms_cooldown"
RELOAD_COOLDOWN_SECONDS = 600  # 10 minutes

# In-memory term cache. Populated lazily by get_all_terms (and primed in
# on_ready), invalidated by update_brevity_terms. The TTL is a multi-instance
# backstop — terms change at most once a day, so 5 min of staleness is fine.
_terms_cache = None
_terms_cache_at = 0.0
_TERMS_CACHE_TTL = 300

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
    """Add a term to the used-set. Returns 1 if newly added, 0 if it was already there.

    The return value lets concurrent callers detect when another worker
    claimed the same term first (TOCTOU between read-used / pick-random / save).
    """
    added = await r.sadd(f"used_terms:{guild_id}", term)
    if added:
        logger.info("Saved used term '%s' for guild %s", term, guild_id)
    return added

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
    
    # Initialize last_posted if not set, so the first post happens after the configured interval
    last_posted = await get_last_posted(guild_id)
    if last_posted < 1577836800:  # Uninitialized (before Jan 1, 2020)
        await set_last_posted(guild_id, time.time())
        logger.info("Initialized last_posted for guild %s", guild_id)
    
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


def pick_single_definition(defn: str) -> str:
    """Return a single definition string for use in a quiz prompt or option.

    Some Wikipedia entries pack multiple meanings into one definition (e.g.
    "1. Foo something. 2. Bar something else."). This helper splits those
    numbered variants and returns one at random. Falls back to picking a
    non-empty line, and finally to returning the input unchanged.
    """
    if not defn:
        return defn
    defn = defn.strip()
    parts = re.split(r"\d+\.\s*|\d+\)\s*", defn)
    parts = [p.strip() for p in parts if p.strip()]
    good_parts = []
    for p in parts:
        cleaned = re.sub(r"^[\s\-\(\)\.]*\d*[\)\.\-]*\s*", "", p).strip()
        if re.search(r"[A-Za-z]", cleaned) and len(cleaned) >= 5:
            good_parts.append(cleaned)
    if len(good_parts) > 1:
        return random.choice(good_parts)
    if len(good_parts) == 1:
        return good_parts[0]
    lines = [l.strip() for l in defn.splitlines() if l.strip()]
    good_lines = [l for l in lines if re.search(r"[A-Za-z]", l) and len(l) >= 5]
    if good_lines:
        return random.choice(good_lines)
    return defn


def build_quiz_question(current, all_terms, *, title, footer, with_timestamp=False):
    """Build a multiple-choice quiz question.

    `current` is the term being tested; `all_terms` is the full term list (the
    helper samples 3 distractors). The returned `options` is a list of dicts
    with keys term/definition/is_correct/display, in display order. `display`
    is what the user sees on each button (masked definition, or term).

    Caller supplies styling (title/footer/timestamp) so private and public
    quiz modes can share this builder while keeping their own headers.

    Returns:
        (embed, options, correct_idx, question_type)

    `question_type` is "term_to_definition" (show term, pick definition) or
    "definition_to_term" (show definition, pick term).
    """
    correct_term = current["term"]
    correct_def = pick_single_definition(current["definition"])
    question_type = random.choice(["term_to_definition", "definition_to_term"])

    incorrect_terms = [t for t in all_terms if t["term"] != correct_term]
    incorrect_choices = random.sample(incorrect_terms, 3)
    options = []

    if question_type == "term_to_definition":
        for t in incorrect_choices:
            options.append({
                "term": t["term"],
                "definition": pick_single_definition(t["definition"]),
                "is_correct": False,
            })
        options.append({"term": correct_term, "definition": correct_def, "is_correct": True})
        prompt = f"What is the correct definition of:\n**{correct_term}**"
    else:
        for t in incorrect_choices:
            options.append({
                "term": t["term"],
                "definition": t["definition"],
                "is_correct": False,
            })
        options.append({"term": correct_term, "definition": correct_def, "is_correct": True})
        prompt = f"Which brevity term matches this definition:\n\n**{correct_def}**"

    random.shuffle(options)
    correct_idx = next(i for i, o in enumerate(options) if o["is_correct"])

    embed = discord.Embed(title=title, description=prompt, color=discord.Color.orange())

    option_labels = ["A", "B", "C", "D"]
    for idx, opt in enumerate(options):
        label = option_labels[idx]
        if question_type == "term_to_definition":
            display = sanitize_definition_for_quiz(opt["definition"], correct_term)
            display_block = "\n".join(" " + line for line in display.splitlines())
            opt["display"] = display
            embed.add_field(name="", value=f"**`{label}`** {display_block}", inline=False)
        else:
            opt["display"] = opt["term"]
            embed.add_field(name="", value=f"**`{label}`** {opt['term']}", inline=False)

    embed.set_footer(text=footer)
    if with_timestamp:
        embed.timestamp = discord.utils.utcnow()

    return embed, options, correct_idx, question_type


async def parse_brevity_terms():

    url = WIKIPEDIA_URL
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
    return _parse_terms_from_content(content or b"")


def _parse_terms_from_content(content):
    """Extract `[{term, definition}]` from a Wikipedia HTML page body.

    Walks only <dl> blocks (definition lists) and stops at h2 headers like
    "See also" or "References". Within each <dl>, iterates the DIRECT
    <dt>/<dd> children — so a nested <dl> never gets double-counted, and
    a stray <ul>/<ol> outside any <dl> can never be glued onto the previous
    term's definition. Lists nested inside a <dd> are extracted before the
    main text so their items aren't included twice (once as inline text via
    get_text, once as explicit bullets).
    """
    terms = []
    soup = BeautifulSoup(content, "html.parser")
    content_div = soup.find("div", class_="mw-parser-output")
    if not content_div:
        snippet = (content.decode('utf-8', errors='replace')[:1000] + "...") if content else ""
        logger.error("Couldn't find Wikipedia content container. Page snippet:\n%s", snippet)
        return terms

    current_term = None
    current_definition_parts = []

    def flush_term():
        nonlocal current_term, current_definition_parts
        if current_term and current_definition_parts:
            definition = "\n".join(current_definition_parts).strip()
            cleaned_term = clean_term(current_term)
            terms.append({"term": cleaned_term, "definition": definition})
        current_term = None
        current_definition_parts = []

    # Identify top-level <dl>s (those not nested inside another <dl>) so a
    # nested <dl> inside a <dd> can't show up as separate top-level terms.
    top_level_dl_ids = {id(dl) for dl in content_div.find_all("dl") if not dl.find_parent("dl")}

    for tag in content_div.find_all(["h2", "dl"]):
        if tag.name == "h2":
            heading_text = tag.get_text(" ", strip=True).lower()
            if any(x in heading_text for x in ["see also", "references", "footnotes", "sources"]):
                logger.debug("Stopping parse at section: %s", heading_text)
                flush_term()
                logger.info("Parsed %d brevity terms from HTML.", len(terms))
                return terms
            continue

        if id(tag) not in top_level_dl_ids:
            continue

        # tag is a top-level <dl>: iterate its DIRECT <dt>/<dd> children only.
        for child in tag.find_all(["dt", "dd"], recursive=False):
            if child.name == "dt":
                flush_term()
                for sup in child.find_all("sup"):
                    sup.decompose()
                current_term = child.get_text(" ", strip=True)
                continue

            if not current_term:
                continue

            for sup in child.find_all("sup"):
                sup.decompose()
            for span in child.find_all("span"):
                span.decompose()

            # Pull nested ul/ol/dl out of the <dd> before extracting text, so
            # bullet items / inner term content aren't included as inline text.
            nested_blocks = child.find_all(["ul", "ol", "dl"], recursive=True)
            for blk in nested_blocks:
                blk.extract()

            text = child.get_text(" ", strip=True)
            text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)
            if text:
                current_definition_parts.append(text)

            for blk in nested_blocks:
                if blk.name in ("ul", "ol"):
                    bullets = [f"- {li.get_text(' ', strip=True)}" for li in blk.find_all("li", recursive=False)]
                    current_definition_parts.extend(bullets)
                # Nested <dl>s inside a <dd> are dropped — they're rare on the
                # brevity-codes page and treating them as sub-definitions would
                # be ambiguous.

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

    # Atomically backup-then-replace so a crash mid-write can't leave us with
    # the new TERMS_KEY but a stale (or missing) backup.
    new_serialized = json.dumps(new_terms)
    async with r.pipeline(transaction=True) as pipe:
        if existing_raw:
            pipe.set(f"{TERMS_KEY}_backup", existing_raw)
        pipe.set(TERMS_KEY, new_serialized)
        await pipe.execute()
    if existing_raw:
        logger.info("Backed up existing terms to TERMS_KEY_backup.")
    # Drop the local cache so the next get_all_terms reflects the new write.
    _invalidate_terms_cache()

    logger.info(
        "Successfully loaded %d brevity terms. Added: %d, Updated: %d, Unchanged: %d.",
        len(new_terms), len(added_terms), len(updated_terms), len(unchanged_terms)
    )

    return len(new_terms), len(added_terms), len(updated_terms)

async def get_all_terms():
    """Return the cached brevity-terms list, fetching from Redis on miss.

    The cache lives in this process and is invalidated locally when
    update_brevity_terms succeeds. The TTL is a backstop for multi-instance
    deploys where another replica may have refreshed Redis (terms change at
    most once a day, so 5-minute staleness is fine).
    """
    global _terms_cache, _terms_cache_at
    now = time.time()
    if _terms_cache is not None and (now - _terms_cache_at) < _TERMS_CACHE_TTL:
        return _terms_cache

    terms_data = await r.get(TERMS_KEY)
    if not terms_data:
        return []
    try:
        parsed = json.loads(terms_data)
    except json.JSONDecodeError as e:
        logger.error("Failed to decode terms data from Redis: %s", e)
        return []
    _terms_cache = parsed
    _terms_cache_at = now
    return parsed


def _invalidate_terms_cache():
    global _terms_cache, _terms_cache_at
    _terms_cache = None
    _terms_cache_at = 0.0


def _truncate_code_block(text: str, limit: int) -> str:
    """Truncate a ``` -fenced string to fit within `limit` chars while keeping
    the closing fence intact, so Discord still renders it as a code block.

    Returns the input unchanged if it already fits.
    """
    if len(text) <= limit:
        return text
    OPEN, CLOSE, MARKER = "```\n", "\n```", "..."
    if not (text.startswith(OPEN) and text.endswith(CLOSE)):
        # Not a fenced block we recognize — fall back to plain truncation.
        return text[: limit - len(MARKER)] + MARKER
    inner = text[len(OPEN): -len(CLOSE)]
    max_inner = limit - len(OPEN) - len(CLOSE) - len(MARKER)
    if max_inner < 0:
        return text[:limit]
    return OPEN + inner[:max_inner] + MARKER + CLOSE


async def build_greenie_board_text(guild, user_greenies, name_col: int = 14, as_field: bool = False):
    """Return the Greenie Board text.
    If `as_field` is False (default) returns a decorated message with header + code block suitable
    for sending as a normal message. If `as_field` is True, returns only a code-block string that
    can be used as an embed field value (no external header).
    `user_greenies` should be a list of tuples (uid, entries, avg) as used elsewhere.
    """
    lines = []
    
    # Parallelize member fetching for all users.
    # Strategy: cache hits first (guild.get_member, then client.get_user); only
    # fall back to client.fetch_user as a last resort. Avoids guild.fetch_member
    # entirely, which is an API call per uncached user and would rate-limit
    # quickly with 10 users in a busy multi-guild bot.
    async def fetch_member_name(uid):
        try:
            uid_int = int(uid)
        except (TypeError, ValueError):
            return str(uid)
        if guild:
            member = guild.get_member(uid_int)
            if member:
                return member.display_name
        cached_user = client.get_user(uid_int)
        if cached_user:
            return cached_user.name
        try:
            user_obj = await client.fetch_user(uid_int)
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

    # Retry loop guards against the read-pick-write race when /nextterm and the
    # scheduled post run concurrently (or in a multi-instance deploy). SADD's
    # return value is 1 when we won the race, 0 when another caller claimed it
    # first; on a loss we re-read the used set and pick again.
    max_attempts = 5
    chosen = None
    for _ in range(max_attempts):
        used_terms = await load_used_terms(guild_id)
        unused_terms = [t for t in all_terms if t["term"] not in used_terms]
        if not unused_terms:
            logger.info("All terms used for guild %s, resetting used terms.", guild_id)
            await r.delete(f"used_terms:{guild_id}")
            unused_terms = all_terms

        chosen = random.choice(unused_terms)
        if await save_used_term(guild_id, chosen["term"]):
            return chosen
        logger.debug("Lost race claiming term '%s' for guild %s; retrying.", chosen["term"], guild_id)

    logger.warning("Exhausted race retries for guild %s; returning last picked term anyway.", guild_id)
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
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def setup(interaction: discord.Interaction):
    await save_config(interaction.guild.id, interaction.channel.id)
    await enable_posting(interaction.guild.id)
    # /setup explicitly resets the schedule so the first post is one full
    # interval from now, even on re-setup of an already-configured guild
    # (where enable_posting would have left last_posted untouched).
    await set_last_posted(interaction.guild.id, time.time())
    await interaction.response.send_message(f"Setup complete for <#{interaction.channel.id}>.", ephemeral=True)

@tree.command(name="nextterm", description="Send a new brevity term immediately.")
@app_commands.guild_only()
async def nextterm(interaction: discord.Interaction):
    try:
        await interaction.response.defer(thinking=True)  # <-- always defer immediately
    except discord.NotFound:
        logger.warning("Interaction expired before defer could happen.")
        return

    term = await get_next_brevity_term(interaction.guild.id)
    if not term:
        # The defer above was public (thinking=True); a followup with
        # ephemeral=True would create a separate message and leave the
        # public "Thinking..." placeholder hanging. Reply publicly instead.
        await interaction.followup.send("No terms available.")
        return
    embed = discord.Embed(
        title=term['term'],
        description=term['definition'],
        color=discord.Color.blue(),
        url=WIKIPEDIA_URL,
    )
    image_url = await get_random_flickr_jet(FLICKR_API_KEY)
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(text="From Wikipedia – Multi-service Tactical Brevity Code")
    await interaction.followup.send(embed=embed)

@tree.command(name="reloadterms", description="Manually refresh brevity terms from Wikipedia.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def reloadterms(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)  # <-- defer immediately
    except discord.NotFound:
        logger.warning("Interaction expired before defer could happen.")
        return

    # Global cooldown — terms are a single shared key, so multiple admins (across
    # guilds) reloading in quick succession would just hammer Wikipedia for the
    # same content. The cooldown is enforced via TTL on a sentinel key.
    cooldown_remaining = await r.ttl(RELOAD_COOLDOWN_KEY)
    if cooldown_remaining > 0:
        await interaction.followup.send(
            f"Brevity terms were just reloaded. Try again in {cooldown_remaining}s "
            f"(global cooldown to avoid hammering Wikipedia).",
            ephemeral=True,
        )
        return

    total, added, updated = await update_brevity_terms()
    await r.set(RELOAD_COOLDOWN_KEY, "1", ex=RELOAD_COOLDOWN_SECONDS)
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
        url=WIKIPEDIA_URL,
    )
    await interaction.response.send_message(embed=embed)

@tree.command(name="setfrequency", description="Set the post frequency in hours for this server.")
@app_commands.describe(hours="Number of hours between posts")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def setfrequency(interaction: discord.Interaction, hours: int):
    if hours <= 0:
        await interaction.response.send_message("Frequency must be a positive integer.", ephemeral=True)
        return
    await set_post_frequency(interaction.guild.id, hours)
    await interaction.response.send_message(f"Frequency set to {hours} hour(s).", ephemeral=True)

@tree.command(name="disableposting", description="Stop scheduled brevity term posts in this server.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def disableposting(interaction: discord.Interaction):
    await disable_posting(interaction.guild.id)
    await interaction.response.send_message("Scheduled posting disabled.", ephemeral=True)

@tree.command(name="enableposting", description="Resume scheduled brevity term posts in this server.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def enableposting(interaction: discord.Interaction):
    await enable_posting(interaction.guild.id)
    await interaction.response.send_message("Scheduled posting enabled.", ephemeral=True)

class QuizButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"q:(?P<quiz_id>[^:]+):(?P<q_idx>\d+):(?P<answer_idx>\d+)",
):
    """A persistent button for public-quiz answers.

    The custom_id encodes (quiz_id, q_idx, answer_idx), so after a bot
    restart Discord can route a click straight to from_custom_id below
    without us holding the original View instance in memory. Per-quiz
    state lives in Redis under quiz:{quiz_id}:meta and answers go to
    quiz:{quiz_id}:answers:{q_idx}.

    Register the class once at startup with `client.add_dynamic_items(QuizButton)`.
    """

    LABELS = ("A", "B", "C", "D")

    def __init__(self, quiz_id: str, q_idx: int, answer_idx: int):
        super().__init__(
            discord.ui.Button(
                label=QuizButton.LABELS[answer_idx],
                style=discord.ButtonStyle.primary,
                custom_id=f"q:{quiz_id}:{q_idx}:{answer_idx}",
            )
        )
        self.quiz_id = quiz_id
        self.q_idx = q_idx
        self.answer_idx = answer_idx

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["quiz_id"], int(match["q_idx"]), int(match["answer_idx"]))

    async def callback(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        key = f"quiz:{self.quiz_id}:answers:{self.q_idx}"
        try:
            async with r.pipeline(transaction=False) as pipe:
                pipe.hset(key, user_id, self.answer_idx)
                # Refresh TTL so a vote near the deadline keeps the data alive
                # for the summary task. 1h is comfortably longer than any quiz.
                pipe.expire(key, 3600)
                await pipe.execute()
        except Exception:
            logger.exception("Failed to record vote for quiz %s q=%s user=%s", self.quiz_id, self.q_idx, user_id)
        label = QuizButton.LABELS[self.answer_idx]
        try:
            await interaction.response.send_message(
                f"Q{self.q_idx + 1}: You selected {label}", ephemeral=True
            )
        except Exception:
            try:
                await interaction.followup.send(
                    f"Q{self.q_idx + 1}: You selected {label}", ephemeral=True
                )
            except Exception:
                logger.debug("Could not acknowledge vote for user %s (quiz %s q=%s)", user_id, self.quiz_id, self.q_idx)


def _make_quiz_view(quiz_id: str, q_idx: int) -> discord.ui.View:
    """Build a fresh persistent View with 4 QuizButton instances.

    The View object itself isn't kept in memory after the message is sent —
    Discord routes button clicks via the QuizButton custom_id pattern.
    """
    view = discord.ui.View(timeout=None)
    for answer_idx in range(4):
        view.add_item(QuizButton(quiz_id, q_idx, answer_idx))
    return view


async def _cleanup_quiz_keys(quiz_id: str, num_questions: int):
    """Delete a quiz's meta + per-question answer hashes."""
    try:
        keys = [f"quiz:{quiz_id}:answers:{i}" for i in range(num_questions)]
        keys.append(f"quiz:{quiz_id}:meta")
        await r.delete(*keys)
        logger.info("Cleaned up keys for quiz_id=%s", quiz_id)
    except Exception:
        logger.exception("Failed to clean up quiz keys for quiz_id=%s", quiz_id)


async def close_and_summarize(quiz_id: str):
    """Sleep until a quiz's deadline, then post the summary embed and clean up.

    Reads all per-quiz state from quiz:{quiz_id}:meta so it can be resumed
    after a bot restart — on_ready scans for active quizzes and schedules
    this task for each. If meta has expired (e.g. very long downtime), the
    function exits silently.
    """
    meta_raw = await r.get(f"quiz:{quiz_id}:meta")
    if not meta_raw:
        logger.warning("close_and_summarize: no meta for quiz_id=%s; skipping", quiz_id)
        return
    try:
        meta = json.loads(meta_raw)
    except Exception:
        logger.exception("close_and_summarize: bad meta JSON for quiz_id=%s", quiz_id)
        return

    delay = meta["deadline"] - time.time()
    if delay > 0:
        logger.info("close_and_summarize sleeping %.1fs until quiz %s deadline", delay, quiz_id)
        await asyncio.sleep(delay)
    logger.info("close_and_summarize starting for quiz_id=%s", quiz_id)

    correct_indices = meta["correct_indices"]
    used_options = meta["used_options"]
    question_types = meta["question_types"]
    quiz_terms = meta["quiz_terms"]
    n = len(correct_indices)
    guild_id = meta["guild_id"]

    guild = client.get_guild(guild_id) if guild_id else None
    channel = client.get_channel(meta["channel_id"])
    if channel is None:
        logger.error("Channel %s not found for quiz_id=%s; abandoning summary", meta["channel_id"], quiz_id)
        await _cleanup_quiz_keys(quiz_id, n)
        return

    results_embed = discord.Embed(title="Brevity Quiz Results", color=discord.Color.green())
    user_correct = {}
    user_participation = set()
    option_labels = ["A", "B", "C", "D"]

    for i in range(n):
        answers = await r.hgetall(f"quiz:{quiz_id}:answers:{i}")
        logger.info("Fetched answers for quiz_id=%s q_index=%s count=%d", quiz_id, i, len(answers))
        correct_idx = correct_indices[i]
        for uid, idx in answers.items():
            user_participation.add(uid)
            if str(idx) == str(correct_idx):
                user_correct[uid] = user_correct.get(uid, 0) + 1
        correct_users = [uid for uid, idx in answers.items() if str(idx) == str(correct_idx)]
        correct_mentions = ", ".join(f"<@{uid}>" for uid in correct_users) or "None"
        term = quiz_terms[i]["term"]
        opt = used_options[i][correct_idx]
        question_type = question_types[i]
        if question_type == "term_to_definition":
            correct_answer_text = opt["definition"]
            field_name = f"**Q{i+1}:** {term}"
        else:
            correct_answer_text = opt["term"]
            d = quiz_terms[i]["definition"]
            definition_short = d[:100] + "..." if len(d) > 100 else d
            field_name = f"**Q{i+1}:** {definition_short}"
        field_value = f"**Answer:**  **`{option_labels[correct_idx]}`** - {correct_answer_text}\n **Correct users:** {correct_mentions}\n"
        results_embed.add_field(name=field_name, value=field_value, inline=False)
        await asyncio.sleep(0)

    # Save each user's result to Redis (capped at 10) — pipelined LPUSH+LTRIM.
    total = n
    for uid in user_participation:
        correct = user_correct.get(uid, 0)
        entry = json.dumps({"correct": correct, "total": total, "ts": int(time.time())})
        key = f"greenie:{guild_id}:{uid}"
        async with r.pipeline(transaction=True) as pipe:
            pipe.lpush(key, entry)
            pipe.ltrim(key, 0, 9)
            await pipe.execute()
        logger.info("Saved greenie entry for guild=%s user=%s -> %d/%d", guild_id, uid, correct, total)
        await asyncio.sleep(0)

    if user_correct:
        leaderboard = sorted(user_correct.items(), key=lambda x: -x[1])
        leaderboard_lines = [f"<@{uid}> got {correct}/{total} ({int(100 * correct / total)}%)"
                             for uid, correct in leaderboard]
        results_embed.add_field(name="​", value="​", inline=False)
        results_embed.add_field(name="Quiz Leaderboard", value="\n".join(leaderboard_lines), inline=False)
    else:
        results_embed.add_field(name="​", value="​", inline=False)
        results_embed.add_field(name="Quiz Leaderboard", value="No correct answers this round.", inline=False)

    # Greenie Board
    greenie_keys = [k async for k in r.scan_iter(match=f"greenie:{guild_id}:*", count=100)]
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
        board_field = await build_greenie_board_text(guild, user_greenies, as_field=True)
        board_field = _truncate_code_block(board_field, 1024)
        results_embed.add_field(name="​", value="​", inline=False)
        results_embed.add_field(name="Greenie Board (Last 10 quizzes)", value=board_field, inline=False)
    except Exception:
        logger.exception("Failed to build greenie board during quiz summary")

    logger.info("Sending summary embed for quiz_id=%s to channel=%s", quiz_id, channel.id)
    try:
        await channel.send(embed=results_embed)
        logger.info("Successfully posted quiz results for quiz_id=%s", quiz_id)
    except discord.Forbidden:
        logger.error("Bot lacks permissions to post quiz results in channel=%s", channel.id)
    except Exception:
        logger.exception("Failed to post quiz results for quiz_id=%s", quiz_id)

    await _cleanup_quiz_keys(quiz_id, n)

@tree.command(name="quiz", description="Start a multiple-choice quiz on brevity terms")
@app_commands.describe(questions="Number of questions to answer", mode="Quiz mode: public (poll in channel) or private (ephemeral message)", duration="Total quiz duration in minutes (public mode only)")
@app_commands.guild_only()
async def quiz(
    interaction: discord.Interaction,
    questions: int = 1,
    mode: str = "private",
    duration: int = 2
):
    logger.info(f"/quiz invoked by user={interaction.user.id} guild={interaction.guild_id} questions={questions} mode={mode} duration={duration}")

    # Defer immediately so we never miss Discord's 3-second response deadline.
    # Private mode replies ephemerally; public mode replies in-channel.
    is_private = mode != "public"
    try:
        await interaction.response.defer(ephemeral=is_private, thinking=True)
    except discord.NotFound:
        logger.warning("Interaction expired before defer could happen.")
        return

    all_terms = await get_all_terms()
    max_questions = len(all_terms)
    if max_questions < 4:
        await interaction.followup.send("Not enough terms to generate a quiz.", ephemeral=True)
        return
    if questions < 1 or questions > max_questions:
        await interaction.followup.send(f"Number of questions must be between 1 and {max_questions}.", ephemeral=True)
        return

    quiz_terms = random.sample(all_terms, questions)
    score = {"correct": 0, "total": questions}

    if mode == "private":
        async def ask_question(q_idx, parent_interaction=None):
            # parent_interaction carries forward each button-click's interaction so
            # subsequent questions use a fresh 15-minute token instead of the
            # original slash-command interaction (which would expire on long quizzes).
            parent = parent_interaction or interaction
            embed, options, _correct_idx, question_type = build_quiz_question(
                quiz_terms[q_idx], all_terms,
                title=f"Question {q_idx+1}/{questions}",
                footer=f"Question {q_idx+1} of {questions}",
                with_timestamp=True,
            )
            option_labels = ["A", "B", "C", "D"]

            class QuizView(discord.ui.View):
                def __init__(self, options):
                    super().__init__(timeout=300)
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
                        # Pass the button's interaction forward so the next question
                        # is sent through a fresh 15-minute token.
                        await ask_question(q_idx + 1, parent_interaction=interaction_btn)
                    else:
                        await interaction_btn.followup.send(f"Quiz complete! You got {score['correct']} out of {score['total']} correct.", ephemeral=True)

            await parent.followup.send(embed=embed, view=QuizView(options), ephemeral=True)
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
        embed, options, correct_idx, question_type = build_quiz_question(
            current, all_terms,
            title="Brevity Quiz!",
            footer=f"Question {q_idx+1} of {questions} • Quiz initiated by {interaction.user.display_name}",
        )
        correct_indices.append(correct_idx)
        logger.info(
            f"Built question {q_idx+1}/{questions} term='{current['term']}' "
            f"correct_idx={correct_idx} type={question_type}"
        )
        view = _make_quiz_view(quiz_id, q_idx)
        embeds.append(embed)
        views.append(view)
        used_options.append(options)
        question_types.append(question_type)

    # Post all questions
    channel = interaction.channel
    # Defensive checks before sending to capture permission issues early
    if channel is None:
        logger.error("interaction.channel is None for guild=%s user=%s", interaction.guild_id, interaction.user.id)
        await interaction.followup.send("Couldn't determine the channel to post the quiz in. Please run this command in a server text channel.", ephemeral=True)
        return

    minute_label = "minute" if duration == 1 else "minutes"
    question_label = "question" if questions == 1 else "questions"
    start_embed = discord.Embed(
        title="Brevity quiz started!",
        description=f"You have **{duration}** {minute_label} to answer **{questions}** {question_label}.",
        color=discord.Color.orange()
    )
    start_embed.set_footer(text=f"Quiz initiated by {interaction.user.display_name}")

    # Response slot was consumed by the defer at the top, so always use followup.
    try:
        await interaction.followup.send(embed=start_embed, ephemeral=False)
    except Exception as e:
        logger.error("Failed to send start embed: %s", e)
        try:
            await interaction.followup.send("Couldn't announce the quiz; check my permissions.", ephemeral=True)
        except Exception:
            logger.error("Also failed to send fallback ephemeral about start embed failure.")
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

            # Response was already deferred at the top, so always use followup.
            try:
                await interaction.followup.send(user_msg, ephemeral=True)
            except Exception:
                logger.error("Also failed to send ephemeral response to the user about missing access.")
            return
        
        messages.append(msg)
        logger.info("Posted quiz message id=%s quiz_id=%s q_index=%d", msg.id, quiz_id, idx)

    # Persist quiz state so close_and_summarize can resume after a bot restart.
    # The TTL outlives the deadline by an hour for safety; close_and_summarize
    # deletes meta + answer keys explicitly on success.
    deadline = time.time() + duration * 60
    meta = {
        "guild_id": interaction.guild_id,
        "channel_id": getattr(interaction.channel, "id", None),
        "initiator_id": interaction.user.id,
        "initiator_name": interaction.user.display_name,
        "deadline": deadline,
        "duration": duration,
        "quiz_terms": [{"term": t["term"], "definition": t["definition"]} for t in quiz_terms],
        "used_options": used_options,
        "correct_indices": correct_indices,
        "question_types": question_types,
        "message_ids": [m.id for m in messages],
    }
    try:
        await r.set(
            f"quiz:{quiz_id}:meta",
            json.dumps(meta),
            ex=duration * 60 + 3600,
        )
    except Exception:
        logger.exception("Failed to persist quiz meta for quiz_id=%s", quiz_id)
    asyncio.create_task(close_and_summarize(quiz_id))
# Greenie Board command
@tree.command(name="greenieboard", description="Show the Greenie Board for this server (last 10 quizzes per user)")
@app_commands.guild_only()
async def greenieboard(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    # SCAN instead of KEYS so we don't block Redis on large keyspaces.
    greenie_keys = [k async for k in r.scan_iter(match=f"greenie:{guild_id}:*", count=100)]
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
@app_commands.guild_only()
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
            
            # Handle uninitialized or invalid last_posted timestamp
            # If last_posted is 0 or before Jan 1, 2020, treat as uninitialized
            if last_posted < 1577836800:  # Jan 1, 2020 timestamp
                logger.info("Uninitialized last_posted for guild %s, posting immediately", guild_id)
                # Post immediately by setting next_post_time to now
                next_post_time = time.time()
            else:
                next_post_time = last_posted + (freq_hours * 3600)

            # Check if it's time to post (allowing a ±5-minute window)
            current_time = time.time()
            if current_time < next_post_time - 300:
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
                url=WIKIPEDIA_URL,
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
    logger.info("BrevityBot Statistics: Servers: %d", len(client.guilds))

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

    # Prime the terms cache so the first /define / autocomplete / quiz doesn't
    # eat the cold-cache penalty (~500-term JSON parse).
    primed = await get_all_terms()
    logger.info("Primed term cache with %d terms", len(primed))

    # Register the persistent QuizButton handler so public-quiz buttons
    # posted before this restart keep working, then resume any in-flight
    # close_and_summarize tasks for those quizzes.
    client.add_dynamic_items(QuizButton)
    logger.info("Registered QuizButton dynamic item")
    resumed = 0
    async for key in r.scan_iter(match="quiz:*:meta", count=100):
        # key is like "quiz:{quiz_id}:meta"; quiz_id may contain hyphens.
        parts = key.split(":", 2)
        if len(parts) >= 2:
            asyncio.create_task(close_and_summarize(parts[1]))
            resumed += 1
    if resumed:
        logger.info("Resumed %d in-flight quiz summary task(s)", resumed)

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
                logger.warning("Command sync rate limited. Cooling down for an hour across restarts.")
                _commands_synced = True  # Don't retry this session
                # Persist the timestamp so a fast crash-loop restart doesn't keep
                # hammering the rate limit before the cooldown expires.
                await r.set("last_command_sync", str(current_time))
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


@client.event
async def on_guild_remove(guild):
    guild_id = guild.id
    logger.info("Removed from guild %s (%s). Cleaning up Redis data...", guild_id, guild.name)
    await r.hdel(CHANNEL_MAP_KEY, str(guild_id))
    await r.delete(f"used_terms:{guild_id}")
    await r.delete(f"{FREQ_KEY_PREFIX}{guild_id}")
    await r.delete(f"{LAST_POSTED_KEY_PREFIX}{guild_id}")
    await r.srem(DISABLED_GUILDS_KEY, str(guild_id))


if __name__ == "__main__":
    logger.info("Starting BrevityBot...")

    # Add a small startup delay to prevent rapid restart loops from triggering rate limits
    # This gives Railway/hosting platform time to stabilize before attempting Discord connection
    startup_delay = int(os.getenv("STARTUP_DELAY", "0"))
    if startup_delay > 0:
        logger.info("Waiting %d seconds before connecting (STARTUP_DELAY)...", startup_delay)
        time.sleep(startup_delay)

    client.run(DISCORD_BOT_TOKEN)

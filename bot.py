import os
import re
import csv
import io
import json
import time
import asyncio
import threading
import traceback
from collections import OrderedDict
from collections.abc import Callable
import requests
import discord
from discord import app_commands
from discord.ext import tasks

TOKEN = os.getenv("DISCORD_TOKEN")
BASE_URL = "https://questlog.gg/throne-and-liberty/api/trpc"

API_TIMEOUT = 8          # seconds before questlog times out
STAT_FORMAT_TTL = 86400  # 24h in seconds
STAT_FORMAT_RETRY = 300  # seconds before retrying a failed stat-format refresh
DATA_DIR = "data"

EMBED_MAX_CHARS = 5900   # margin under Discord's 6000-character-per-embed limit
EMBED_MAX_FIELDS = 25
EMBEDS_PER_MESSAGE = 10
LOOT_STATE_CACHE_SIZE = 500
SEARCH_CACHE_TTL = 60    # seconds; item names barely change, so this only needs to absorb keystroke bursts
SEARCH_CACHE_SIZE = 512

LOOT_FIELD_NAME = "🎯 Loot Interest"
LOOT_CATEGORIES = [
    ("pvp", "Main PvP", "loot_pvp"),
    ("pve", "Main PvE", "loot_pve"),
    ("alt", "Alternate Build", "loot_alt"),
    ("greed", "Greed", "loot_greed"),
]

# Grade → rarity label + color
GRADE_CONFIG = {
    40: ("🟦", "Rare",     0x2196F3),
    41: ("🟪", "Epic",     0xAB47BC),
    42: ("💜", "Epic II",  0x7B1FA2),
    43: ("💎", "Epic III", 0x4A148C),
}

# Stat formats cache
_stat_formats: dict = {}
_stat_formats_loaded_at: float = 0.0
_stat_formats_attempted_at: float = 0.0
_stat_formats_refreshing: bool = False
_stat_formats_lock = threading.Lock()


def load_stat_formats() -> None:
    global _stat_formats, _stat_formats_loaded_at
    try:
        r = requests.get(
            f"{BASE_URL}/statFormat.getStatFormat",
            params={"input": json.dumps({"language": "en"}, separators=(",", ":"))},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=API_TIMEOUT
        )
        r.raise_for_status()
        _stat_formats = r.json()["result"]["data"]
        _stat_formats_loaded_at = time.time()
        print(f"Loaded {len(_stat_formats)} stat formats")
    except Exception as e:
        print(f"Warning: could not load stat formats: {e}")


def _refresh_stat_formats_in_background() -> None:
    global _stat_formats_refreshing
    try:
        load_stat_formats()
    finally:
        with _stat_formats_lock:
            _stat_formats_refreshing = False


def get_stat_formats() -> dict:
    """Never blocks: called from async handlers, where a slow questlog.gg would otherwise
    freeze the whole bot for up to API_TIMEOUT seconds. When the formats are older than 24h
    the stale ones keep being served while one background thread refreshes them; a failed
    refresh is retried after STAT_FORMAT_RETRY seconds instead of on every call."""
    global _stat_formats_refreshing, _stat_formats_attempted_at
    now = time.time()
    stale = now - _stat_formats_loaded_at > STAT_FORMAT_TTL
    if stale and now - _stat_formats_attempted_at > STAT_FORMAT_RETRY:
        with _stat_formats_lock:
            if not _stat_formats_refreshing:
                _stat_formats_refreshing = True
                _stat_formats_attempted_at = now
                threading.Thread(target=_refresh_stat_formats_in_background, daemon=True).start()
    return _stat_formats


def format_stat(key: str, value: float) -> str:
    fmt = get_stat_formats().get(key)
    if not fmt:
        return f"{key}: {value}"
    name = fmt.get("name", key)
    multiplier = fmt.get("multiplier", 1)
    value_format = fmt.get("valueFormat", "{0}")
    computed = round(value * multiplier, 2)
    computed_str = str(int(computed)) if computed == int(computed) else str(computed)
    return f"{name}: {value_format.replace('{0}', computed_str)}"


class QuestlogClient(discord.Client):
    async def setup_hook(self) -> None:
        """Runs once per process, before the gateway connects (unlike on_ready, which
        re-fires after every full reconnect). An exception here would abort startup and
        crash-loop the container, so the sync stays wrapped."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, load_stat_formats)
        self.add_view(LootView())
        weekly_wishlist_cleanup.start()
        try:
            synced = await tree.sync()
            print(f"Synced {len(synced)} command(s)")
        except Exception as e:
            print(f"SYNC ERROR: {type(e).__name__}: {e}")


intents = discord.Intents.default()
client = QuestlogClient(intents=intents)
tree = app_commands.CommandTree(client)

# ── Error reporting ───────────────────────────────────────────────────────────

GENERIC_ERROR_MESSAGE = "❌ Something went wrong. Please try again, and tell an admin if it keeps happening."
ADMIN_REQUIRED_MESSAGE = "❌ This command requires the Administrator permission."
FORBIDDEN_ERROR_MESSAGE = (
    "❌ The bot is missing permissions for this action. "
    "An admin should check its permissions on the channel involved."
)


async def reply_ephemeral(interaction: discord.Interaction, message: str) -> None:
    """Initial response or followup depending on is_done(); never raises."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException as e:
        print(f"Warning: could not deliver the error message: {e}")


async def report_interaction_error(interaction: discord.Interaction, source: str, error: Exception) -> None:
    """Log the traceback to the container logs and tell the user something went wrong.
    Never raises: a failing error handler would leave the user with no feedback at all."""
    print(f"ERROR in {source} (guild={interaction.guild_id}, user={interaction.user.id})")
    traceback.print_exception(type(error), error, error.__traceback__)
    message = FORBIDDEN_ERROR_MESSAGE if isinstance(error, discord.Forbidden) else GENERIC_ERROR_MESSAGE
    await reply_ephemeral(interaction, message)


class ErrorReportingView(discord.ui.View):
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await report_interaction_error(interaction, type(self).__name__, error)


class ErrorReportingModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_interaction_error(interaction, type(self).__name__, error)


@tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    command = f"/{interaction.command.name}" if interaction.command else "command tree"
    if isinstance(error, app_commands.MissingPermissions):
        print(
            f"[DENIED] {interaction.user.name} ({interaction.user.id}) → {command} "
            f"(guild={interaction.guild_id}, missing: {', '.join(error.missing_permissions)})"
        )
        await reply_ephemeral(interaction, ADMIN_REQUIRED_MESSAGE)
        return
    await report_interaction_error(interaction, command, getattr(error, "original", error))


# ── API helpers ───────────────────────────────────────────────────────────────

def api_get(endpoint: str, input_data: dict) -> dict | None:
    try:
        r = requests.get(
            f"{BASE_URL}/{endpoint}",
            params={"input": json.dumps(input_data, separators=(",", ":"))},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=API_TIMEOUT
        )
        r.raise_for_status()
        return r.json()["result"]["data"]
    except requests.exceptions.Timeout:
        print(f"API timeout [{endpoint}]")
        return "timeout"
    except Exception as e:
        print(f"API error [{endpoint}]: {e}")
        return None


class TTLCache:
    """Small cache with per-entry expiry and a bounded size (oldest-used entry evicted first).
    search_items runs in executor threads, so every access goes through the lock."""

    def __init__(self, ttl: float, max_size: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._max_size = max_size
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()

    def get(self, key: str) -> list[dict] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if self._clock() >= expires_at:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def set(self, key: str, value: list[dict]) -> None:
        with self._lock:
            self._entries[key] = (self._clock() + self._ttl, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_size:
                self._entries.popitem(last=False)


_search_cache = TTLCache(SEARCH_CACHE_TTL, SEARCH_CACHE_SIZE)


def search_items(query: str) -> list[dict]:
    """Autocomplete fires on every keystroke and several members often type the same names,
    so successful lookups are cached. Failures are never cached: a transient API error must
    not turn into a minute of empty suggestions. The key ignores case and extra spaces, which
    the questlog search was checked not to care about."""
    key = " ".join(query.split()).casefold()
    cached = _search_cache.get(key)
    if cached is not None:
        return [dict(item) for item in cached]

    data = api_get("database.getItems", {
        "language": "en", "page": 1,
        "searchTerm": query, "mainCategory": "", "subCategory": ""
    })
    if not data or data == "timeout":
        return []
    results = [
        {"id": item["id"], "name": item["name"]}
        for item in data.get("pageData", [])
        if not item.get("isDisabled")
    ][:25]
    _search_cache.set(key, results)
    return [dict(item) for item in results]


def fetch_item(item_id: str) -> dict | str | None:
    return api_get("database.getItem", {"id": item_id, "language": "en"})


AH_REGION = "eu-f"


def fetch_market(auction_house_id: int | None) -> dict | str | None:
    """Current price of an item in AH_REGION, as {"minPrice": int | None, "inStock": int}.
    questlog.gg indexes the auction house by the item's auctionHouseId (not its id), and
    answers 400 for a null one, so items that can't be traded never reach the API.
    Returns None / "timeout" like api_get when the lookup failed."""
    if auction_house_id is None:
        return {"minPrice": None, "inStock": 0}
    data = api_get("auctionHouse.getItemMarket", {
        "auctionHouseId": auction_house_id, "potentialAbilityId": None
    })
    if data is None or data == "timeout":
        return data
    current = (data.get("current") or {}).get(AH_REGION) or {}
    return {"minPrice": current.get("minPrice"), "inStock": current.get("inStock") or 0}


def fetch_market_for_item(item_id: str) -> dict | str | None:
    """Blocking helper for callers that only know the item id: resolve its auctionHouseId, then its price."""
    item = fetch_item(item_id)
    if not isinstance(item, dict):
        return item
    return fetch_market(item.get("auctionHouseId"))


def fetch_price_history(auction_house_id: int, days: int, now_ms: float | None = None) -> list[dict] | str | None:
    """Price points for the last `days` days in AH_REGION, oldest first. Only periods that had
    offers are present. Each raw point is [time_ms, min, max, avg, last, in_stock], where the
    prices are the floor price sampled during that period.

    The hourly ranges (7d, 30d, 90d) are all capped at 168 points, i.e. the last 7 days, so
    anything longer is read from the daily "all" series and cut to the requested window."""
    range_name = "7d" if days <= 7 else "all"
    data = api_get("auctionHouse.getItemHistory", {
        "auctionHouseId": auction_house_id, "potentialAbilityId": None, "range": range_name
    })
    if data is None or data == "timeout":
        return data
    raw = (data.get("series") or {}).get(AH_REGION) or []
    points = [
        {"time": p[0], "min": p[1], "max": p[2], "avg": p[3], "last": p[4], "stock": p[5]}
        for p in raw if p and p[1] is not None
    ]
    if range_name == "all":
        cutoff = (time.time() * 1000 if now_ms is None else now_ms) - days * 86_400_000
        points = [point for point in points if point["time"] >= cutoff]
    return sorted(points, key=lambda point: point["time"])


# ── Guild config (per-server role restrictions for /item-loot) ────────────────

def _guild_config_path(guild_id: int) -> str:
    return os.path.join(DATA_DIR, f"{guild_id}.json")


def load_guild_config(guild_id: int) -> dict:
    """Read-only I/O errors propagate on purpose: returning {} would make the next
    save overwrite the real config. A corrupt file is quarantined instead of lost."""
    path = _guild_config_path(guild_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        quarantine = f"{path}.corrupt-{int(time.time())}"
        os.replace(path, quarantine)
        print(f"ERROR: guild config {guild_id} is corrupt ({e}); moved to {quarantine}, starting from an empty config")
        return {}


def _write_json_atomic(path: str, data: dict) -> None:
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def save_guild_config(guild_id: int, **updates) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    config = load_guild_config(guild_id)
    config.update(updates)
    _write_json_atomic(_guild_config_path(guild_id), config)


def try_add_wishlist_item(guild_id: int, user_id: int, item_id: str, item_name: str) -> tuple[str, int]:
    """Race-safe add: re-reads the config right before writing and contains no await,
    so concurrent /wishlist calls can't overwrite each other.
    Returns (status, count) with status in {"added", "duplicate", "full", "unconfigured"}."""
    config = load_guild_config(guild_id)
    limit = config.get("wishlist_limit")
    if not limit:
        return "unconfigured", 0
    wishlists = config.get("wishlists", {})
    user_items = wishlists.get(str(user_id), [])
    if any(i["id"] == item_id for i in user_items):
        return "duplicate", len(user_items)
    if len(user_items) >= limit:
        return "full", len(user_items)
    user_items.append({"id": item_id, "name": item_name})
    wishlists[str(user_id)] = user_items
    save_guild_config(guild_id, wishlists=wishlists)
    return "added", len(user_items)


def remove_wishlist_entries(guild_id: int, user_ids: list[str]) -> None:
    """Delta-based removal on a fresh read, for callers that awaited since their last read."""
    wishlists = load_guild_config(guild_id).get("wishlists", {})
    for user_id in user_ids:
        wishlists.pop(user_id, None)
    save_guild_config(guild_id, wishlists=wishlists)


def has_role(member: discord.abc.User, role_id: int) -> bool:
    return isinstance(member, discord.Member) and any(r.id == role_id for r in member.roles)


async def resolve_member_name(guild: discord.Guild, user_id: int) -> str:
    """guild.get_member() only hits the local cache (incomplete without the privileged
    Members intent), so fall back to a REST fetch for members not in cache."""
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            member = None
    return member.display_name if member else f"Former member ({user_id})"


async def clean_guild_wishlists(guild: discord.Guild) -> list[tuple[str, str]]:
    """Remove wishlist entries belonging to members no longer in the guild.
    Only removes on a confirmed 404 (member truly gone) — any other API error
    leaves the entry untouched to avoid false positives from transient issues.
    Returns a list of (user_id, name) for the entries removed."""
    wishlists = load_guild_config(guild.id).get("wishlists", {})
    if not wishlists:
        return []

    removed = []
    for user_id_str in list(wishlists.keys()):
        if guild.get_member(int(user_id_str)) is not None:
            continue
        try:
            await guild.fetch_member(int(user_id_str))
        except discord.NotFound:
            name = user_id_str
            try:
                user = await client.fetch_user(int(user_id_str))
                name = user.name
            except discord.HTTPException:
                pass
            removed.append((user_id_str, name))
        except discord.HTTPException as e:
            print(f"Warning: could not verify member {user_id_str} in guild {guild.id}: {e}")
        await asyncio.sleep(0.5)

    if removed:
        remove_wishlist_entries(guild.id, [user_id for user_id, _ in removed])
    return removed


def build_wishlist_clean_embed(removed: list[tuple[str, str]]) -> discord.Embed:
    description = "\n".join(f"• {name} (`{uid}`)" for uid, name in removed)
    if len(description) > 4000:
        description = description[:3997] + "..."
    return discord.Embed(
        title=f"🧹 Cleanup — {len(removed)} member(s) removed",
        description=description,
        color=0x5865F2
    )


# ── Loot list (state stored directly in the embed field) ──────────────────────

def format_loot_field(state: dict[str, list[int]]) -> str:
    lines = []
    for key, label, _ in LOOT_CATEGORIES:
        ids = state.get(key, [])
        value = " ".join(f"<@{i}>" for i in ids) if ids else "—"
        lines.append(f"**{label}:** {value}")
    return "\n".join(lines)


def parse_loot_field(value: str) -> dict[str, list[int]]:
    state = {key: [] for key, _, _ in LOOT_CATEGORIES}
    for line in value.split("\n"):
        for key, label, _ in LOOT_CATEGORIES:
            if line.startswith(f"**{label}:**"):
                state[key] = [int(i) for i in re.findall(r"<@!?(\d+)>", line)]
    return state


def toggle_loot_signup(state: dict[str, list[int]], user_id: int, category_key: str) -> dict[str, list[int]]:
    """Exclusive choice with toggle-off on re-click. Returns a new dict, never mutates `state`."""
    already_in = user_id in state[category_key]
    new_state = {key: [i for i in ids if i != user_id] for key, ids in state.items()}
    if not already_in:
        new_state[category_key].append(user_id)
    return new_state


class LootEntry:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.state: dict[str, list[int]] | None = None


class LootStateStore:
    """Authoritative sign-up state per loot message, plus one lock per message.

    A click's payload carries the message as it was when the click happened, so two
    near-simultaneous clicks start from the same stale snapshot and the second edit erases
    the first. Serializing on this state fixes that without fetching the message (which
    would need channel permissions the interaction flow doesn't). After a restart the cache
    is empty and the first click falls back to the state parsed from the embed."""

    def __init__(self, max_size: int = LOOT_STATE_CACHE_SIZE) -> None:
        self._max_size = max_size
        self._entries: OrderedDict[int, LootEntry] = OrderedDict()

    def entry(self, message_id: int) -> LootEntry:
        entry = self._entries.get(message_id)
        if entry is None:
            entry = self._entries[message_id] = LootEntry()
        self._entries.move_to_end(message_id)
        while len(self._entries) > self._max_size:
            oldest_id, oldest = next(iter(self._entries.items()))
            if oldest.lock.locked():
                break
            del self._entries[oldest_id]
        return entry


LOOT_STATES = LootStateStore()


class LootView(ErrorReportingView):
    def __init__(self):
        super().__init__(timeout=None)

    async def _handle_click(self, interaction: discord.Interaction, category_key: str):
        guild_id = interaction.guild_id
        config = load_guild_config(guild_id) if guild_id else {}
        button_role_id = config.get("button_role_id")
        if not button_role_id or not has_role(interaction.user, button_role_id):
            await interaction.response.send_message(
                "❌ You don't have permission to click these buttons.", ephemeral=True
            )
            return

        embed = interaction.message.embeds[0]
        field_index = next((i for i, f in enumerate(embed.fields) if f.name == LOOT_FIELD_NAME), None)
        if field_index is None:
            await interaction.response.send_message("❌ Internal error: loot field not found.", ephemeral=True)
            return

        # Acknowledge right away: clicks queue on the per-message lock and must not hit Discord's 3s limit.
        await interaction.response.defer()
        entry = LOOT_STATES.entry(interaction.message.id)
        async with entry.lock:
            current = entry.state if entry.state is not None else parse_loot_field(embed.fields[field_index].value)
            new_state = toggle_loot_signup(current, interaction.user.id, category_key)
            embed.set_field_at(field_index, name=LOOT_FIELD_NAME, value=format_loot_field(new_state), inline=False)
            await interaction.edit_original_response(embed=embed, view=self)
            entry.state = new_state

    @discord.ui.button(label="Main PvP", style=discord.ButtonStyle.primary, custom_id="loot_pvp")
    async def pvp_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_click(interaction, "pvp")

    @discord.ui.button(label="Main PvE", style=discord.ButtonStyle.success, custom_id="loot_pve")
    async def pve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_click(interaction, "pve")

    @discord.ui.button(label="Alternate Build", style=discord.ButtonStyle.secondary, custom_id="loot_alt")
    async def alt_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_click(interaction, "alt")

    @discord.ui.button(label="Greed", style=discord.ButtonStyle.danger, custom_id="loot_greed")
    async def greed_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_click(interaction, "greed")

    @discord.ui.button(label="Distributed", style=discord.ButtonStyle.gray, emoji="📦", custom_id="loot_distributed")
    async def distributed_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild_id
        config = load_guild_config(guild_id) if guild_id else {}
        command_role_id = config.get("command_role_id")
        if not command_role_id or not has_role(interaction.user, command_role_id):
            await interaction.response.send_message(
                "❌ You don't have permission to mark loot as distributed.", ephemeral=True
            )
            return

        log_channel_id = config.get("loot_log_channel_id")
        if not log_channel_id:
            await interaction.response.send_message(
                "⚠️ No distribution log channel is configured on this server.\n"
                "💡 An admin needs to run `/item-setup log_channel:<channel>`.",
                ephemeral=True
            )
            return

        item_embed = interaction.message.embeds[0]
        item_name = item_embed.title or "Unknown item"
        item_url = item_embed.url
        item_id = item_url.rsplit("/", 1)[-1] if item_url else None
        item_thumbnail = item_embed.thumbnail.url if item_embed.thumbnail else None

        view = LootDistributeView(guild_id, interaction.message, item_name, item_url, item_id, item_thumbnail)
        await interaction.response.send_message(f"Who received **{item_name}**?", view=view, ephemeral=True)


def is_listed(ah: dict | str | None) -> bool:
    return isinstance(ah, dict) and ah.get("inStock", 0) > 0 and ah.get("minPrice") is not None


def format_current_price(ah: dict | str | None) -> str:
    if is_listed(ah):
        price_fmt = f"{ah['minPrice']:,}".replace(",", " ")
        return f"{price_fmt} ◈ (×{ah['inStock']} in stock)"
    if isinstance(ah, dict):
        return "Not listed"
    return "Unavailable"


class LootDistributeView(ErrorReportingView):
    def __init__(
        self,
        guild_id: int,
        source_message: discord.Message,
        item_name: str,
        item_url: str | None,
        item_id: str | None,
        item_thumbnail: str | None
    ):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.source_message = source_message
        self.item_name = item_name
        self.item_url = item_url
        self.item_id = item_id
        self.item_thumbnail = item_thumbnail

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Who received this item?", min_values=1, max_values=1)
    async def select_recipient(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        recipient = select.values[0]
        modal = LootNoteModal(
            self.guild_id, self.source_message, self.item_name, self.item_url, self.item_id,
            self.item_thumbnail, recipient
        )
        await interaction.response.send_modal(modal)


class LootNoteModal(ErrorReportingModal):
    note = discord.ui.TextInput(
        label="Note (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
        placeholder="e.g. rolled highest, traded for materials, ..."
    )

    def __init__(
        self,
        guild_id: int,
        source_message: discord.Message,
        item_name: str,
        item_url: str | None,
        item_id: str | None,
        item_thumbnail: str | None,
        recipient: discord.abc.User
    ):
        super().__init__(title=f"Distribute: {item_name}"[:45])
        self.guild_id = guild_id
        self.source_message = source_message
        self.item_name = item_name
        self.item_url = item_url
        self.item_id = item_id
        self.item_thumbnail = item_thumbnail
        self.recipient = recipient

    async def on_submit(self, interaction: discord.Interaction):
        log_channel_id = load_guild_config(self.guild_id).get("loot_log_channel_id")
        channel = interaction.guild.get_channel(log_channel_id) if log_channel_id else None
        if channel is None:
            await interaction.response.send_message(
                "⚠️ The configured distribution log channel could not be found (maybe it was deleted?). Nothing was changed.",
                ephemeral=True
            )
            return

        ah = None
        if self.item_id:
            loop = asyncio.get_event_loop()
            ah = await loop.run_in_executor(None, fetch_market_for_item, self.item_id)

        embed = discord.Embed(title="📦 Loot Distributed", url=self.item_url, color=0x5865F2)
        embed.add_field(name="Item", value=self.item_name, inline=False)
        embed.add_field(name="Distributed to", value=self.recipient.mention, inline=True)
        embed.add_field(name="Distributed by", value=interaction.user.mention, inline=True)
        embed.add_field(name="Current Price", value=format_current_price(ah), inline=True)
        if self.note.value:
            embed.add_field(name="Note", value=self.note.value, inline=False)
        embed.add_field(name="Date", value=f"<t:{int(time.time())}:F>", inline=False)
        if self.item_thumbnail:
            embed.set_thumbnail(url=self.item_thumbnail)
        await channel.send(embed=embed)

        delete_failed = False
        try:
            await self.source_message.delete()
        except discord.HTTPException as e:
            delete_failed = True
            print(f"Warning: could not delete loot message {self.source_message.id}: {e}")

        note_suffix = f" note={self.note.value!r}" if self.note.value else ""
        print(
            f"[LOOT DISTRIBUTE] {interaction.user.name} ({interaction.user.id}) → "
            f"{self.item_name} to {self.recipient.name} ({self.recipient.id}){note_suffix}"
        )

        confirmation = f"✅ Marked **{self.item_name}** as distributed to {self.recipient.mention}."
        if delete_failed:
            confirmation += (
                "\n⚠️ Couldn't delete the original loot post — the bot may be missing permissions "
                "in that channel. Please remove it manually."
            )
        await interaction.response.edit_message(content=confirmation, view=None)


# ── Wishlist ───────────────────────────────────────────────────────────────────

def build_wishlist_embed(user: discord.abc.User, items: list[dict]) -> discord.Embed:
    embed = discord.Embed(title=f"📜 Wishlist — {user.display_name}", color=0x5865F2)
    if not items:
        embed.description = "Your wishlist is empty. Use `/wishlist <item>` to add one."
    else:
        embed.description = "\n".join(
            f"• [{item['name']}](https://questlog.gg/throne-and-liberty/en/db/item/{item['id']})"
            for item in items
        )
    return embed


class WishlistRemoveView(ErrorReportingView):
    def __init__(self, guild_id: int, user_id: int, items: list[dict]):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.user_id = user_id
        select = discord.ui.Select(
            placeholder="Remove an item from the wishlist...",
            options=[discord.SelectOption(label=item["name"][:100], value=item["id"]) for item in items]
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        item_id = interaction.data["values"][0]
        config = load_guild_config(self.guild_id)
        wishlists = config.get("wishlists", {})
        user_key = str(self.user_id)
        user_items = wishlists.get(user_key, [])
        removed = next((i for i in user_items if i["id"] == item_id), None)
        user_items = [i for i in user_items if i["id"] != item_id]
        wishlists[user_key] = user_items
        save_guild_config(self.guild_id, wishlists=wishlists)

        if removed:
            print(f"[WISHLIST REMOVE] {interaction.user.name} ({interaction.user.id}) → {removed['name']} ({removed['id']})")

        embed = build_wishlist_embed(interaction.user, user_items)
        view = WishlistRemoveView(self.guild_id, self.user_id, user_items) if user_items else None
        await interaction.response.edit_message(embed=embed, view=view)


def build_wishlist_export_embeds(entries: list[tuple[str, list[dict]]]) -> list[discord.Embed]:
    """entries: list of (display_name, items). Chunks into embeds respecting Discord's
    25-fields and ~6000-total-characters-per-embed limits."""
    title = "📜 Wishlists — Export"
    embeds = []
    fields: list[tuple[str, str]] = []
    char_count = len(title)

    def flush():
        embed = discord.Embed(title=title, color=0x5865F2)
        for name, value in fields:
            embed.add_field(name=name, value=value, inline=False)
        embeds.append(embed)

    for display, items in entries:
        value = ", ".join(i["name"] for i in items) or "—"
        if len(value) > 1024:
            value = value[:1021] + "..."
        field_chars = len(display) + len(value)
        if fields and (len(fields) >= EMBED_MAX_FIELDS or char_count + field_chars > EMBED_MAX_CHARS):
            flush()
            fields = []
            char_count = len(title)
        fields.append((display, value))
        char_count += field_chars

    if fields:
        flush()
    return embeds


async def build_wishlist_csv(guild: discord.Guild, wishlists: dict) -> discord.File:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Member", "Member ID", "Item Name", "Item ID"])
    for user_id_str, items in wishlists.items():
        display = await resolve_member_name(guild, int(user_id_str))
        for item in items:
            writer.writerow([display, user_id_str, item["name"], item["id"]])
    return discord.File(io.BytesIO(buf.getvalue().encode("utf-8")), filename="wishlists.csv")


class WishlistExportView(ErrorReportingView):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    @discord.ui.button(label="Export as CSV", style=discord.ButtonStyle.secondary, emoji="📄")
    async def export_csv(self, interaction: discord.Interaction, button: discord.ui.Button):
        config = load_guild_config(self.guild_id)
        wishlists = config.get("wishlists", {})
        file = await build_wishlist_csv(interaction.guild, wishlists)
        await interaction.response.send_message(file=file, ephemeral=True)


# ── Build embed ───────────────────────────────────────────────────────────────

def build_embed(item: dict, ah: dict | None) -> discord.Embed:
    grade = item.get("grade", 41)
    rarity_emoji, rarity_label, color = GRADE_CONFIG.get(grade, ("🔹", f"Grade {grade}", 0x5865F2))
    item_type = item.get("subCategory", "").capitalize()
    item_id = item.get("id", "")
    url = f"https://questlog.gg/throne-and-liberty/en/db/item/{item_id}"

    # AH price
    if is_listed(ah):
        price_fmt = f"{ah['minPrice']:,}".replace(",", " ")
        ah_str = f"  ·  🏪 **{price_fmt} ◈** ×{ah['inStock']}"
    elif isinstance(ah, dict):
        ah_str = "  ·  🏪 *Not listed*"
    else:
        ah_str = "  ·  🏪 *Unavailable*"

    embed = discord.Embed(
        title=item.get("name", "Unknown"),
        url=url,
        description=f"{rarity_emoji} **{rarity_label}** {item_type}{ah_str}",
        color=color
    )

    # Icon
    icon_path = item.get("icon", "")
    if icon_path:
        icon_clean = icon_path.rsplit(".", 1)[0]
        embed.set_thumbnail(url=f"https://cdn.questlog.gg/throne-and-liberty{icon_clean}.webp")

    stats = item.get("itemStats") or {}
    levels = set(stats.get("main") or {}) | set(stats.get("extra") or {})
    lvl = max(levels, key=int) if levels else None

    # ── Base Stats ────────────────────────────────────────────────────────────
    main = (stats.get("main") or {}).get(lvl, {}) if lvl else {}
    stat_lines = []
    mainhand = main.get("mainhand")
    offhand = main.get("offhand")
    if mainhand:
        stat_lines.append(f"Damage: {mainhand['min']} ~ {mainhand['max']}")
    if offhand:
        stat_lines.append(f"Off-Hand: {offhand['min']} ~ {offhand['max']}")
    extra_main = main.get("extra") or {}
    if extra_main.get("attack_speed_main_hand"):
        spd = round(extra_main["attack_speed_main_hand"] * 0.001, 3)
        stat_lines.append(f"Attack Speed: {spd}s")
    if extra_main.get("attack_range_main_hand"):
        rng = round(extra_main["attack_range_main_hand"] * 0.01, 1)
        stat_lines.append(f"Range: {rng}m")
    if extra_main.get("armor"):
        stat_lines.append(f"Armor: {extra_main['armor']}")
    if stat_lines:
        embed.add_field(name=f"⚔️ Base Stats (Lv. {lvl})", value=" │ ".join(stat_lines), inline=False)

    # ── Unique Skill ──────────────────────────────────────────────────────────
    passive = item.get("passives")
    if passive and passive.get("name"):
        desc = re.sub(r"<[^>]+>", "", passive.get("text", ""))
        embed.add_field(name=f"✨ {passive['name']}", value=desc or "No description", inline=False)

    # ── Extra Stats ───────────────────────────────────────────────────────────
    extra = (stats.get("extra") or {}).get(lvl, {}) if lvl else {}
    extra_parts = [format_stat(k, v) for k, v in extra.items()]
    if extra_parts:
        embed.add_field(name=f"📊 Stats (Lv. {lvl})", value=" │ ".join(extra_parts), inline=False)

    # ── Traits ────────────────────────────────────────────────────────────────
    traits = stats.get("traits") or {}
    if traits:
        stat_fmts = get_stat_formats()
        trait_lines = []
        for key, values in traits.items():
            fmt = stat_fmts.get(key)
            name = fmt["name"] if fmt else key
            multiplier = fmt["multiplier"] if fmt else 1
            value_format = fmt["valueFormat"] if fmt else "{0}"
            formatted_values = []
            for v in values:
                computed = round(v * multiplier, 2)
                computed_str = str(int(computed)) if computed == int(computed) else str(computed)
                formatted_values.append(value_format.replace("{0}", computed_str))
            trait_lines.append(f"**{name}**: {' | '.join(formatted_values)}")
        embed.add_field(name="🎲 Possible Traits", value="\n".join(trait_lines), inline=False)

    # ── Description ───────────────────────────────────────────────────────────
    raw_desc = item.get("description", "")
    if raw_desc:
        clean = re.sub(r"<[^>]+>", "", raw_desc).strip()
        if clean:
            embed.add_field(name="📖 Description", value=clean, inline=False)

    return embed


# ── Slash command ─────────────────────────────────────────────────────────────

@tree.command(name="item", description="Search a Throne & Liberty item")
@app_commands.describe(item_name="Start typing the item name...")
async def item_command(interaction: discord.Interaction, item_name: str):
    user = f"{interaction.user.name} ({interaction.user.id})"
    await interaction.response.defer()

    loop = asyncio.get_event_loop()
    item = await loop.run_in_executor(None, fetch_item, item_name)

    # Timeout on the item (blocking)
    if item == "timeout":
        print(f"[TIMEOUT] {user} requested '{item_name}'")
        await interaction.followup.send("⏱️ questlog.gg is taking too long to respond. Please try again in a few seconds.")
        return

    if not item:
        print(f"[NOT FOUND] {user} requested '{item_name}'")
        await interaction.followup.send(
            f"❌ Item not found: `{item_name}`\n"
            "💡 Use autocomplete to select an item from the list."
        )
        return

    ah = await loop.run_in_executor(None, fetch_market, item.get("auctionHouseId"))
    print(f"[OK] {user} → {item.get('name')} ({item.get('id')})")
    embed = build_embed(item, ah)
    await interaction.followup.send(embed=embed)


# ── Slash command /price ──────────────────────────────────────────────────────

def compute_price_stats(points: list[dict], current_price: int | None) -> dict:
    """The figures /price has always shown, from hourly points (hours with offers only):
    Min/Max are the lowest and highest floor price seen, Avg Price the mean of the hourly
    averages, and the change compares the current floor price with the oldest hour's average."""
    oldest_price = points[0]["avg"]
    change_pct = None
    if current_price is not None and oldest_price:
        change_pct = round((current_price - oldest_price) / oldest_price * 100, 1)
    return {
        "min_price": min(p["min"] for p in points),
        "max_price": max(p["max"] for p in points),
        "avg_price": round(sum(p["avg"] for p in points) / len(points)),
        "avg_stock": round(sum(p["stock"] for p in points) / len(points)),
        "change_pct": change_pct,
    }


def format_change(change_pct: float | None) -> str:
    if change_pct is None:
        return "—"
    if change_pct > 0:
        return f"📈 +{change_pct}%"
    if change_pct < 0:
        return f"📉 {change_pct}%"
    return "➡️ 0%"


@tree.command(name="price", description="Auction House price history for an item (EU)")
@app_commands.describe(
    item_name="Start typing the item name...",
    days="History period (default: 7 days)"
)
@app_commands.choices(days=[
    app_commands.Choice(name="7 days",  value=7),
    app_commands.Choice(name="30 days", value=30),
])
async def price_command(interaction: discord.Interaction, item_name: str, days: int = 7):
    user = f"{interaction.user.name} ({interaction.user.id})"
    await interaction.response.defer()

    loop = asyncio.get_event_loop()
    item = await loop.run_in_executor(None, fetch_item, item_name)

    if item == "timeout":
        print(f"[TIMEOUT/price] {user} requested '{item_name}'")
        await interaction.followup.send("⏱️ questlog.gg is taking too long to respond. Please try again.")
        return

    if not item:
        print(f"[NOT FOUND/price] {user} requested '{item_name}'")
        await interaction.followup.send(
            f"❌ Item not found: `{item_name}`\n"
            "💡 Use autocomplete to select an item from the list."
        )
        return

    auction_house_id = item.get("auctionHouseId")
    if auction_house_id is None:
        await interaction.followup.send("❌ This item can't be sold on the Auction House.")
        return

    market, history = await asyncio.gather(
        loop.run_in_executor(None, fetch_market, auction_house_id),
        loop.run_in_executor(None, fetch_price_history, auction_house_id, days),
    )

    if market == "timeout" or history == "timeout":
        print(f"[TIMEOUT/price] {user} requested '{item_name}'")
        await interaction.followup.send("⏱️ questlog.gg is taking too long to respond. Please try again.")
        return

    if market is None or history is None:
        print(f"[API ERROR/price] {user} requested '{item_name}'")
        await interaction.followup.send("❌ Auction House data is unavailable right now. Please try again later.")
        return

    if not history:
        await interaction.followup.send("❌ No price history available for this item.")
        return

    current_price = market["minPrice"]
    current_stock = market["inStock"]
    stats = compute_price_stats(history, current_price)

    def fmt_price(p: int) -> str:
        return f"{p:,}".replace(",", " ")

    grade = item.get("grade", 41)
    _, _, color = GRADE_CONFIG.get(grade, ("", "", 0x5865F2))
    item_url = f"https://questlog.gg/throne-and-liberty/en/db/item/{item.get('id', item_name)}"

    embed = discord.Embed(
        title=f"{item.get('name', item_name)}",
        url=item_url,
        description=f"🏪 **Auction House — EU** · Last {days} days",
        color=color
    )

    icon_path = item.get("icon", "")
    if icon_path:
        icon_clean = icon_path.rsplit(".", 1)[0]
        embed.set_thumbnail(url=f"https://cdn.questlog.gg/throne-and-liberty{icon_clean}.webp")

    current_text = f"**{fmt_price(current_price)} ◈**" if current_price is not None else "**Not listed**"
    embed.add_field(name="💰 Current Price", value=current_text,                                  inline=True)
    embed.add_field(name="📦 In Stock",      value=f"**{current_stock}**",                       inline=True)
    embed.add_field(name="📊 Change",        value=f"**{format_change(stats['change_pct'])}**",  inline=True)
    embed.add_field(name="⬇️ Min",           value=f"{fmt_price(stats['min_price'])} ◈",         inline=True)
    embed.add_field(name="⬆️ Max",           value=f"{fmt_price(stats['max_price'])} ◈",         inline=True)
    embed.add_field(name="〰️ Avg Price",     value=f"{fmt_price(stats['avg_price'])} ◈",         inline=True)
    embed.add_field(name="📦 Avg Stock",     value=str(stats["avg_stock"]),                      inline=True)

    print(f"[PRICE] {user} → {item.get('name')} ({item_name}) {days}d")
    await interaction.followup.send(embed=embed)


# ── Slash command /item-loot ──────────────────────────────────────────────────

@tree.command(name="item-loot", description="Search a T&L item and track loot interest (Main PvP / Main PvE / Alternate Build)")
@app_commands.describe(item_name="Start typing the item name...")
async def item_loot_command(interaction: discord.Interaction, item_name: str):
    user = f"{interaction.user.name} ({interaction.user.id})"
    guild_id = interaction.guild_id

    if not guild_id:
        await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
        return

    config = load_guild_config(guild_id)
    command_role_id = config.get("command_role_id")
    button_role_id = config.get("button_role_id")
    if not command_role_id or not button_role_id:
        await interaction.response.send_message(
            "⚠️ This feature isn't configured on this server yet.\n"
            "💡 An admin needs to run `/item-setup`.",
            ephemeral=True
        )
        return
    if not has_role(interaction.user, command_role_id):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return

    await interaction.response.defer()

    loop = asyncio.get_event_loop()
    item = await loop.run_in_executor(None, fetch_item, item_name)

    if item == "timeout":
        print(f"[TIMEOUT/loot] {user} requested '{item_name}'")
        await interaction.followup.send("⏱️ questlog.gg is taking too long to respond. Please try again in a few seconds.")
        return

    if not item:
        print(f"[NOT FOUND/loot] {user} requested '{item_name}'")
        await interaction.followup.send(
            f"❌ Item not found: `{item_name}`\n"
            "💡 Use autocomplete to select an item from the list."
        )
        return

    ah = await loop.run_in_executor(None, fetch_market, item.get("auctionHouseId"))
    print(f"[LOOT] {user} → {item.get('name')} ({item.get('id')})")
    embed = build_embed(item, ah)
    empty_state = {key: [] for key, _, _ in LOOT_CATEGORIES}
    embed.add_field(name=LOOT_FIELD_NAME, value=format_loot_field(empty_state), inline=False)
    await interaction.followup.send(embed=embed, view=LootView())


# ── Slash command /item-setup ─────────────────────────────────────────────────

@tree.command(name="item-setup", description="Configure roles and/or the distribution log channel for /item-loot (admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    command_role="Role allowed to run /item-loot and mark items as distributed",
    button_role="Role allowed to click the loot sign-up buttons",
    log_channel="Channel where loot distribution reports are posted"
)
async def item_setup_command(
    interaction: discord.Interaction,
    command_role: discord.Role = None,
    button_role: discord.Role = None,
    log_channel: discord.TextChannel = None
):
    guild_id = interaction.guild_id
    if not guild_id:
        await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
        return
    if command_role is None and button_role is None and log_channel is None:
        await interaction.response.send_message("⚠️ Provide at least `command_role`, `button_role`, or `log_channel`.", ephemeral=True)
        return

    updates = {}
    parts = []
    if command_role is not None:
        updates["command_role_id"] = command_role.id
        parts.append(f"Command role (`/item-loot`, Distributed button) → {command_role.mention}")
    if button_role is not None:
        updates["button_role_id"] = button_role.id
        parts.append(f"Sign-up buttons role → {button_role.mention}")
    if log_channel is not None:
        updates["loot_log_channel_id"] = log_channel.id
        parts.append(f"Distribution log channel → {log_channel.mention}")

    save_guild_config(guild_id, **updates)
    print(f"[ITEM SETUP] {interaction.user.name} ({interaction.user.id}) → guild={guild_id} {updates}")
    await interaction.response.send_message("✅ Configured: " + " · ".join(parts), ephemeral=True)


# ── Slash command /wishlist ────────────────────────────────────────────────────

@tree.command(name="wishlist", description="Add an item to your loot wishlist, or view your current wishlist")
@app_commands.describe(item_name="Item to add (leave empty to view your current wishlist)")
async def wishlist_command(interaction: discord.Interaction, item_name: str = None):
    guild_id = interaction.guild_id
    if not guild_id:
        await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
        return

    config = load_guild_config(guild_id)
    limit = config.get("wishlist_limit")
    if not limit:
        await interaction.response.send_message(
            "⚠️ The wishlist isn't configured on this server yet.\n"
            "💡 An admin needs to run `/wishlist-setup`.",
            ephemeral=True
        )
        return

    wishlists = config.get("wishlists", {})
    user_key = str(interaction.user.id)
    user_items = wishlists.get(user_key, [])

    if item_name is None:
        view = WishlistRemoveView(guild_id, interaction.user.id, user_items) if user_items else None
        await interaction.response.send_message(embed=build_wishlist_embed(interaction.user, user_items), view=view, ephemeral=True)
        return

    if any(i["id"] == item_name for i in user_items):
        await interaction.response.send_message("⚠️ This item is already in your wishlist.", ephemeral=True)
        return
    if len(user_items) >= limit:
        await interaction.response.send_message(
            f"❌ Your wishlist is full ({len(user_items)}/{limit}). Remove an item via `/wishlist` before adding a new one.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_event_loop()
    item = await loop.run_in_executor(None, fetch_item, item_name)

    if item == "timeout":
        await interaction.followup.send("⏱️ questlog.gg is taking too long to respond. Please try again in a few seconds.", ephemeral=True)
        return
    if not item:
        await interaction.followup.send(
            f"❌ Item not found: `{item_name}`\n💡 Use autocomplete to select an item from the list.",
            ephemeral=True
        )
        return

    status, count = try_add_wishlist_item(guild_id, interaction.user.id, item.get("id"), item.get("name"))
    if status == "duplicate":
        await interaction.followup.send("⚠️ This item is already in your wishlist.", ephemeral=True)
        return
    if status == "full":
        await interaction.followup.send(
            f"❌ Your wishlist is full ({count}/{limit}). Remove an item via `/wishlist` before adding a new one.",
            ephemeral=True
        )
        return
    if status == "unconfigured":
        await interaction.followup.send(
            "⚠️ The wishlist isn't configured on this server yet.\n"
            "💡 An admin needs to run `/wishlist-setup`.",
            ephemeral=True
        )
        return

    print(f"[WISHLIST ADD] {interaction.user.name} ({interaction.user.id}) → {item.get('name')} ({item.get('id')}) [{count}/{limit}]")
    await interaction.followup.send(f"✅ **{item.get('name')}** added to your wishlist ({count}/{limit}).", ephemeral=True)


# ── Slash command /wishlist-setup ──────────────────────────────────────────────

@tree.command(name="wishlist-setup", description="Configure the wishlist size limit, staff role and/or cleanup log channel (admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    limit="Maximum number of items each member can have in their wishlist (1-25)",
    role_staff="Role allowed to use /wishlist-check and /wishlist-export",
    log_channel="Channel where weekly auto-cleanup reports are posted (only when members were removed)"
)
async def wishlist_setup_command(
    interaction: discord.Interaction,
    limit: app_commands.Range[int, 1, 25] = None,
    role_staff: discord.Role = None,
    log_channel: discord.TextChannel = None
):
    guild_id = interaction.guild_id
    if not guild_id:
        await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
        return
    if limit is None and role_staff is None and log_channel is None:
        await interaction.response.send_message("⚠️ Provide at least `limit`, `role_staff`, or `log_channel`.", ephemeral=True)
        return

    updates = {}
    parts = []
    if limit is not None:
        updates["wishlist_limit"] = limit
        parts.append(f"Limit → **{limit}** items per member")
    if role_staff is not None:
        updates["staff_role_id"] = role_staff.id
        parts.append(f"Staff role (`/wishlist-check`, `/wishlist-export`) → {role_staff.mention}")
    if log_channel is not None:
        updates["log_channel_id"] = log_channel.id
        parts.append(f"Auto-cleanup log channel → {log_channel.mention}")

    save_guild_config(guild_id, **updates)
    print(f"[WISHLIST SETUP] {interaction.user.name} ({interaction.user.id}) → guild={guild_id} {updates}")
    await interaction.response.send_message("✅ Configured: " + " · ".join(parts), ephemeral=True)


# ── Slash command /wishlist-check ──────────────────────────────────────────────

@tree.command(name="wishlist-check", description="Staff: list members who have this item in their wishlist")
@app_commands.describe(item_name="Item to check (autocomplete: items currently wishlisted on this server)")
async def wishlist_check_command(interaction: discord.Interaction, item_name: str):
    guild_id = interaction.guild_id
    if not guild_id:
        await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
        return

    config = load_guild_config(guild_id)
    staff_role_id = config.get("staff_role_id")
    if not staff_role_id:
        await interaction.response.send_message(
            "⚠️ The staff role isn't configured on this server yet.\n"
            "💡 An admin needs to run `/wishlist-setup role_staff:<role>`.",
            ephemeral=True
        )
        return
    if not has_role(interaction.user, staff_role_id):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return

    wishlists = config.get("wishlists", {})
    item_display_name = item_name
    interested = []
    for user_id_str, items in wishlists.items():
        match = next((i for i in items if i["id"] == item_name), None)
        if match:
            item_display_name = match["name"]
            member = interaction.guild.get_member(int(user_id_str))
            interested.append(member.mention if member else f"<@{user_id_str}>")

    if not interested:
        await interaction.response.send_message("📭 No one has this item in their wishlist.")
        return

    embed = discord.Embed(
        title=f"🔍 Interested in: {item_display_name}",
        description="\n".join(interested),
        color=0x5865F2
    )
    print(f"[WISHLIST CHECK] {interaction.user.name} ({interaction.user.id}) → {item_display_name} ({len(interested)} interested)")
    await interaction.response.send_message(embed=embed)


@wishlist_check_command.autocomplete("item_name")
async def wishlist_check_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    guild_id = interaction.guild_id
    if not guild_id:
        return []
    config = load_guild_config(guild_id)
    wishlists = config.get("wishlists", {})
    seen = {}
    for items in wishlists.values():
        for i in items:
            seen[i["id"]] = i["name"]
    current_lower = current.lower()
    matches = sorted(
        (name, iid) for iid, name in seen.items() if current_lower in name.lower()
    )
    return [app_commands.Choice(name=name[:100], value=iid) for name, iid in matches[:25]]


# ── Slash command /wishlist-export ─────────────────────────────────────────────

@tree.command(name="wishlist-export", description="Staff: view every member's wishlist on this server, with CSV export")
async def wishlist_export_command(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    if not guild_id:
        await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
        return

    config = load_guild_config(guild_id)
    staff_role_id = config.get("staff_role_id")
    if not staff_role_id:
        await interaction.response.send_message(
            "⚠️ The staff role isn't configured on this server yet.\n"
            "💡 An admin needs to run `/wishlist-setup role_staff:<role>`.",
            ephemeral=True
        )
        return
    if not has_role(interaction.user, staff_role_id):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return

    wishlists = config.get("wishlists", {})
    entries = []
    for user_id_str, items in wishlists.items():
        if not items:
            continue
        display = await resolve_member_name(interaction.guild, int(user_id_str))
        entries.append((display, items))
    entries.sort(key=lambda e: e[0].lower())

    if not entries:
        await interaction.response.send_message("📭 No wishlists recorded on this server.")
        return

    embeds = build_wishlist_export_embeds(entries)
    print(f"[WISHLIST EXPORT] {interaction.user.name} ({interaction.user.id}) → {len(entries)} member(s), {len(embeds)} embed(s)")

    await interaction.response.send_message(
        embeds=embeds[:EMBEDS_PER_MESSAGE], view=WishlistExportView(guild_id)
    )
    for start in range(EMBEDS_PER_MESSAGE, len(embeds), EMBEDS_PER_MESSAGE):
        await interaction.followup.send(embeds=embeds[start:start + EMBEDS_PER_MESSAGE])


# ── Slash command /wishlist-clean ──────────────────────────────────────────────

@tree.command(name="wishlist-clean", description="Staff: remove wishlists belonging to members who left the server")
async def wishlist_clean_command(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    if not guild_id:
        await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
        return

    config = load_guild_config(guild_id)
    staff_role_id = config.get("staff_role_id")
    if not staff_role_id:
        await interaction.response.send_message(
            "⚠️ The staff role isn't configured on this server yet.\n"
            "💡 An admin needs to run `/wishlist-setup role_staff:<role>`.",
            ephemeral=True
        )
        return
    if not has_role(interaction.user, staff_role_id):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    removed = await clean_guild_wishlists(interaction.guild)
    print(f"[WISHLIST CLEAN] {interaction.user.name} ({interaction.user.id}) → guild={guild_id} removed={len(removed)}: {removed}")

    if not removed:
        await interaction.followup.send("🧹 Cleanup complete: no departed members found.", ephemeral=True)
        return

    await interaction.followup.send(embed=build_wishlist_clean_embed(removed), ephemeral=True)


@price_command.autocomplete("item_name")
async def price_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if len(current) < 2:
        return []
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, search_items, current)
    return [
        app_commands.Choice(name=r["name"][:100], value=r["id"])
        for r in results
    ]

@item_command.autocomplete("item_name")
async def item_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if len(current) < 2:
        return []
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, search_items, current)
    return [
        app_commands.Choice(name=r["name"][:100], value=r["id"])
        for r in results
    ]

@item_loot_command.autocomplete("item_name")
async def item_loot_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if len(current) < 2:
        return []
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, search_items, current)
    return [
        app_commands.Choice(name=r["name"][:100], value=r["id"])
        for r in results
    ]

@wishlist_command.autocomplete("item_name")
async def wishlist_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if len(current) < 2:
        return []
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, search_items, current)
    return [
        app_commands.Choice(name=r["name"][:100], value=r["id"])
        for r in results
    ]


# ── Background tasks ─────────────────────────────────────────────────────────

async def run_weekly_cleanup_for_guild(guild: discord.Guild) -> None:
    removed = await clean_guild_wishlists(guild)
    if not removed:
        return
    print(f"[WISHLIST CLEAN/auto] guild={guild.id} removed={removed}")

    log_channel_id = load_guild_config(guild.id).get("log_channel_id")
    if not log_channel_id:
        return
    channel = guild.get_channel(log_channel_id)
    if channel is None:
        print(f"Warning: log channel {log_channel_id} not found in guild {guild.id}")
        return
    await channel.send(embed=build_wishlist_clean_embed(removed))


@tasks.loop(hours=24 * 7)
async def weekly_wishlist_cleanup():
    for guild in client.guilds:
        try:
            await run_weekly_cleanup_for_guild(guild)
        except Exception as e:
            print(f"Warning: auto wishlist cleanup failed for guild {guild.id}: {e}")


@weekly_wishlist_cleanup.before_loop
async def wait_for_ready_before_cleanup():
    # The task is started from setup_hook, before the gateway connects: client.guilds is empty until READY.
    await client.wait_until_ready()


# ── Events ────────────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


if __name__ == "__main__":
    client.run(TOKEN)

import os
import re
import csv
import io
import json
import time
import asyncio
import requests
import discord
from discord import app_commands
from discord.ext import tasks

TOKEN = os.getenv("DISCORD_TOKEN")
BASE_URL = "https://questlog.gg/throne-and-liberty/api/trpc"

API_TIMEOUT = 8          # secondes avant timeout questlog
STAT_FORMAT_TTL = 86400  # 24h en secondes
DATA_DIR = "data"

EMBED_MAX_CHARS = 5900   # marge sous la limite Discord de 6000 caractères/embed
EMBED_MAX_FIELDS = 25
EMBEDS_PER_MESSAGE = 10

LOOT_FIELD_NAME = "🎯 Loot Interest"
LOOT_CATEGORIES = [
    ("pvp", "Main PvP", "loot_pvp"),
    ("pve", "Main PvE", "loot_pve"),
    ("alt", "Alternate Build", "loot_alt"),
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


def get_stat_formats() -> dict:
    """Return stat formats, reloading if older than 24h."""
    if time.time() - _stat_formats_loaded_at > STAT_FORMAT_TTL:
        load_stat_formats()
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


intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

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


def search_items(query: str) -> list[dict]:
    data = api_get("database.getItems", {
        "language": "en", "page": 1,
        "searchTerm": query, "mainCategory": "", "subCategory": ""
    })
    if not data or data == "timeout":
        return []
    return [
        {"id": item["id"], "name": item["name"]}
        for item in data.get("pageData", [])
        if not item.get("isDisabled")
    ][:25]


def fetch_item(item_id: str) -> dict | str | None:
    return api_get("database.getItem", {"id": item_id, "language": "en"})


def fetch_ah_price(item_id: str) -> dict | str | None:
    return api_get("auctionHouse.getAuctionItem", {
        "language": "en", "regionId": "eu-f",
        "itemId": item_id, "timespan": 360
    })


# ── Guild config (per-server role restrictions for /item-loot) ────────────────

def _guild_config_path(guild_id: int) -> str:
    return os.path.join(DATA_DIR, f"{guild_id}.json")


def load_guild_config(guild_id: int) -> dict:
    path = _guild_config_path(guild_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: could not read guild config {guild_id}: {e}")
        return {}


def save_guild_config(guild_id: int, **updates) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    config = load_guild_config(guild_id)
    config.update(updates)
    with open(_guild_config_path(guild_id), "w") as f:
        json.dump(config, f, indent=2)


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
    return member.display_name if member else f"Ancien membre ({user_id})"


async def clean_guild_wishlists(guild: discord.Guild) -> int:
    """Remove wishlist entries belonging to members no longer in the guild.
    Only removes on a confirmed 404 (member truly gone) — any other API error
    leaves the entry untouched to avoid false positives from transient issues."""
    config = load_guild_config(guild.id)
    wishlists = config.get("wishlists", {})
    if not wishlists:
        return 0

    removed = 0
    for user_id_str in list(wishlists.keys()):
        if guild.get_member(int(user_id_str)) is not None:
            continue
        try:
            await guild.fetch_member(int(user_id_str))
        except discord.NotFound:
            del wishlists[user_id_str]
            removed += 1
        except discord.HTTPException as e:
            print(f"Warning: could not verify member {user_id_str} in guild {guild.id}: {e}")
        await asyncio.sleep(0.5)

    if removed:
        save_guild_config(guild.id, wishlists=wishlists)
    return removed


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


class LootView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _handle_click(self, interaction: discord.Interaction, category_key: str):
        guild_id = interaction.guild_id
        config = load_guild_config(guild_id) if guild_id else {}
        button_role_id = config.get("button_role_id")
        if not button_role_id or not has_role(interaction.user, button_role_id):
            await interaction.response.send_message(
                "❌ Tu n'as pas la permission de cliquer sur ces boutons.", ephemeral=True
            )
            return

        embed = interaction.message.embeds[0]
        field_index = next((i for i, f in enumerate(embed.fields) if f.name == LOOT_FIELD_NAME), None)
        if field_index is None:
            await interaction.response.send_message("❌ Erreur interne : champ loot introuvable.", ephemeral=True)
            return

        state = parse_loot_field(embed.fields[field_index].value)
        user_id = interaction.user.id
        already_in = user_id in state[category_key]
        for key in state:
            if user_id in state[key]:
                state[key].remove(user_id)
        if not already_in:
            state[category_key].append(user_id)

        embed.set_field_at(field_index, name=LOOT_FIELD_NAME, value=format_loot_field(state), inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Main PvP", style=discord.ButtonStyle.primary, custom_id="loot_pvp")
    async def pvp_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_click(interaction, "pvp")

    @discord.ui.button(label="Main PvE", style=discord.ButtonStyle.success, custom_id="loot_pve")
    async def pve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_click(interaction, "pve")

    @discord.ui.button(label="Alternate Build", style=discord.ButtonStyle.secondary, custom_id="loot_alt")
    async def alt_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_click(interaction, "alt")


# ── Wishlist ───────────────────────────────────────────────────────────────────

def build_wishlist_embed(user: discord.abc.User, items: list[dict]) -> discord.Embed:
    embed = discord.Embed(title=f"📜 Wishlist — {user.display_name}", color=0x5865F2)
    if not items:
        embed.description = "Ta wishlist est vide. Utilise `/wishlist <item>` pour en ajouter."
    else:
        embed.description = "\n".join(
            f"• [{item['name']}](https://questlog.gg/throne-and-liberty/en/db/item/{item['id']})"
            for item in items
        )
    return embed


class WishlistRemoveView(discord.ui.View):
    def __init__(self, guild_id: int, user_id: int, items: list[dict]):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.user_id = user_id
        select = discord.ui.Select(
            placeholder="Retirer un item de la wishlist...",
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


class WishlistExportView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    @discord.ui.button(label="Exporter en CSV", style=discord.ButtonStyle.secondary, emoji="📄")
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
    if ah and ah != "timeout" and ah.get("inStock", 0) > 0:
        price_fmt = f"{ah['minPrice']:,}".replace(",", " ")
        ah_str = f"  ·  🏪 **{price_fmt} ◈** ×{ah['inStock']}"
    elif ah == "timeout":
        ah_str = "  ·  🏪 *Unavailable*"
    elif ah is not None:
        ah_str = "  ·  🏪 *Not listed*"
    else:
        ah_str = ""

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
    item, ah = await asyncio.gather(
        loop.run_in_executor(None, fetch_item, item_name),
        loop.run_in_executor(None, fetch_ah_price, item_name),
    )

    # Timeout sur l'item (bloquant)
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

    print(f"[OK] {user} → {item.get('name')} ({item.get('id')})")
    embed = build_embed(item, ah)
    await interaction.followup.send(embed=embed)


# ── Slash command /price ──────────────────────────────────────────────────────

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
    ah = await loop.run_in_executor(None, fetch_ah_price, item_name)

    if ah == "timeout":
        print(f"[TIMEOUT/price] {user} requested '{item_name}'")
        await interaction.followup.send("⏱️ questlog.gg is taking too long to respond. Please try again.")
        return

    if not ah:
        print(f"[NOT FOUND/price] {user} requested '{item_name}'")
        await interaction.followup.send(
            f"❌ Item not found: `{item_name}`\n"
            "💡 Use autocomplete to select an item from the list."
        )
        return

    history = ah.get("history", [])
    buckets_needed = days * 24 // 2  # buckets de 2h
    window = history[:buckets_needed]

    if not window:
        await interaction.followup.send("❌ No price history available for this item.")
        return

    prices  = [e["minPrice"] for e in window if e.get("minPrice") is not None]
    stocks  = [e["inStock"]  for e in window if e.get("inStock")  is not None]

    current_price = ah.get("minPrice", 0)
    current_stock = ah.get("inStock", 0)
    oldest_price  = prices[-1] if prices else current_price
    avg_price     = round(sum(prices) / len(prices)) if prices else 0
    min_price     = min(prices) if prices else 0
    max_price     = max(prices) if prices else 0
    avg_stock     = round(sum(stocks) / len(stocks)) if stocks else 0

    # Évolution en %
    if oldest_price and oldest_price != current_price:
        change_pct = round((current_price - oldest_price) / oldest_price * 100, 1)
        if change_pct > 0:
            change_str = f"📈 +{change_pct}%"
        elif change_pct < 0:
            change_str = f"📉 {change_pct}%"
        else:
            change_str = "➡️ 0%"
    else:
        change_str = "➡️ 0%"

    def fmt_price(p: int) -> str:
        return f"{p:,}".replace(",", " ")

    grade = ah.get("grade", 41)
    _, _, color = GRADE_CONFIG.get(grade, ("", "", 0x5865F2))
    item_url = f"https://questlog.gg/throne-and-liberty/en/db/item/{item_name}"

    embed = discord.Embed(
        title=f"{ah.get('name', item_name)}",
        url=item_url,
        description=f"🏪 **Auction House — EU** · Last {days} days",
        color=color
    )

    icon_path = ah.get("icon", "")
    if icon_path:
        icon_clean = icon_path.rsplit(".", 1)[0]
        embed.set_thumbnail(url=f"https://cdn.questlog.gg/throne-and-liberty{icon_clean}.webp")

    embed.add_field(name="💰 Current Price", value=f"**{fmt_price(current_price)} ◈**", inline=True)
    embed.add_field(name="📦 In Stock",      value=f"**{current_stock}**",              inline=True)
    embed.add_field(name="📊 Change",        value=f"**{change_str}**",                 inline=True)
    embed.add_field(name="⬇️ Min",           value=f"{fmt_price(min_price)} ◈",         inline=True)
    embed.add_field(name="⬆️ Max",           value=f"{fmt_price(max_price)} ◈",         inline=True)
    embed.add_field(name="〰️ Avg Price",     value=f"{fmt_price(avg_price)} ◈",         inline=True)
    embed.add_field(name="📦 Avg Stock",     value=str(avg_stock),                      inline=True)

    print(f"[PRICE] {user} → {ah.get('name')} ({item_name}) {days}d")
    await interaction.followup.send(embed=embed)


# ── Slash command /item-loot ──────────────────────────────────────────────────

@tree.command(name="item-loot", description="Search a T&L item and track loot interest (Main PvP / Main PvE / Alternate Build)")
@app_commands.describe(item_name="Start typing the item name...")
async def item_loot_command(interaction: discord.Interaction, item_name: str):
    user = f"{interaction.user.name} ({interaction.user.id})"
    guild_id = interaction.guild_id

    if not guild_id:
        await interaction.response.send_message("❌ Cette commande n'est utilisable que sur un serveur.", ephemeral=True)
        return

    config = load_guild_config(guild_id)
    command_role_id = config.get("command_role_id")
    button_role_id = config.get("button_role_id")
    if not command_role_id or not button_role_id:
        await interaction.response.send_message(
            "⚠️ Cette fonctionnalité n'est pas encore configurée sur ce serveur.\n"
            "💡 Un administrateur doit exécuter `/item-setup`.",
            ephemeral=True
        )
        return
    if not has_role(interaction.user, command_role_id):
        await interaction.response.send_message("❌ Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True)
        return

    await interaction.response.defer()

    loop = asyncio.get_event_loop()
    item, ah = await asyncio.gather(
        loop.run_in_executor(None, fetch_item, item_name),
        loop.run_in_executor(None, fetch_ah_price, item_name),
    )

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

    print(f"[LOOT] {user} → {item.get('name')} ({item.get('id')})")
    embed = build_embed(item, ah)
    empty_state = {key: [] for key, _, _ in LOOT_CATEGORIES}
    embed.add_field(name=LOOT_FIELD_NAME, value=format_loot_field(empty_state), inline=False)
    await interaction.followup.send(embed=embed, view=LootView())


# ── Slash command /item-setup ─────────────────────────────────────────────────

@tree.command(name="item-setup", description="Configure the roles allowed to use /item-loot (admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    role_commande="Role allowed to run /item-loot",
    role_boutons="Role allowed to click the loot buttons"
)
async def item_setup_command(interaction: discord.Interaction, role_commande: discord.Role, role_boutons: discord.Role):
    guild_id = interaction.guild_id
    if not guild_id:
        await interaction.response.send_message("❌ Cette commande n'est utilisable que sur un serveur.", ephemeral=True)
        return

    save_guild_config(guild_id, command_role_id=role_commande.id, button_role_id=role_boutons.id)
    print(f"[SETUP] {interaction.user.name} ({interaction.user.id}) → guild={guild_id} command_role={role_commande.id} button_role={role_boutons.id}")
    await interaction.response.send_message(
        f"✅ Configuré : `/item-loot` → {role_commande.mention} · Boutons → {role_boutons.mention}",
        ephemeral=True
    )


# ── Slash command /wishlist ────────────────────────────────────────────────────

@tree.command(name="wishlist", description="Add an item to your loot wishlist, or view your current wishlist")
@app_commands.describe(item_name="Item to add (leave empty to view your current wishlist)")
async def wishlist_command(interaction: discord.Interaction, item_name: str = None):
    guild_id = interaction.guild_id
    if not guild_id:
        await interaction.response.send_message("❌ Cette commande n'est utilisable que sur un serveur.", ephemeral=True)
        return

    config = load_guild_config(guild_id)
    limit = config.get("wishlist_limit")
    if not limit:
        await interaction.response.send_message(
            "⚠️ La wishlist n'est pas encore configurée sur ce serveur.\n"
            "💡 Un administrateur doit exécuter `/wishlist-setup`.",
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
        await interaction.response.send_message("⚠️ Cet item est déjà dans ta wishlist.", ephemeral=True)
        return
    if len(user_items) >= limit:
        await interaction.response.send_message(
            f"❌ Ta wishlist est pleine ({len(user_items)}/{limit}). Retire un item via `/wishlist` avant d'en ajouter un nouveau.",
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

    user_items.append({"id": item.get("id"), "name": item.get("name")})
    wishlists[user_key] = user_items
    save_guild_config(guild_id, wishlists=wishlists)
    print(f"[WISHLIST ADD] {interaction.user.name} ({interaction.user.id}) → {item.get('name')} ({item.get('id')}) [{len(user_items)}/{limit}]")
    await interaction.followup.send(f"✅ **{item.get('name')}** ajouté à ta wishlist ({len(user_items)}/{limit}).", ephemeral=True)


# ── Slash command /wishlist-setup ──────────────────────────────────────────────

@tree.command(name="wishlist-setup", description="Configure the wishlist size limit and/or the staff role (admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    limit="Maximum number of items each member can have in their wishlist (1-25)",
    role_staff="Role allowed to use /wishlist-check and /wishlist-export"
)
async def wishlist_setup_command(
    interaction: discord.Interaction,
    limit: app_commands.Range[int, 1, 25] = None,
    role_staff: discord.Role = None
):
    guild_id = interaction.guild_id
    if not guild_id:
        await interaction.response.send_message("❌ Cette commande n'est utilisable que sur un serveur.", ephemeral=True)
        return
    if limit is None and role_staff is None:
        await interaction.response.send_message("⚠️ Renseigne au moins `limit` ou `role_staff`.", ephemeral=True)
        return

    updates = {}
    parts = []
    if limit is not None:
        updates["wishlist_limit"] = limit
        parts.append(f"Limite → **{limit}** items par membre")
    if role_staff is not None:
        updates["staff_role_id"] = role_staff.id
        parts.append(f"Rôle staff (`/wishlist-check`, `/wishlist-export`) → {role_staff.mention}")

    save_guild_config(guild_id, **updates)
    print(f"[WISHLIST SETUP] {interaction.user.name} ({interaction.user.id}) → guild={guild_id} {updates}")
    await interaction.response.send_message("✅ Configuré : " + " · ".join(parts), ephemeral=True)


# ── Slash command /wishlist-check ──────────────────────────────────────────────

@tree.command(name="wishlist-check", description="Staff: list members who have this item in their wishlist")
@app_commands.describe(item_name="Item to check (autocomplete: items currently wishlisted on this server)")
async def wishlist_check_command(interaction: discord.Interaction, item_name: str):
    guild_id = interaction.guild_id
    if not guild_id:
        await interaction.response.send_message("❌ Cette commande n'est utilisable que sur un serveur.", ephemeral=True)
        return

    config = load_guild_config(guild_id)
    staff_role_id = config.get("staff_role_id")
    if not staff_role_id:
        await interaction.response.send_message(
            "⚠️ Le rôle staff n'est pas encore configuré sur ce serveur.\n"
            "💡 Un administrateur doit exécuter `/wishlist-setup role_staff:<rôle>`.",
            ephemeral=True
        )
        return
    if not has_role(interaction.user, staff_role_id):
        await interaction.response.send_message("❌ Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True)
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
        await interaction.response.send_message(f"📭 Personne n'a cet item dans sa wishlist.")
        return

    embed = discord.Embed(
        title=f"🔍 Intéressés par : {item_display_name}",
        description="\n".join(interested),
        color=0x5865F2
    )
    print(f"[WISHLIST CHECK] {interaction.user.name} ({interaction.user.id}) → {item_display_name} ({len(interested)} intéressé(s))")
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
        await interaction.response.send_message("❌ Cette commande n'est utilisable que sur un serveur.", ephemeral=True)
        return

    config = load_guild_config(guild_id)
    staff_role_id = config.get("staff_role_id")
    if not staff_role_id:
        await interaction.response.send_message(
            "⚠️ Le rôle staff n'est pas encore configuré sur ce serveur.\n"
            "💡 Un administrateur doit exécuter `/wishlist-setup role_staff:<rôle>`.",
            ephemeral=True
        )
        return
    if not has_role(interaction.user, staff_role_id):
        await interaction.response.send_message("❌ Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True)
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
        await interaction.response.send_message("📭 Aucune wishlist enregistrée sur ce serveur.")
        return

    embeds = build_wishlist_export_embeds(entries)
    print(f"[WISHLIST EXPORT] {interaction.user.name} ({interaction.user.id}) → {len(entries)} membre(s), {len(embeds)} embed(s)")

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
        await interaction.response.send_message("❌ Cette commande n'est utilisable que sur un serveur.", ephemeral=True)
        return

    config = load_guild_config(guild_id)
    staff_role_id = config.get("staff_role_id")
    if not staff_role_id:
        await interaction.response.send_message(
            "⚠️ Le rôle staff n'est pas encore configuré sur ce serveur.\n"
            "💡 Un administrateur doit exécuter `/wishlist-setup role_staff:<rôle>`.",
            ephemeral=True
        )
        return
    if not has_role(interaction.user, staff_role_id):
        await interaction.response.send_message("❌ Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    removed = await clean_guild_wishlists(interaction.guild)
    print(f"[WISHLIST CLEAN] {interaction.user.name} ({interaction.user.id}) → guild={guild_id} removed={removed}")
    await interaction.followup.send("🧹 Nettoyage effectué. Détails dans les logs du bot.", ephemeral=True)


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

@tasks.loop(hours=24 * 7)
async def weekly_wishlist_cleanup():
    for guild in client.guilds:
        try:
            removed = await clean_guild_wishlists(guild)
            if removed:
                print(f"[WISHLIST CLEAN/auto] guild={guild.id} removed={removed}")
        except Exception as e:
            print(f"Warning: auto wishlist cleanup failed for guild {guild.id}: {e}")


# ── Events ────────────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    load_stat_formats()
    client.add_view(LootView())
    if not weekly_wishlist_cleanup.is_running():
        weekly_wishlist_cleanup.start()
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"SYNC ERROR: {type(e).__name__}: {e}")
    print(f"Logged in as {client.user}")


if __name__ == "__main__":
    client.run(TOKEN)

import os
import re
import time
import asyncio
import requests
import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")
BASE_URL = "https://questlog.gg/throne-and-liberty/api/trpc"

API_TIMEOUT = 8          # secondes avant timeout questlog
STAT_FORMAT_TTL = 86400  # 24h en secondes

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
    import json
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
    import json
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
    lvl = "12"

    # ── Base Stats ────────────────────────────────────────────────────────────
    main = (stats.get("main") or {}).get(lvl, {})
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
        embed.add_field(name="⚔️ Base Stats (+12)", value=" │ ".join(stat_lines), inline=False)

    # ── Unique Skill ──────────────────────────────────────────────────────────
    passive = item.get("passives")
    if passive and passive.get("name"):
        desc = re.sub(r"<[^>]+>", "", passive.get("text", ""))
        embed.add_field(name=f"✨ {passive['name']}", value=desc or "No description", inline=False)

    # ── Extra Stats ───────────────────────────────────────────────────────────
    extra = (stats.get("extra") or {}).get(lvl, {})
    extra_parts = [format_stat(k, v) for k, v in extra.items()]
    if extra_parts:
        embed.add_field(name="📊 Stats (+12)", value=" │ ".join(extra_parts), inline=False)

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


# ── Autocomplete ──────────────────────────────────────────────────────────────

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


# ── Events ────────────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    load_stat_formats()
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"SYNC ERROR: {type(e).__name__}: {e}")
    print(f"Logged in as {client.user}")


if __name__ == "__main__":
    client.run(TOKEN)

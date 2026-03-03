import os
import re
import json
import time
import asyncio
import subprocess
import requests
import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")
ITEMS_PATH = "/app/data/items.json"
ITEMS_REFRESH_HOURS = 24

# Rarity colors for embed
RARITY_COLORS = {
    10: 0xE040FB,  # Epic (purple)
    11: 0xE040FB,  # Epic II
    12: 0xE040FB,  # Epic III
    9:  0x42A5F5,  # Rare (blue)
    8:  0x66BB6A,  # Uncommon (green)
}

ICON_BASE_URL = "https://cdn.tldb.info/db/images/ags/v41/128/image/"

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ── Items list (RAM cache) ────────────────────────────────────────────────────

_items: list[dict] = []
_items_loaded_at: float = 0.0


def load_items_from_disk():
    global _items, _items_loaded_at
    try:
        with open(ITEMS_PATH, "r", encoding="utf-8") as f:
            _items = json.load(f)
        _items_loaded_at = time.time()
        print(f"Loaded {len(_items)} items from {ITEMS_PATH}")
    except Exception as e:
        print(f"Warning: could not load items list: {e}")
        _items = []


def refresh_items_if_needed():
    if (time.time() - _items_loaded_at) / 3600 < ITEMS_REFRESH_HOURS:
        return
    print("Item list outdated, refreshing...")
    try:
        result = subprocess.run(
            ["node", "/app/fetch_items.mjs"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            load_items_from_disk()
        else:
            print(f"fetch_items.mjs failed: {result.stderr}")
    except Exception as e:
        print(f"Could not refresh items: {e}")


def search_items_local(query: str) -> list[dict]:
    refresh_items_if_needed()
    q = query.lower()
    return [item for item in _items if q in item["name"].lower()][:25]


# ── Fetch item details via Node.js ───────────────────────────────────────────

def fetch_tldb_item(item_id: str) -> dict | None:
    """Call fetch_item_details.mjs and return parsed JSON."""
    try:
        result = subprocess.run(
            ["node", "/app/fetch_item_details.mjs", item_id],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0 or not result.stdout:
            print(f"fetch_item_details error: {result.stderr}")
            return None
        return json.loads(result.stdout)
    except Exception as e:
        print(f"fetch_tldb_item exception: {e}")
        return None


# ── Format stat value ─────────────────────────────────────────────────────────

def fmt(stat: dict) -> str:
    return f"{stat['name']}: {stat['value']}"


# ── Build Discord embed ───────────────────────────────────────────────────────

def build_embed(data: dict, item_id: str) -> discord.Embed:
    rarity_num = data.get("rarity", 0)
    color = RARITY_COLORS.get(rarity_num, discord.Color.blurple().value)

    # Rarity label from rarity number
    rarity_labels = {10: "Epic", 11: "Epic II", 12: "Epic III",
                     9: "Rare", 8: "Uncommon", 7: "Common"}
    rarity_label = rarity_labels.get(rarity_num, f"Rarity {rarity_num}")
    item_type = data.get("type", "")

    url = f"https://tldb.info/db/item/{item_id}"

    embed = discord.Embed(
        title=data["name"],
        url=url,
        description=f"{rarity_label} {item_type}".strip(),
        color=color
    )

    # Icon
    icon_path = data.get("icon", "")
    if icon_path:
        icon_url = ICON_BASE_URL + icon_path.replace("Image/", "").lower() + ".png"
        embed.set_thumbnail(url=icon_url)

    # Base Stats
    main_stats = data.get("main_stats", [])
    if main_stats:
        lines = [f"{s['name']}: {s['value']}" for s in main_stats]
        embed.add_field(
            name="⚔️ Base Stats (+12)",
            value="\n".join(lines),
            inline=False
        )

    # Unique Skill
    skill = data.get("skill")
    if skill and skill.get("name"):
        # Strip HTML tags from description
        desc = re.sub(r"<[^>]+>", "", skill.get("description", ""))
        embed.add_field(
            name=f"✨ {skill['name']}",
            value=desc or "No description",
            inline=False
        )

    # Extra stats (Fortitude, Perception…)
    extra_stats = data.get("extra_stats", [])
    if extra_stats:
        embed.add_field(
            name="📊 Stats (+12)",
            value=" / ".join(fmt(s) for s in extra_stats),
            inline=False
        )

    # Description
    raw_desc = data.get("description", "")
    if raw_desc:
        desc_clean = re.sub(r"<[^>]+>", "", raw_desc)
        embed.add_field(name="📖 Description", value=desc_clean, inline=False)

    # EU Auction House prices — affiche uniquement le prix le plus bas
    eu_prices = data.get("eu_prices", {})
    listed = {s: e for s, e in eu_prices.items() if e and e["quantity"] > 0}
    if listed:
        best = min(listed.values(), key=lambda e: e["price"])
        price_fmt = f"{best['price']:,}".replace(",", " ")
        embed.add_field(
            name="🏪 Auction House (EU)",
            value=f"{price_fmt} ◈ ×{best['quantity']}",
            inline=True
        )
    elif eu_prices:
        embed.add_field(name="🏪 Auction House (EU)", value="Not listed", inline=True)

    return embed


# ── Slash command ─────────────────────────────────────────────────────────────

@tree.command(name="item", description="Rechercher un item TLDB par nom")
@app_commands.describe(item_name="Commence à taper le nom de l'item...")
async def item_command(interaction: discord.Interaction, item_name: str):
    await interaction.response.defer()

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, fetch_tldb_item, item_name)

    if not data:
        await interaction.followup.send(
            f"❌ Item introuvable : `{item_name}`\n"
            "💡 Utilise l'autocomplétion pour sélectionner un item."
        )
        return

    embed = build_embed(data, item_name)
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
    results = await loop.run_in_executor(None, search_items_local, current)
    return [
        app_commands.Choice(name=r["name"][:100], value=r["id"])
        for r in results
    ]


# ── Events ────────────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    load_items_from_disk()
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)
    print(f"Logged in as {client.user}")


if __name__ == "__main__":
    client.run(TOKEN)

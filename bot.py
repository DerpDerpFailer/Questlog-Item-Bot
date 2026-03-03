import os
import re
import json
import time
import asyncio
import subprocess
import requests
from bs4 import BeautifulSoup
import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")
ITEMS_PATH = "/app/data/items.json"
ITEMS_REFRESH_HOURS = 24

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ==========================
# ITEMS LIST (chargée en RAM)
# ==========================
_items: list[dict] = []
_items_loaded_at: float = 0.0


def clean(text):
    return re.sub(r'\s+', ' ', text).strip()


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
    age_hours = (time.time() - _items_loaded_at) / 3600
    if age_hours < ITEMS_REFRESH_HOURS:
        return
    print(f"Item list is {age_hours:.1f}h old, refreshing...")
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


# ==========================
# FETCH + PARSE ITEM
# ==========================
def fetch_tldb_item(item_id: str) -> dict | None:
    url = f"https://tldb.info/db/item/{item_id}"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
    except requests.exceptions.RequestException:
        return None

    soup = BeautifulSoup(r.text, "lxml")

    # ── NAME ──────────────────────────────────────────────
    name_tag = soup.find("h1")
    if not name_tag:
        return None
    item_name = clean(name_tag.text)

    # ── IMAGE ─────────────────────────────────────────────
    image_url = None
    item_img = soup.find("div", class_=re.compile(r"\bitem-image\b"))
    if item_img:
        img = item_img.find("img")
        if img and img.get("src"):
            image_url = img["src"]

    # ── RARITY ────────────────────────────────────────────
    rarity = "Unknown"
    rarity_tag = soup.find("span", class_="item-header-rarity-name")
    if rarity_tag:
        rarity = clean(rarity_tag.text)

    # ── TYPE (Belt / Orb / Sword…) ────────────────────────
    item_type = ""
    # Le type est dans un span.text-truncate à l'intérieur du header
    header = soup.find("div", class_=re.compile(r"\bitem-header\b"))
    if header:
        type_tag = header.find("span", class_="text-truncate")
        if type_tag:
            item_type = clean(type_tag.text)

    # ── BASE STATS PRIMAIRES ───────────────────────────────
    # Stat principale (grand affichage) : container-sub-panel avec flex-fill
    primary_stats = []
    for block in soup.find_all("div", class_=re.compile(r"\bflex-fill\b")):
        name_el = block.find("span", class_=re.compile(r"\bstat-name\b"))
        value_el = block.find("span", class_=re.compile(r"\bstat-value\b"))
        if name_el and value_el:
            primary_stats.append(f"{clean(name_el.text)} {clean(value_el.get_text())}")

    # Stats secondaires de base (Attack Speed, Range) : stat-name w-50
    for row in soup.find_all("span", class_=re.compile(r"\bstat-name\b.*\bw-50\b|\bw-50\b.*\bstat-name\b")):
        stat_name = clean(row.text)
        value_container = row.find_next_sibling("span", class_=re.compile(r"\bcontainer-sub-panel\b"))
        if value_container:
            stat_val = clean(value_container.get_text())
            primary_stats.append(f"{stat_name} {stat_val}")

    # ── STATS SECONDAIRES (Dexterity, Strength…) ──────────
    # Dans container-sub-panel d-flex flex-column gap-0
    # On s'arrête dès qu'on rencontre "Possible Traits"
    secondary_stats = []
    sec_container = soup.find("span", class_=re.compile(r"\bcontainer-sub-panel\b.*\bgap-0\b|\bgap-0\b.*\bcontainer-sub-panel\b"))
    if sec_container:
        for row in sec_container.children:
            if not hasattr(row, 'get'):
                continue
            # Stop at "Possible Traits"
            if "text-accent" in " ".join(row.get("class", [])):
                break
            name_el = row.find("span", class_=re.compile(r"\bstat-name\b"))
            value_el = row.find("span", class_=re.compile(r"\bstat-value\b"))
            if name_el and value_el:
                stat_name = clean(name_el.text).rstrip(":")
                stat_val = clean(value_el.get_text())
                secondary_stats.append(f"{stat_name}: {stat_val}")

    # ── UNIQUE SKILL ──────────────────────────────────────
    # Ancre : div contenant unique-skill-icon
    skill_name = ""
    skill_desc = ""
    skill_icon_div = soup.find("div", class_=re.compile(r"\bunique-skill-icon\b"))
    if skill_icon_div:
        skill_block = skill_icon_div.find_parent("div")
        if skill_block:
            name_el = skill_block.find("span", class_=re.compile(r"\btext-accent\b"))
            desc_el = skill_block.find("span", class_=re.compile(r"\bunique-skill-description\b"))
            if name_el:
                skill_name = clean(name_el.text)
            if desc_el:
                skill_desc = clean(desc_el.text)

    # ── DESCRIPTION ───────────────────────────────────────
    description = ""
    desc_tag = soup.find("h2", class_="item-description")
    if desc_tag:
        description = clean(desc_tag.get_text())

    return {
        "name": item_name,
        "rarity": rarity,
        "type": item_type,
        "primary_stats": primary_stats,
        "secondary_stats": secondary_stats,
        "skill_name": skill_name,
        "skill_desc": skill_desc,
        "description": description,
        "url": url,
        "image": image_url,
    }


# ==========================
# SLASH COMMAND /item
# ==========================
@tree.command(name="item", description="Rechercher un item TLDB par nom")
@app_commands.describe(item_name="Commence à taper le nom de l'item...")
async def item_command(interaction: discord.Interaction, item_name: str):
    await interaction.response.defer()

    data = fetch_tldb_item(item_name)
    if not data:
        await interaction.followup.send(
            f"❌ Item introuvable : `{item_name}`\n"
            "💡 Utilise l'autocomplétion pour sélectionner un item dans la liste."
        )
        return

    # Titre : Nom — Rarity · Type
    title_parts = [data["rarity"]]
    if data["type"]:
        title_parts.append(data["type"])

    embed = discord.Embed(
        title=data["name"],
        url=data["url"],
        description=" · ".join(title_parts),
        color=discord.Color.blurple()
    )

    if data["image"]:
        embed.set_thumbnail(url=data["image"])

    # Base Stats
    if data["primary_stats"]:
        embed.add_field(
            name="⚔️ Base Stats",
            value=" / ".join(data["primary_stats"]),
            inline=False
        )

    # Unique Skill
    if data["skill_name"]:
        embed.add_field(
            name=f"✨ Unique Skill — {data['skill_name']}",
            value=data["skill_desc"] or "Pas de description",
            inline=False
        )

    # Stats secondaires
    if data["secondary_stats"]:
        embed.add_field(
            name="📊 Stats",
            value=" / ".join(data["secondary_stats"]),
            inline=False
        )

    # Description
    if data["description"]:
        embed.add_field(name="📖 Description", value=data["description"], inline=False)

    embed.set_footer(text="Data from TLDB.info")
    await interaction.followup.send(embed=embed)


# ==========================
# AUTOCOMPLETE
# ==========================
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


# ==========================
# EVENTS
# ==========================
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

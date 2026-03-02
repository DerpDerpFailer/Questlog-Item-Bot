import os
import re
import asyncio
import time
import requests
from bs4 import BeautifulSoup
import discord
from discord import app_commands

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ==========================
# SIMPLE CACHE (TTL 5 min)
# ==========================
_search_cache: dict[str, tuple[list, float]] = {}
CACHE_TTL = 300  # secondes


def clean_text(text):
    return re.sub(r'\s+', ' ', text).strip()


# ==========================
# RECHERCHE D'ITEMS PAR NOM
# ==========================
def search_items(query: str) -> list[dict]:
    """Cherche des items par nom sur tldb.info. Retourne [{name, id}]."""
    if len(query) < 2:
        return []

    cache_key = query.lower()
    if cache_key in _search_cache:
        results, ts = _search_cache[cache_key]
        if time.time() - ts < CACHE_TTL:
            return results

    url = "https://tldb.info/db/items/page/1"
    headers = {"User-Agent": "Mozilla/5.0"}
    params = {"search": query}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=5)
        response.raise_for_status()
    except requests.exceptions.RequestException:
        return []

    soup = BeautifulSoup(response.text, "lxml")

    results = []
    seen_ids = set()

    # Les items sont dans des liens de la forme /db/item/{id}
    for link in soup.find_all("a", href=re.compile(r"^/db/item/[^/]+")):
        href = link.get("href", "")
        item_id = href.split("/db/item/")[-1].strip("/")
        item_name = clean_text(link.get_text())

        if item_id and item_name and item_id not in seen_ids:
            seen_ids.add(item_id)
            results.append({"name": item_name, "id": item_id})

    results = results[:25]
    _search_cache[cache_key] = (results, time.time())
    return results


# ==========================
# FETCH ITEM DETAILS
# ==========================
def fetch_tldb_item(item_id: str) -> dict | None:
    url = f"https://tldb.info/db/item/{item_id}"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException:
        return None

    soup = BeautifulSoup(response.text, "lxml")

    # NAME
    name_tag = soup.find("h1")
    if not name_tag:
        return None
    item_name = clean_text(name_tag.text)

    # ICON
    image_url = None
    header = soup.find("div", class_=re.compile("item-header"))
    if header:
        img = header.find("img")
        if img and img.get("src"):
            image_url = img["src"]

    # RARITY
    rarity = "Unknown"
    rarity_tag = soup.find("span", class_=re.compile("item-header-rarity-name"))
    if rarity_tag:
        rarity = clean_text(rarity_tag.text)

    # DESCRIPTION
    description = ""
    desc_tag = soup.find("h2", class_="item-description")
    if desc_tag:
        description = clean_text(desc_tag.text)

    # BASE STATS
    stats = []
    base_stats_text = soup.find(string=re.compile("Base Stats", re.I))
    if base_stats_text:
        base_section = base_stats_text.find_parent()
        current = base_section.find_next()
        while current:
            if current.name in ["h2", "h3"] and "Base Stats" not in current.text:
                break
            if current.name == "span" and current.get("class"):
                if any("stat-name" in c for c in current.get("class")):
                    stat_name = clean_text(current.text.replace(":", ""))
                    value_tag = current.find_next("span", class_=re.compile("stat-value"))
                    if value_tag:
                        stat_value = clean_text(value_tag.text)
                        stats.append(f"• {stat_name}: {stat_value}")
            current = current.find_next()

    # UNIQUE SKILL
    skill_name = ""
    skill_desc = ""
    skill_title = soup.find("span", class_=re.compile("text-accent"))
    if skill_title:
        skill_name = clean_text(skill_title.text)
        skill_description = soup.find("span", class_=re.compile("unique-skill-description"))
        if skill_description:
            skill_desc = clean_text(skill_description.text)

    return {
        "name": item_name,
        "rarity": rarity,
        "description": description,
        "stats": stats,
        "skill_name": skill_name,
        "skill_desc": skill_desc,
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

    # item_name contient l'ID quand sélectionné via autocomplete,
    # ou le texte brut sinon — on tente les deux
    data = fetch_tldb_item(item_name)

    if not data:
        await interaction.followup.send(
            f"❌ Item introuvable : `{item_name}`\n"
            "💡 Utilise l'autocomplétion pour sélectionner un item dans la liste."
        )
        return

    embed = discord.Embed(
        title=data["name"],
        url=data["url"],
        color=discord.Color.blurple()
    )

    if data["image"]:
        embed.set_thumbnail(url=data["image"])

    embed.add_field(name="Rarity", value=data["rarity"], inline=True)

    if data["description"]:
        embed.add_field(name="Description", value=data["description"], inline=False)

    if data["stats"]:
        embed.add_field(
            name="⚔ Base Stats",
            value="\n".join(data["stats"]),
            inline=False
        )

    if data["skill_name"]:
        embed.add_field(
            name=f"✨ Unique Skill — {data['skill_name']}",
            value=data["skill_desc"] if data["skill_desc"] else "Pas de description",
            inline=False
        )

    embed.set_footer(text="Data from TLDB.info")
    await interaction.followup.send(embed=embed)


# ==========================
# AUTOCOMPLETE
# ==========================
@item_command.autocomplete("item_name")
async def item_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    if len(current) < 2:
        return []

    # Appel bloquant dans un thread pour ne pas bloquer la boucle asyncio
    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, search_items, current)

    return [
        app_commands.Choice(
            name=r["name"][:100],  # Discord limite à 100 chars
            value=r["id"]
        )
        for r in results
    ]


# ==========================
# EVENTS
# ==========================
@client.event
async def on_ready():
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)
    print(f"Logged in as {client.user}")


if __name__ == "__main__":
    client.run(TOKEN)

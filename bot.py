import discord
from discord import app_commands
import requests
from bs4 import BeautifulSoup
import os

# =========================
# CONFIG
# =========================

QUESTLOG_SEARCH_URL = "https://questlog.gg/throne-and-liberty/en/db/search"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (DiscordBot)"
}

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# =========================
# DISCORD CLIENT
# =========================

class MyClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

client = MyClient()

# =========================
# SCRAPING FUNCTIONS
# =========================

def search_item(item_name: str) -> str | None:
    """
    Recherche un item par nom sur Questlog.gg
    Retourne l'URL du premier résultat ou None
    """

    params = {
        "q": item_name
    }

    response = requests.get(QUESTLOG_SEARCH_URL, params=params, headers=HEADERS, timeout=10)

    if response.status_code != 200:
        return None

    soup = BeautifulSoup(response.text, "lxml")

    # Premier lien d'item
    link = soup.select_one("a[href*='/db/item/']")

    if not link:
        return None

    return "https://questlog.gg" + link["href"]


def get_item_data(item_url: str) -> dict | None:
    """
    Récupère les infos principales de l'item
    """

    response = requests.get(item_url, headers=HEADERS, timeout=10)

    if response.status_code != 200:
        return None

    soup = BeautifulSoup(response.text, "lxml")

    # Nom
    title = soup.find("h1")
    if not title:
        return None

    name = title.text.strip()

    # Icône
    icon = soup.select_one("img[src*='/icons/']")
    icon_url = icon["src"] if icon else None

    # Description
    description_tag = soup.select_one("div.text-muted")
    description = description_tag.text.strip() if description_tag else "No description available."

    return {
        "name": name,
        "icon": icon_url,
        "description": description,
        "url": item_url
    }

# =========================
# SLASH COMMAND
# =========================

@client.tree.command(name="item", description="Search an item from Questlog.gg")
@app_commands.describe(name="Name of the item")
async def item(interaction: discord.Interaction, name: str):
    await interaction.response.defer()

    item_url = search_item(name)

    if not item_url:
        await interaction.followup.send(
            f"❌ No item found for **{name}**"
        )
        return

    data = get_item_data(item_url)

    if not data:
        await interaction.followup.send(
            "❌ Failed to retrieve item data."
        )
        return

    embed = discord.Embed(
        title=data["name"],
        description=data["description"],
        url=data["url"],
        color=0x2ECC71
    )

    if data["icon"]:
        embed.set_thumbnail(url=data["icon"])

    embed.set_footer(text="Data from Questlog.gg")

    await interaction.followup.send(embed=embed)

# =========================
# RUN
# =========================

client.run(DISCORD_TOKEN)

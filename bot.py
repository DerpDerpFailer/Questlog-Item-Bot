import os
import threading
import requests
import discord
from discord import app_commands
from bs4 import BeautifulSoup
from flask import Flask

# ============================================================
# CONFIGURATION
# ============================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

QUESTLOG_SEARCH_URL = "https://questlog.gg/throne-and-liberty/en/db/search"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Questlog-Discord-Bot)"
}

# ============================================================
# MINI SERVEUR WEB (OBLIGATOIRE POUR RENDER FREE)
# ============================================================

app = Flask(__name__)

@app.route("/")
def home():
    return "Questlog Item Bot is running"

def run_web():
    # Render expose automatiquement le port via la variable PORT
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# Lancement du serveur web dans un thread séparé
threading.Thread(target=run_web, daemon=True).start()

# ============================================================
# CLIENT DISCORD
# ============================================================

class MyClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Synchronisation des slash commands
        await self.tree.sync()

client = MyClient()

# ============================================================
# SCRAPING QUESTLOG.GG
# ============================================================

def search_item(item_name: str) -> str | None:
    """
    Recherche un item sur Questlog.gg via la page HTML de recherche.
    Retourne l'URL du premier item trouvé ou None.
    """
    params = {"q": item_name}

    try:
        response = requests.get(
            QUESTLOG_SEARCH_URL,
            params=params,
            headers=HEADERS,
            timeout=10
        )
    except requests.RequestException:
        return None

    if response.status_code != 200:
        return None

    soup = BeautifulSoup(response.text, "lxml")

    # Premier lien qui correspond à un item
    link = soup.select_one("a[href*='/db/item/']")

    if not link or "href" not in link.attrs:
        return None

    return "https://questlog.gg" + link["href"]


def get_item_data(item_url: str) -> dict | None:
    """
    Récupère les informations principales d'un item Questlog.gg
    """
    try:
        response = requests.get(
            item_url,
            headers=HEADERS,
            timeout=10
        )
    except requests.RequestException:
        return None

    if response.status_code != 200:
        return None

    soup = BeautifulSoup(response.text, "lxml")

    # Nom de l'item
    title = soup.find("h1")
    if not title:
        return None

    name = title.text.strip()

    # Icône de l'item
    icon_tag = soup.select_one("img[src*='/icons/']")
    icon_url = icon_tag["src"] if icon_tag else None

    # Description (si disponible)
    description_tag = soup.select_one("div.text-muted")
    description = (
        description_tag.text.strip()
        if description_tag and description_tag.text.strip()
        else "No description available."
    )

    return {
        "name": name,
        "icon": icon_url,
        "description": description,
        "url": item_url
    }

# ============================================================
# SLASH COMMAND /item
# ============================================================

@client.tree.command(
    name="item",
    description="Search an item from Questlog.gg (Throne and Liberty)"
)
@app_commands.describe(name="Name of the item to search")
async def item(interaction: discord.Interaction, name: str):

    # Discord impose une réponse rapide → on diffère
    await interaction.response.defer()

    # 1) Recherche de l'item
    item_url = search_item(name)

    if not item_url:
        await interaction.followup.send(
            f"❌ No item found for **{name}**"
        )
        return

    # 2) Récupération des données de l'item
    data = get_item_data(item_url)

    if not data:
        await interaction.followup.send(
            "❌ Failed to retrieve item data."
        )
        return

    # 3) Création de l'embed Discord
    embed = discord.Embed(
        title=data["name"],
        description=data["description"],
        url=data["url"],
        color=0x2ECC71
    )

    if data["icon"]:
        embed.set_thumbnail(url=data["icon"])

    embed.set_footer(text="Data from Questlog.gg")

    # 4) Envoi de la réponse
    await interaction.followup.send(embed=embed)

# ============================================================
# LANCEMENT DU BOT
# ============================================================

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is not set")

client.run(DISCORD_TOKEN)

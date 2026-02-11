import discord
from discord import app_commands
import requests
from bs4 import BeautifulSoup
import os
from urllib.parse import urlparse
from flask import Flask
from threading import Thread

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ==============================
# Fonction qui scrape l'URL
# ==============================
def scrape_item_from_url(url):
    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        return None

    soup = BeautifulSoup(response.text, "html.parser")

    # Nom
    title = soup.find("h1")
    if not title:
        return None

    item_name = title.text.strip()

    # Image
    image = soup.find("meta", property="og:image")
    image_url = image["content"] if image else None

    # Description
    description_tag = soup.find("meta", property="og:description")
    description = description_tag["content"] if description_tag else "No description found."

    return {
        "name": item_name,
        "image": image_url,
        "description": description
    }


# ==============================
# Commande /item
# ==============================
@tree.command(name="item", description="Get item info from a Questlog URL")
@app_commands.describe(url="Full Questlog item URL")
async def item(interaction: discord.Interaction, url: str):

    await interaction.response.defer()

    # Vérification URL valide
    parsed = urlparse(url)
    if "questlog.gg" not in parsed.netloc:
        await interaction.followup.send("❌ Please provide a valid Questlog.gg URL.")
        return

    data = scrape_item_from_url(url)

    if not data:
        await interaction.followup.send("❌ Could not retrieve item from this URL.")
        return

    embed = discord.Embed(
        title=data["name"],
        description=data["description"],
        url=url,
        color=0x5865F2
    )

    if data["image"]:
        embed.set_thumbnail(url=data["image"])

    embed.set_footer(text="Data from Questlog.gg")

    await interaction.followup.send(embed=embed)


# ==============================
# Bot Ready
# ==============================
@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user}")

# ==============================
# Mini serveur pour Render
# ==============================

app = Flask(__name__)

@app.route("/")
def home():
    return "Questlog Item Bot is running!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# Lancer le serveur web dans un thread séparé
Thread(target=run_web).start()

client.run(TOKEN)

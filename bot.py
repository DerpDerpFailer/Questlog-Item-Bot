import os
import re
import requests
from bs4 import BeautifulSoup
import discord
from discord import app_commands
from flask import Flask
from threading import Thread

# ==============================
# CONFIG
# ==============================

TOKEN = os.getenv("DISCORD_TOKEN")

INTENTS = discord.Intents.default()
bot = discord.Client(intents=INTENTS)
tree = app_commands.CommandTree(bot)

BASE_URL = "https://questlog.gg"

# ==============================
# QUESTLOG SCRAPER (URL ONLY)
# ==============================

def extract_item_id_from_url(url: str):
    """
    Extract item ID from full Questlog URL.
    Example:
    https://questlog.gg/throne-and-liberty/en/db/item/wand_aa_t3_normal_002
    """
    match = re.search(r"/db/item/([a-zA-Z0-9_]+)", url)
    if match:
        return match.group(1)
    return None


def fetch_item_data(item_url: str):
    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    response = requests.get(item_url, headers=headers)

    if response.status_code != 200:
        return None

    soup = BeautifulSoup(response.text, "lxml")

    # Title
    title = soup.find("h1")
    if not title:
        return None

    item_name = title.text.strip()

    # Try to get description (optional)
    description = ""
    desc_block = soup.find("div", class_="description")
    if desc_block:
        description = desc_block.text.strip()

    return {
        "name": item_name,
        "description": description,
        "url": item_url
    }

# ==============================
# DISCORD COMMAND
# ==============================

@tree.command(name="item", description="Get item info from a Questlog URL")
@app_commands.describe(url="Full Questlog item URL")
async def item(interaction: discord.Interaction, url: str):

    await interaction.response.defer()

    if "questlog.gg" not in url:
        await interaction.followup.send("❌ Please provide a valid Questlog URL.")
        return

    item_id = extract_item_id_from_url(url)

    if not item_id:
        await interaction.followup.send("❌ Invalid item URL format.")
        return

    data = fetch_item_data(url)

    if not data:
        await interaction.followup.send("❌ Could not fetch item data.")
        return

    embed = discord.Embed(
        title=data["name"],
        url=data["url"],
        description=data["description"] or "No description available.",
        color=0x5865F2
    )

    embed.set_footer(text="Data from Questlog.gg")

    await interaction.followup.send(embed=embed)


@bot.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {bot.user}")

# ==============================
# FLASK KEEP-ALIVE (RENDER)
# ==============================

app = Flask(__name__)

@app.route("/")
def home():
    return "Questlog Item Bot is running!"

def run_discord():
    bot.run(TOKEN)

# ==============================
# MAIN
# ==============================

if __name__ == "__main__":
    # Start Discord in separate thread
    Thread(target=run_discord).start()

    # Start Flask in main thread
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

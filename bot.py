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

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# ==============================
# UTILS
# ==============================

def clean_html(raw_html):
    soup = BeautifulSoup(raw_html, "lxml")
    return soup.get_text("\n", strip=True)

def detect_rarity(soup):
    page_text = soup.get_text().lower()

    if "heroic" in page_text:
        return "Heroic", 0x9B59B6  # Purple
    elif "epic" in page_text:
        return "Epic", 0xE91E63
    elif "rare" in page_text:
        return "Rare", 0x3498DB
    elif "uncommon" in page_text:
        return "Uncommon", 0x2ECC71
    elif "common" in page_text:
        return "Common", 0x95A5A6

    return "Unknown", 0x5865F2

# ==============================
# TLDB SCRAPER
# ==============================

def fetch_tldb_item(url: str):
    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Request error: {e}")
        return None

    soup = BeautifulSoup(response.text, "lxml")

    # Item name
    title_tag = soup.find("h1")
    if not title_tag:
        print("No <h1> found.")
        return None

    item_name = title_tag.text.strip()

    # Image
    image_tag = soup.find("meta", property="og:image")
    image_url = image_tag["content"] if image_tag else None

    # Full tooltip block (more reliable than description div)
    tooltip_block = soup.find("div", class_=re.compile("tooltip", re.I))

    description = ""
    stats = ""

    if tooltip_block:
        text = clean_html(str(tooltip_block))

        # Split stats if present
        if "Stats:" in text:
            parts = text.split("Stats:")
            description = parts[0].strip()
            stats = parts[1].strip()
        else:
            description = text.strip()

    # Detect rarity
    rarity, color = detect_rarity(soup)

    return {
        "name": item_name,
        "image": image_url,
        "description": description,
        "stats": stats,
        "rarity": rarity,
        "color": color,
        "url": url
    }

# ==============================
# DISCORD COMMAND
# ==============================

@tree.command(name="item", description="Get item info from a TLDB URL")
@app_commands.describe(url="Full TLDB item URL (https://tldb.info/db/item/...)")
async def item(interaction: discord.Interaction, url: str):

    await interaction.response.defer()

    if not url.startswith("https://tldb.info/db/item/"):
        await interaction.followup.send("❌ Please provide a valid TLDB item URL.")
        return

    data = fetch_tldb_item(url)

    if not data:
        await interaction.followup.send("❌ Could not fetch item data from TLDB.")
        return

    embed = discord.Embed(
        title=data["name"],
        url=data["url"],
        description=data["description"][:4000],
        color=data["color"]
    )

    if data["image"]:
        embed.set_thumbnail(url=data["image"])

    embed.add_field(
        name="Rarity",
        value=data["rarity"],
        inline=True
    )

    if data["stats"]:
        embed.add_field(
            name="Stats",
            value=data["stats"][:1000],
            inline=False
        )

    embed.set_footer(text="Data from TLDB.info")

    await interaction.followup.send(embed=embed)


@bot.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {bot.user}")

# ==============================
# FLASK (RENDER KEEP-ALIVE)
# ==============================

app = Flask(__name__)

@app.route("/")
def home():
    return "TLDB Item Bot is running!"

def run_discord():
    bot.run(TOKEN)

# ==============================
# MAIN
# ==============================

if __name__ == "__main__":
    Thread(target=run_discord).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

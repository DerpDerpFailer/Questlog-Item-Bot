import os
import re
import requests
from bs4 import BeautifulSoup
import discord
from discord import app_commands
from flask import Flask
import threading

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running"

def run_web():
    app.run(host="0.0.0.0", port=10000)

def clean_text(text):
    return re.sub(r'\s+', ' ', text).strip()

def fetch_tldb_item(item_id):
    url = f"https://tldb.info/db/item/{item_id}"
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        return None

    soup = BeautifulSoup(response.text, "lxml")

    # =========================
    # ITEM NAME
    # =========================
    title_tag = soup.find("h1")
    if not title_tag:
        return None
    item_name = clean_text(title_tag.text)

    # =========================
    # IMAGE
    # =========================
    image_url = None
    img_tag = soup.find("img")
    if img_tag and img_tag.get("src"):
        image_url = img_tag["src"]
        if image_url.startswith("/"):
            image_url = "https://tldb.info" + image_url

    # =========================
    # RARITY
    # =========================
    rarity = "Unknown"
    rarity_block = soup.find(string=re.compile("Rarity"))
    if rarity_block:
        parent = rarity_block.find_parent("div")
        if parent:
            rarity_text = parent.get_text(separator=" ").replace("Rarity", "")
            rarity = clean_text(rarity_text)

    # =========================
    # DESCRIPTION
    # =========================
    description = ""
    desc_block = soup.find("h2", class_="item-description")
    if desc_block:
        description = clean_text(desc_block.text)

    # =========================
    # BASE STATS (ONLY FIRST BLOCK)
    # =========================
    stats = []

    stat_container = soup.find("div", class_=re.compile("stats-container"))
    if stat_container:
        names = stat_container.find_all("span", class_=re.compile("stat-name"))
        for name_tag in names:
            value_tag = name_tag.find_next("span", class_=re.compile("stat-value"))
            if value_tag:
                name = clean_text(name_tag.text.replace(":", ""))
                value = clean_text(value_tag.text)
                stats.append(f"• {name}: {value}")

    # =========================
    # UNIQUE SKILL
    # =========================
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
        "image": image_url
    }

@tree.command(name="item", description="Get TLDB item by ID")
@app_commands.describe(item_id="Example: spear2h_aa_t2_raid_001")
async def item(interaction: discord.Interaction, item_id: str):
    await interaction.response.defer()

    data = fetch_tldb_item(item_id)

    if not data:
        await interaction.followup.send("❌ Item not found.")
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
            value=data["skill_desc"] if data["skill_desc"] else "No description",
            inline=False
        )

    embed.set_footer(text="Data from TLDB.info")

    await interaction.followup.send(embed=embed)

@client.event
async def on_ready():
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)

    print(f"Logged in as {client.user}")

if __name__ == "__main__":
    threading.Thread(target=run_web).start()
    client.run(TOKEN)

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

    # ======================
    # NAME
    # ======================
    name_tag = soup.find("h1")
    if not name_tag:
        return None
    item_name = clean_text(name_tag.text)

    # ======================
    # ICON
    # ======================
    image_url = None
    header = soup.find("div", class_=re.compile("item-header"))
    if header:
        img = header.find("img")
        if img and img.get("src"):
            image_url = img["src"]

    # ======================
    # RARITY
    # ======================
    rarity = "Unknown"
    rarity_tag = soup.find("span", class_=re.compile("item-header-rarity-name"))
    if rarity_tag:
        rarity = clean_text(rarity_tag.text)

    # ======================
    # DESCRIPTION
    # ======================
    description = ""
    desc_tag = soup.find("h2", class_="item-description")
    if desc_tag:
        description = clean_text(desc_tag.text)

    # ======================
    # BASE STATS (STOP AT POSSIBLE TRAITS)
    # ======================
    stats = []

    stats_containers = soup.find_all("div", class_=re.compile("stats-container"))

    stop_parsing = False

    for container in stats_containers:
        if stop_parsing:
            break

        stat_blocks = container.find_all("span", class_=re.compile("stat-name"))

        for stat_name_tag in stat_blocks:
            stat_name = clean_text(stat_name_tag.text.replace(":", ""))

            # STOP condition
            if "Possible Traits" in stat_name:
                stop_parsing = True
                break

            # Find corresponding value
            value_tag = stat_name_tag.find_next("span", class_=re.compile("stat-value"))
            if value_tag:
                stat_value = clean_text(value_tag.text)
                stats.append(f"• {stat_name}: {stat_value}")

    # ======================
    # UNIQUE SKILL
    # ======================
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
@app_commands.describe(item_id="Example: sword2h_aa_t2_raid_001")
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

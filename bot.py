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

    # ITEM NAME
    title_tag = soup.find("h1")
    if not title_tag:
        return None
    item_name = clean_text(title_tag.text)

    # RARITY
    rarity = "Unknown"
    rarity_block = soup.find(string=re.compile("Rarity"))
    if rarity_block:
        parent = rarity_block.find_parent()
        if parent:
            rarity = clean_text(parent.text.replace("Rarity", ""))

    # DESCRIPTION
    description = ""
    desc_block = soup.find("h2", class_="item-description")
    if desc_block:
        description = clean_text(desc_block.text)

    # MAIN STATS (Damage, Speed, Range etc.)
    stats = []

    stat_containers = soup.find_all("div", class_=re.compile("stats-container"))

    for container in stat_containers:
        names = container.find_all("span", class_=re.compile("stat-name"))
        for name_tag in names:
            value_tag = name_tag.find_next("span", class_=re.compile("stat-value"))
            if value_tag:
                name = clean_text(name_tag.text.replace(":", ""))
                value = clean_text(value_tag.text)
                stats.append(f"• **{name}**: {value}")

    # UNIQUE SKILL
    skill_name = ""
    skill_desc = ""

    skill_title = soup.find("span", class_=re.compile("text-accent"))
    if skill_title:
        skill_name = clean_text(skill_title.text)

        skill_description = skill_title.find_next("span", class_=re.compile("unique-skill-description"))
        if skill_description:
            skill_desc = clean_text(skill_description.text)

    # SALE PRICE
    sale_price = ""
    sale_block = soup.find(string=re.compile("Sale Price"))
    if sale_block:
        parent = sale_block.find_parent()
        if parent:
            sale_price = clean_text(parent.text.replace("Sale Price:", ""))

    return {
        "name": item_name,
        "rarity": rarity,
        "description": description,
        "stats": stats,
        "skill_name": skill_name,
        "skill_desc": skill_desc,
        "sale_price": sale_price,
        "url": url
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
        color=discord.Color.purple()
    )

    embed.add_field(name="Rarity", value=data["rarity"], inline=True)

    if data["description"]:
        embed.add_field(name="Description", value=data["description"], inline=False)

    if data["stats"]:
        stats_text = "\n".join(data["stats"][:15])  # limit to avoid embed overflow
        embed.add_field(name="Stats", value=stats_text, inline=False)

    if data["skill_name"]:
        embed.add_field(
            name=f"Unique Skill — {data['skill_name']}",
            value=data["skill_desc"] if data["skill_desc"] else "No description",
            inline=False
        )

    if data["sale_price"]:
        embed.add_field(name="Sale Price", value=data["sale_price"], inline=True)

    embed.set_footer(text="Data from TLDB.info")

    await interaction.followup.send(embed=embed)

@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user}")

if __name__ == "__main__":
    threading.Thread(target=run_web).start()
    client.run(TOKEN)

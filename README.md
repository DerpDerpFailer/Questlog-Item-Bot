# 🗡️ Questlog Item Bot

A Discord bot to look up Throne & Liberty items directly in your server, powered by [questlog.gg](https://questlog.gg/throne-and-liberty).

## ✨ Features

- **Autocomplete** — start typing an item name and pick from a live list
- **Stats at +12** — all stats shown at maximum enchantment level
- **Unique skill** — skill name and full description
- **EU Auction House prices** — min price and stock in real time

## 🚀 Deployment

**Prerequisites:** Docker & Docker Compose + a Discord bot token

```bash
git clone https://github.com/DerpDerpFailer/Questlog-Item-Bot.git
cd Questlog-Item-Bot
cp .env.example .env
# Edit .env and add your DISCORD_TOKEN
docker compose up -d
```

## 📋 Commands

| Command | Description |
|---|---|
| `/item <name>` | Search for an item by name with autocomplete |

## 📡 Data Source

Data from [questlog.gg](https://questlog.gg/throne-and-liberty) (personal use only).

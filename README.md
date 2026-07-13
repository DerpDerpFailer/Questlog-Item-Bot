# 🗡️ Questlog Item Bot

A Discord bot to look up Throne & Liberty items directly in your server, powered by [questlog.gg](https://questlog.gg/throne-and-liberty).

## ✨ Features

- **Autocomplete** — start typing an item name and pick from a live list
- **Stats at max level** — stats shown at the item's highest available level (or its fixed level for gear that has one)
- **Unique skill** — skill name and full description
- **EU Auction House prices** — min price and stock in real time
- **Price history** — 7/30 day price trend, average, min/max and stock
- **Loot tracking** — role-gated Main PvP / Main PvE / Alternate Build interest list per item
- **Loot wishlist** — each member can wishlist up to a configurable number of items per server

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
| `/price <name> [days]` | Auction House price history (EU) — 7 or 30 days |
| `/item-loot <name>` | Same as `/item`, plus Main PvP / Main PvE / Alternate Build loot interest buttons (restricted roles, see `/item-setup`) |
| `/item-setup <role_commande> <role_boutons>` | Admin only — configure which roles can run `/item-loot` and click its buttons |
| `/wishlist [name]` | Add an item to your wishlist, or view/edit your current wishlist if no name is given |
| `/wishlist-setup [limit] [role_staff]` | Admin only — configure the max wishlist size per member (1-25) and/or the staff role for `/wishlist-check` and `/wishlist-export` |
| `/wishlist-check <name>` | Staff only — list every member who has this item in their wishlist |
| `/wishlist-export` | Staff only — view every member's wishlist on this server, with a button to export as CSV |

## 📡 Data Source

Data from [questlog.gg](https://questlog.gg/throne-and-liberty) (personal use only).

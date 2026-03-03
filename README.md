<div align="center">

# 🗡️ TLDB Discord Bot

**[English](#english) · [Français](#français)**

A Discord bot to look up Throne & Liberty items directly in your server, with autocomplete and stats at +12.

Un bot Discord pour rechercher des items de Throne & Liberty directement dans votre serveur, avec autocomplétion et stats à +12.

![Discord](https://img.shields.io/badge/discord.py-5865F2?style=flat&logo=discord&logoColor=white)
![Node.js](https://img.shields.io/badge/Node.js-339933?style=flat&logo=node.js&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?style=flat&logo=docker&logoColor=white)

</div>

---

## English

### ✨ Features

- **Autocomplete** — start typing an item name and pick from a live list
- **Stats at +12** — all stats shown at maximum enchantment level
- **Unique skill** — skill name and full description
- **Item icon** — displayed as thumbnail in the embed
- **EU Auction House prices** — lowest listed price across EU servers
- **Color-coded rarity** — embed color matches item rarity (Common → Epic III)
- **Auto-refresh** — item list updates every 24 hours automatically

### 🛠️ Stack

- **Python** + `discord.py` — Discord bot
- **Node.js** + `devalue` + `compress-json` — decoding TLDB's internal API
- **Docker** + **Docker Compose** — deployment

### 🚀 Deployment

**Prerequisites**
- Docker & Docker Compose
- A Discord bot token ([discord.com/developers](https://discord.com/developers))

**Installation**

```bash
git clone https://github.com/<your-user>/<your-repo>.git
cd <your-repo>
```

Create a `.env` file at the root:

```env
DISCORD_TOKEN=your_token_here
```

Start the stack:

```bash
docker compose up -d
```

On startup, the container will:
1. Download and index the full TLDB item list
2. Start the Discord bot

### 📋 Commands

| Command | Description |
|---|---|
| `/item <name>` | Search for an item by name with autocomplete |

### 📁 Project Structure

```
.
├── bot.py                    # Discord bot (Python)
├── fetch_items.mjs           # Item list indexing (Node.js)
├── fetch_item_details.mjs    # Item detail fetching with +12 stats (Node.js)
├── Dockerfile
├── docker-compose.yml
├── package.json
├── requirements.txt
└── data/
    └── items.json            # Local item list cache (generated)
```

### 📡 Data Source

Data comes from [tldb.info](https://tldb.info), the reference database for Throne & Liberty. The internal API is used in accordance with [TLDB's terms of use](https://tldb.info/internal-docs) (personal use only).

---

## Français

### ✨ Fonctionnalités

- **Autocomplétion** — tapez le début d'un nom d'item et sélectionnez dans la liste
- **Stats à +12** — toutes les stats affichées au niveau d'enchantement maximum
- **Skill unique** — nom et description complète du skill
- **Icône de l'item** — affichée en thumbnail dans l'embed
- **Prix de l'Hôtel des Ventes EU** — prix le plus bas parmi les serveurs EU
- **Rareté en couleur** — la couleur de l'embed correspond à la rareté (Commun → Épique III)
- **Rafraîchissement automatique** — la liste d'items se met à jour toutes les 24h

### 🛠️ Stack technique

- **Python** + `discord.py` — bot Discord
- **Node.js** + `devalue` + `compress-json` — décodage de l'API interne TLDB
- **Docker** + **Docker Compose** — déploiement

### 🚀 Déploiement

**Prérequis**
- Docker & Docker Compose
- Un token de bot Discord ([discord.com/developers](https://discord.com/developers))

**Installation**

```bash
git clone https://github.com/<votre-user>/<votre-repo>.git
cd <votre-repo>
```

Créez un fichier `.env` à la racine :

```env
DISCORD_TOKEN=votre_token_ici
```

Lancez la stack :

```bash
docker compose up -d
```

Au démarrage, le container va :
1. Télécharger et indexer la liste complète des items TLDB
2. Lancer le bot Discord

### 📋 Commandes

| Commande | Description |
|---|---|
| `/item <nom>` | Recherche un item par nom avec autocomplétion |

### 📁 Structure du projet

```
.
├── bot.py                    # Bot Discord (Python)
├── fetch_items.mjs           # Indexation de la liste d'items (Node.js)
├── fetch_item_details.mjs    # Récupération des détails + stats à +12 (Node.js)
├── Dockerfile
├── docker-compose.yml
├── package.json
├── requirements.txt
└── data/
    └── items.json            # Cache local de la liste d'items (généré)
```

### 📡 Source des données

Les données proviennent de [tldb.info](https://tldb.info), la base de données de référence pour Throne & Liberty. L'API interne est utilisée conformément aux [conditions d'utilisation de TLDB](https://tldb.info/internal-docs) (usage personnel uniquement).

# 🗡️ TLDB Discord Bot

Bot Discord pour rechercher des items de **Throne & Liberty** directement depuis Discord, avec autocomplétion et stats à +12.

---

## ✨ Fonctionnalités

- **Autocomplétion** — tape le début d'un nom d'item et sélectionne dans la liste
- **Stats à +12** — toutes les stats sont affichées au niveau d'enchantement maximum
- **Skill unique** — nom et description du skill de l'item
- **Icône de l'item** — affichée en thumbnail dans l'embed
- **Rafraîchissement automatique** — la liste d'items se met à jour toutes les 24h

---

## 🛠️ Stack

- **Python** + `discord.py` — bot Discord
- **Node.js** + `devalue` + `compress-json` — décodage de l'API interne TLDB
- **Docker** + **Docker Compose** — déploiement

---

## 🚀 Déploiement

### Prérequis

- Docker & Docker Compose
- Un token de bot Discord ([discord.com/developers](https://discord.com/developers))

### Installation

```bash
git clone https://github.com/<ton-user>/<ton-repo>.git
cd <ton-repo>
```

Crée un fichier `.env` à la racine :

```env
DISCORD_TOKEN=ton_token_ici
```

Lance la stack :

```bash
docker compose up -d
```

Au démarrage, le container :
1. Télécharge et indexe la liste complète des items TLDB
2. Lance le bot Discord

---

## 📋 Commandes

| Commande | Description |
|---|---|
| `/item <nom>` | Recherche un item par nom avec autocomplétion |

---

## 📁 Structure

```
.
├── bot.py                    # Bot Discord (Python)
├── fetch_items.mjs           # Indexation de la liste d'items (Node.js)
├── fetch_item_details.mjs    # Récupération des détails d'un item (Node.js)
├── Dockerfile
├── docker-compose.yml
├── package.json
├── requirements.txt
└── data/
    └── items.json            # Cache local de la liste d'items (généré)
```

---

## 📡 Source des données

Les données proviennent de [tldb.info](https://tldb.info), la base de données de référence pour Throne & Liberty. L'API interne est utilisée conformément aux [conditions d'utilisation](https://tldb.info/internal-docs) de TLDB (usage personnel uniquement).

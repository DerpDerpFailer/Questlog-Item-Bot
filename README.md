# 🗡️ Questlog Item Bot

A Discord bot to look up Throne & Liberty items directly in your server, powered by [questlog.gg](https://questlog.gg/throne-and-liberty).

## ✨ Features

- **Autocomplete** — start typing an item name and pick from a live list
- **Stats at max level** — stats shown at the item's highest available level (or its fixed level for gear that has one)
- **Unique skill** — skill name and full description
- **EU Auction House prices** — min price and stock in real time
- **Price history** — 7/30 day price trend, average, min/max and stock
- **Loot tracking** — role-gated Main PvP / Main PvE / Alternate Build / Greed sign-up per item, a staff "Roll" button, plus a "Distributed" button to pick a recipient, log the distribution, and clean up the post
- **Loot roll** — one click rolls 1-100 for every sign-up, separately in each category (ties for first are re-rolled), shows each category's winner on the post, closes the sign-ups and pings the winners or every participant; the staff still decides who gets the item. Categories are listed in Main PvP > Main PvE > Alternate Build > Greed order. Optional re-roll with a mandatory reason, kept on the post and in the distribution report
- **Loot wishlist** — each member can wishlist up to a configurable number of items per server, staff can check/export who wants what, and stale entries from members who left are cleaned up automatically every week

## 🚀 Deployment

**Prerequisites:** Docker & Docker Compose + a Discord bot token

**Discord application:** invite the bot with the `bot` and `applications.commands` scopes and the permissions integer `18432`:

| Permission | Why |
|---|---|
| Send Messages | post the distribution reports and the weekly wishlist cleanup reports in the configured log channels |
| Embed Links | those reports are embeds |

No privileged intent is needed (default intents only). Slash command replies and roll announcements go through the interaction itself, and the bot only deletes its own `/item-loot` posts, so no other permission is required. If a log channel is private, also give the bot View Channel there (it is not part of `18432`).

```bash
git clone https://github.com/DerpDerpFailer/Questlog-Item-Bot.git
cd Questlog-Item-Bot
cp .env.example .env
# Edit .env and add your DISCORD_TOKEN
docker compose up -d
```

Under Portainer, skip the `.env` file and set `DISCORD_TOKEN` in the stack's environment variables.
Dependencies are pinned in `requirements.txt`; bump them deliberately and run the test suite below.

The container runs as an unprivileged user (UID/GID 1000) with all capabilities dropped.

### Unraid

Deployed as a Portainer "Repository" stack named `questlog-bot` (GitOps off), with these stack variables:

| Variable | Value |
|---|---|
| `DISCORD_TOKEN` | the bot token |
| `QUESTLOG_DATA_PATH` | `/mnt/user/appdata/questlog-bot/data` (create it and `chown -R 1000:1000` it first) |
| `BOT_REPLICAS` | `1` (`0` keeps the stack deployed but offline, so two instances never share the token) |

The Docker tab icon comes from `icon/questlog-256.png` via jsDelivr. To change it, add a file with a new name (jsDelivr caches `@main` for up to 7 days).

## 📋 Commands

| Command | Description |
|---|---|
| `/item <name>` | Search for an item by name with autocomplete |
| `/price <name> [days]` | Auction House price history (EU) — 7 or 30 days |
| `/item-loot <name>` | Opens a pop-up for an optional note (e.g. "Only for DD"), then posts the same embed as `/item`, plus Main PvP / Main PvE / Alternate Build / Greed sign-up buttons, a "Roll" button and a "Distributed" button (restricted roles, see `/item-setup`) |
| `/item-setup [command_role] [button_role] [log_channel] [reroll] [roll_ping]` | Admin only — configure which role can run `/item-loot`/roll/mark items distributed, which role can click the sign-up buttons, the channel where distribution reports are posted, whether an already-rolled item can be re-rolled (default: no) and who the roll announcement pings (`Nobody` / `Winners only`, the default / `All participants`) |
| `/wishlist [name]` | Add an item to your wishlist, or view/edit your current wishlist if no name is given |
| `/wishlist-setup [limit] [role_staff] [log_channel]` | Admin only — configure the max wishlist size per member (1-25), the staff role for `/wishlist-check`/`/wishlist-export`/`/wishlist-clean`, and/or the channel where weekly auto-cleanup reports are posted |
| `/wishlist-check <name>` | Staff only — list every member who has this item in their wishlist |
| `/wishlist-export` | Staff only — view every member's wishlist on this server, with a button to export as CSV |
| `/wishlist-clean` | Staff only — remove wishlists belonging to members who left the server (also runs automatically every week, optionally reported to `log_channel`) |

## 🎲 Loot workflow

1. **Post** — a member of the command role runs `/item-loot <item>`. A pop-up opens for an optional note (e.g. "Only for DD, healers can't need this item"): Submit with text shows it in a `📝 Note` field above the sign-ups, Submit empty posts without it. Closing the pop-up cancels the post.
2. **Sign up** — members of the button role pick Main PvP, Main PvE, Alternate Build or Greed. The choice is exclusive (picking another category moves you) and clicking your current category again removes you.
3. **Roll** — a member of the command role clicks 🎲 Roll (refused while nobody has signed up):
   - every sign-up gets a 1-100 roll, compared only with the other sign-ups of the same category; a tie for first place is re-rolled between the tied members only, shown as `65 → 12`
   - a `🎲 Roll` field on the post lists the scores and each category's winner (with only one sign-up in a category, that member wins without a roll), in Main PvP > Main PvE > Alternate Build > Greed order
   - the sign-up buttons are greyed out and sign-ups are closed
   - an announcement message pings the winners (default), every participant, or nobody (`/item-setup roll_ping`)
4. **Re-roll (optional)** — only when enabled with `/item-setup reroll:True`; otherwise the Roll button is greyed out after the first roll. Clicking Roll again asks for a reason (3-200 characters), re-rolls every category, and keeps the reason and the previous winners on the post; the announcement includes the reason.
5. **Distribute** — a member of the command role clicks 📦 Distributed and picks the recipient (nobody is pre-selected: the roll only informs the staff's decision) and adds an optional note. The report posted in the log channel includes the roll results and the post's note (as "Loot note"), then the loot post is deleted.

Posts created before the Roll button existed get it on their next sign-up click. Enabling re-rolls does not re-enable the Roll button on posts that were already rolled while re-rolls were disabled.

## 💾 Data, backup & restore

Per-server settings (roles, log channels, roll options, wishlists) live in `/app/data/<guild_id>.json`, on the named Docker volume `questlog-data` (under Portainer it is called `<stack name>_questlog-data`), or in the host folder set by `QUESTLOG_DATA_PATH`. Loot sign-ups and roll results are **not** stored there: they live in the Discord messages themselves.

**Back up** — Portainer → Containers → `questlog-bot` → Console → `/bin/sh`, then save the output:

```bash
for f in /app/data/*.json; do echo "== $f"; cat "$f"; echo; done
```

**Restore** — same Console, paste the saved JSON into a heredoc (the bot re-reads the file on every access, no restart needed):

```bash
cat > /app/data/<guild_id>.json << 'EOF'
<paste the saved JSON here>
EOF
```

With a shell on the Docker host, `docker cp questlog-bot:/app/data ./backup` and `docker cp ./backup/. questlog-bot:/app/data/` do the same; files restored with `docker cp` or copied into a bind-mounted folder must end up owned by `1000:1000`.

**Unraid** — `scripts/restore-questlog-data-unraid.sh` restores from the latest Appdata Backup archive (or `--backup ab_…`, or `--from-dir <folder>`), validates the JSON, stops the bot, snapshots the current data, copies, fixes ownership, checks checksums and restarts the bot. Dry-run unless `--apply`; run it from a machine with SSH access, nothing is installed on the host:

```bash
ssh root@192.168.1.240 'bash -s -- --help' < scripts/restore-questlog-data-unraid.sh
ssh root@192.168.1.240 'bash -s --' < scripts/restore-questlog-data-unraid.sh            # dry-run, latest backup
ssh root@192.168.1.240 'bash -s -- --apply' < scripts/restore-questlog-data-unraid.sh    # restore
```

**Upgrading from the old `./data` bind mount:** the named volume starts empty and Docker does not copy the old directory. Back up first, redeploy, then restore. Until restored, the sign-up buttons refuse clicks and `/item-loot` is blocked; re-running `/item-setup` is the fallback. The old directory is left untouched on the host.

## 🧪 Tests

Run the suite inside the same image the bot ships in (pytest is not part of the production image):

```bash
docker build -t questlog-bot:test .
docker run --rm --entrypoint sh questlog-bot:test -c "pip install -q -r requirements-dev.txt && python -m pytest"
```

## 📡 Data Source

Data from [questlog.gg](https://questlog.gg/throne-and-liberty) (personal use only).

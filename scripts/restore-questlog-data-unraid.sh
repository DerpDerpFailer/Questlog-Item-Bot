#!/usr/bin/env bash
# Restore the bot's per-guild JSON files on Unraid, from an Appdata Backup
# archive (default: the latest one) or from a plain folder.
#
# Dry-run by default: nothing is changed unless --apply is given.
# Runs on the Unraid host as root; from the Mac, without installing anything:
#
#   ssh root@192.168.1.240 'bash -s -- [options]' < scripts/restore-questlog-data-unraid.sh
#
# Options:
#   --backup NAME      Appdata Backup run to restore from (e.g. ab_20261115_040001)
#   --from-dir DIR     restore from a folder of <guild_id>.json files instead
#                      (rollback copy, rescue backup...)
#   --guild ID         restore this guild only
#   --apply            really restore (stop bot, snapshot, copy, chown, verify, start)
#   --data-dir DIR     target folder      (default: /mnt/user/appdata/questlog-bot/data)
#   --container NAME   bot container      (default: questlog-bot)
#   --image IMAGE      image used to validate JSON (default: the container's image)
#   --backup-root DIR  Appdata Backup root (default: /mnt/user/documents/backup-appdata)
#   -h, --help         show this help
set -euo pipefail

DATA_DIR="/mnt/user/appdata/questlog-bot/data"
CONTAINER="questlog-bot"
BACKUP_ROOT="/mnt/user/documents/backup-appdata"
BACKUP=""
FROM_DIR=""
GUILD=""
IMAGE=""
APPLY=0
OWNER="1000:1000"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
usage() {
  cat <<'EOF'
Usage: ssh root@192.168.1.240 'bash -s -- [options]' < scripts/restore-questlog-data-unraid.sh
  --backup NAME | --from-dir DIR   source (default: latest Appdata Backup run)
  --guild ID                       restore this guild only
  --apply                          really restore (default: dry-run)
  --data-dir DIR  --container NAME  --image IMAGE  --backup-root DIR
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --backup)      BACKUP="${2:?--backup needs a value}"; shift 2 ;;
    --from-dir)    FROM_DIR="${2:?--from-dir needs a value}"; shift 2 ;;
    --guild)       GUILD="${2:?--guild needs a value}"; shift 2 ;;
    --data-dir)    DATA_DIR="${2:?--data-dir needs a value}"; shift 2 ;;
    --container)   CONTAINER="${2:?--container needs a value}"; shift 2 ;;
    --image)       IMAGE="${2:?--image needs a value}"; shift 2 ;;
    --backup-root) BACKUP_ROOT="${2:?--backup-root needs a value}"; shift 2 ;;
    --apply)       APPLY=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *)             die "unknown option: $1 (see --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "must run as root on the Unraid host"
[ -z "$BACKUP" ] || [ -z "$FROM_DIR" ] || die "--backup and --from-dir are mutually exclusive"
[ -z "$GUILD" ] || [[ "$GUILD" =~ ^[0-9]+$ ]] || die "--guild must be a numeric guild id"
command -v docker >/dev/null || die "docker not found"

WORK="$(mktemp -d /tmp/questlog-restore.XXXXXX)"
STOPPED=0
SNAPSHOT=""
on_exit() {
  local rc=$?
  rm -rf "$WORK"
  if [ "$rc" -ne 0 ] && [ "$STOPPED" -eq 1 ]; then
    log "FAILED after stopping $CONTAINER: it is left stopped."
    [ -z "$SNAPSHOT" ] || log "Data as it was before the restore: $SNAPSHOT"
    log "Check $DATA_DIR, then: docker start $CONTAINER"
  fi
}
trap on_exit EXIT

# ── Resolve the source folder ────────────────────────────────────────────────
if [ -n "$FROM_DIR" ]; then
  [ -d "$FROM_DIR" ] || die "folder not found: $FROM_DIR"
  SRC="$(cd "$FROM_DIR" && pwd)"
  log "Source: folder $SRC"
else
  if [ -z "$BACKUP" ]; then
    # Latest run that actually contains an archive for this container.
    # ab_YYYYMMDD_HHMMSS sorts chronologically; the glob expands in ascending order.
    for run in "$BACKUP_ROOT"/ab_*; do
      [ -f "$run/$CONTAINER.tar.gz" ] && BACKUP="$(basename "$run")"
    done
    [ -n "$BACKUP" ] || die "no Appdata Backup run under $BACKUP_ROOT contains $CONTAINER.tar.gz"
  fi
  ARCHIVE="$BACKUP_ROOT/$BACKUP/$CONTAINER.tar.gz"
  [ -f "$ARCHIVE" ] || die "archive not found: $ARCHIVE"
  log "Source: archive $ARCHIVE"
  tar -xzf "$ARCHIVE" -C "$WORK"
  # The archive layout depends on the plugin: locate the guild files wherever they are.
  mapfile -t dirs < <(find "$WORK" -type f -regextype posix-extended -regex '.*/[0-9]+\.json' -printf '%h\n' | sort -u)
  [ "${#dirs[@]}" -gt 0 ] || die "no <guild_id>.json file in $ARCHIVE"
  [ "${#dirs[@]}" -eq 1 ] || die "guild files found in several folders of the archive: ${dirs[*]}"
  SRC="${dirs[0]}"
  log "Guild files found in the archive at ${SRC#"$WORK"/}"
fi

# ── Select the files to restore ──────────────────────────────────────────────
if [ -n "$GUILD" ]; then
  [ -f "$SRC/$GUILD.json" ] || die "$GUILD.json is not in the source"
  FILES=("$GUILD.json")
else
  mapfile -t FILES < <(find "$SRC" -maxdepth 1 -type f -regextype posix-extended -regex '.*/[0-9]+\.json' -printf '%f\n' | sort)
  [ "${#FILES[@]}" -gt 0 ] || die "no <guild_id>.json file in $SRC"
fi

# ── Validate the JSON with the bot's own image (Unraid has no python) ───────
if [ -z "$IMAGE" ]; then
  IMAGE="$(docker inspect -f '{{.Config.Image}}' "$CONTAINER" 2>/dev/null)" \
    || die "container $CONTAINER not found; pass --image to validate the JSON"
fi
for f in "${FILES[@]}"; do
  docker run --rm --network none --entrypoint python -v "$SRC/$f:/f.json:ro" "$IMAGE" \
    -c '
import json, sys
try:
    d = json.load(open("/f.json", encoding="utf-8"))
except ValueError as e:
    sys.exit(f"invalid JSON: {e}")
if not isinstance(d, dict):
    sys.exit("a JSON object is expected")' \
    || die "$f is not a valid guild config"
done
log "JSON valid: ${#FILES[@]} file(s)"

# ── Plan ─────────────────────────────────────────────────────────────────────
md5() { md5sum "$1" | cut -d' ' -f1; }
changes=0
for f in "${FILES[@]}"; do
  src_sum="$(md5 "$SRC/$f")"
  if [ ! -f "$DATA_DIR/$f" ]; then
    log "  NEW      $f ($(stat -c %s "$SRC/$f") bytes)"; changes=$((changes + 1))
  elif [ "$src_sum" = "$(md5 "$DATA_DIR/$f")" ]; then
    log "  SAME     $f"
  else
    log "  REPLACE  $f ($(stat -c %s "$DATA_DIR/$f") -> $(stat -c %s "$SRC/$f") bytes)"; changes=$((changes + 1))
  fi
done
if [ -z "$GUILD" ] && [ -d "$DATA_DIR" ]; then
  for f in $(find "$DATA_DIR" -maxdepth 1 -type f -name '*.json' -printf '%f\n' | sort); do
    [ -f "$SRC/$f" ] || log "  KEEP     $f (not in the source, left untouched)"
  done
fi

if [ "$APPLY" -eq 0 ]; then
  log "Dry-run: nothing changed ($changes file(s) would change). Re-run with --apply to restore."
  exit 0
fi
[ "$changes" -gt 0 ] || { log "Nothing to restore: target already matches the source."; exit 0; }

# ── Apply ────────────────────────────────────────────────────────────────────
was_running="$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)"
if [ "$was_running" = "true" ]; then
  log "Stopping $CONTAINER"
  docker stop "$CONTAINER" >/dev/null
  STOPPED=1
fi

mkdir -p "$DATA_DIR"
SNAPSHOT="$DATA_DIR.before-restore-$(date '+%Y%m%d-%H%M%S')"
cp -a "$DATA_DIR" "$SNAPSHOT"
log "Current data saved to $SNAPSHOT"

for f in "${FILES[@]}"; do
  cp "$SRC/$f" "$DATA_DIR/$f.restore-tmp"
  mv -f "$DATA_DIR/$f.restore-tmp" "$DATA_DIR/$f"
  [ "$(md5 "$SRC/$f")" = "$(md5 "$DATA_DIR/$f")" ] || die "checksum mismatch after copying $f"
done
chown -R "$OWNER" "$DATA_DIR"
chmod 755 "$DATA_DIR"
chmod 644 "${FILES[@]/#/$DATA_DIR/}"
log "Restored ${#FILES[@]} file(s), owner $OWNER, checksums OK"

if [ "$was_running" = "true" ]; then
  log "Starting $CONTAINER"
  docker start "$CONTAINER" >/dev/null
  STOPPED=0
  sleep 5
  docker logs --tail 20 "$CONTAINER" 2>&1 | sed 's/\x1b\[[0-9;]*m//g' >&2
else
  log "$CONTAINER was not running: left stopped"
fi
log "Done. Rollback: same command with --from-dir $SNAPSHOT --apply"

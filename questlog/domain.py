"""Pure logic: no Discord, no network, no files. Everything here is testable by calling it."""


import asyncio
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable


# ── Sign-up lists for /item-loot: the list lives in the embed field, this is the pure logic around it.

LOOT_FIELD_NAME = "🎯 Loot Interest"
LOOT_CATEGORIES = [
    ("pvp", "Main PvP", "loot_pvp"),
    ("pve", "Main PvE", "loot_pve"),
    ("alt", "Alternate Build", "loot_alt"),
    ("greed", "Greed", "loot_greed"),
]
LOOT_STATE_CACHE_SIZE = 500
# Optional staff note on a loot post (e.g. "Only for DD"), kept under the 1024-char embed field limit.
LOOT_NOTE_FIELD_NAME = "📝 Note"
LOOT_NOTE_MAX_CHARS = 1000


def format_loot_field(state: dict[str, list[int]]) -> str:
    lines = []
    for key, label, _ in LOOT_CATEGORIES:
        ids = state.get(key, [])
        value = " ".join(f"<@{i}>" for i in ids) if ids else "—"
        lines.append(f"**{label}:** {value}")
    return "\n".join(lines)


def parse_loot_field(value: str) -> dict[str, list[int]]:
    state = {key: [] for key, _, _ in LOOT_CATEGORIES}
    for line in value.split("\n"):
        for key, label, _ in LOOT_CATEGORIES:
            if line.startswith(f"**{label}:**"):
                state[key] = [int(i) for i in re.findall(r"<@!?(\d+)>", line)]
    return state


def toggle_loot_signup(state: dict[str, list[int]], user_id: int, category_key: str) -> dict[str, list[int]]:
    """Exclusive choice with toggle-off on re-click. Returns a new dict, never mutates `state`."""
    already_in = user_id in state[category_key]
    new_state = {key: [i for i in ids if i != user_id] for key, ids in state.items()}
    if not already_in:
        new_state[category_key].append(user_id)
    return new_state


# ── Roll for /item-loot: one 1-100 draw per category, among that category's sign-ups only.
# The staff still decides who gets the item; category order (LOOT_CATEGORIES) is only the display order.

LOOT_ROLL_FIELD_NAME = "🎲 Roll"
LOOT_ROLL_MAX = 100
EMBED_FIELD_MAX_CHARS = 1024
ROLL_REASON_MAX_CHARS = 200

# Per category, every sign-up in sign-up order with its rolls: (first roll, tie-break rolls...).
RollResult = dict[str, list[tuple[int, tuple[int, ...]]]]


def roll_loot(state: dict[str, list[int]], randint: Callable[[int, int], int]) -> RollResult:
    """Roll for every non-empty category. Ties for first place are re-rolled among the tied
    members only, until one is ahead; the extra rolls are appended to their tuples, so the
    winner is always the member with the greatest tuple."""
    result: RollResult = {}
    for key, _, _ in LOOT_CATEGORIES:
        user_ids = state.get(key, [])
        if not user_ids:
            continue
        rolls = {user_id: [randint(1, LOOT_ROLL_MAX)] for user_id in user_ids}
        if len(user_ids) > 1:
            while True:
                best = max(tuple(r) for r in rolls.values())
                tied = [user_id for user_id, r in rolls.items() if tuple(r) == best]
                if len(tied) == 1:
                    break
                for user_id in tied:
                    rolls[user_id].append(randint(1, LOOT_ROLL_MAX))
        result[key] = [(user_id, tuple(rolls[user_id])) for user_id in user_ids]
    return result


def roll_winner(entries: list[tuple[int, tuple[int, ...]]]) -> int:
    return max(entries, key=lambda entry: entry[1])[0]


def _single_line(text: str) -> str:
    return " ".join(text.split())


def _winners_summary(winners: dict[str, int]) -> str:
    return " · ".join(f"{label} 🏆 <@{winners[key]}>" for key, label, _ in LOOT_CATEGORIES if key in winners)


def format_roll_field(
    result: RollResult,
    rolled_by: int,
    rolled_at: int,
    reason: str | None = None,
    previous: str | None = None,
) -> str:
    """The roll field's text. `reason` marks a re-roll; `previous` is the field text of the
    roll it replaces (only its winners and author are kept, so the field stays short)."""

    def body(compact: bool) -> list[str]:
        lines = []
        for key, label, _ in LOOT_CATEGORIES:
            entries = result.get(key)
            if not entries:
                continue
            winner = roll_winner(entries)
            if len(entries) == 1:
                lines.append(f"**{label}:** 🏆 <@{winner}> (only one sign-up)")
            elif compact:
                score = " → ".join(str(r) for r in dict(entries)[winner])
                lines.append(f"**{label}:** 🏆 <@{winner}> ({score}) + {len(entries) - 1} other(s)")
            else:
                scores = " · ".join(f"<@{u}> ({' → '.join(str(r) for r in rolls)})" for u, rolls in entries)
                lines.append(f"**{label}:** {scores} → 🏆 <@{winner}>")
        return lines

    if reason is None:
        footer = [f"Rolled by <@{rolled_by}> <t:{rolled_at}:f>"]
    else:
        footer = [f"Re-rolled by <@{rolled_by}> <t:{rolled_at}:f> — reason: {_single_line(reason)}"]
    if previous:
        old = parse_roll_field(previous)
        footer.append(f"Previous: {_winners_summary(old['winners'])} (by <@{old['rolled_by']}> <t:{old['rolled_at']}:f>)")

    value = "\n".join(body(compact=False) + footer)
    if len(value) > EMBED_FIELD_MAX_CHARS:
        value = "\n".join(body(compact=True) + footer)
    return value[:EMBED_FIELD_MAX_CHARS]


_ROLLED_BY_RE = re.compile(r"^(?:Re-r|R)olled by <@!?(\d+)> <t:(\d+):f>")


def parse_roll_field(value: str) -> dict:
    """Winners per category, plus who rolled and when — enough to summarize this roll as the
    "previous" one after a re-roll."""
    winners: dict[str, int] = {}
    rolled_by = rolled_at = None
    for line in value.split("\n"):
        for key, label, _ in LOOT_CATEGORIES:
            if line.startswith(f"**{label}:**"):
                match = re.search(r"🏆 <@!?(\d+)>", line)
                if match:
                    winners[key] = int(match.group(1))
        match = _ROLLED_BY_RE.match(line)
        if match:
            rolled_by, rolled_at = int(match.group(1)), int(match.group(2))
    return {"winners": winners, "rolled_by": rolled_by, "rolled_at": rolled_at}


def roll_ping_targets(result: RollResult, mode: str) -> list[int]:
    """Who the roll announcement pings: "winners", "all" sign-ups, or nobody ("none")."""
    if mode == "all":
        return [user_id for entries in result.values() for user_id, _ in entries]
    if mode == "winners":
        return [roll_winner(entries) for entries in result.values()]
    return []


class LootEntry:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.state: dict[str, list[int]] | None = None
        # Roll field text once this message was rolled; None until then (or unknown after a restart).
        self.roll: str | None = None


class LootStateStore:
    """Authoritative sign-up state per loot message, plus one lock per message.

    A click's payload carries the message as it was when the click happened, so two
    near-simultaneous clicks start from the same stale snapshot and the second edit erases
    the first. Serializing on this state fixes that without fetching the message (which
    would need channel permissions the interaction flow doesn't). After a restart the cache
    is empty and the first click falls back to the state parsed from the embed."""

    def __init__(self, max_size: int = LOOT_STATE_CACHE_SIZE) -> None:
        self._max_size = max_size
        self._entries: OrderedDict[int, LootEntry] = OrderedDict()

    def entry(self, message_id: int) -> LootEntry:
        entry = self._entries.get(message_id)
        if entry is None:
            entry = self._entries[message_id] = LootEntry()
        self._entries.move_to_end(message_id)
        while len(self._entries) > self._max_size:
            oldest_id, oldest = next(iter(self._entries.items()))
            if oldest.lock.locked():
                break
            del self._entries[oldest_id]
        return entry


# ── Auction house price figures and formatting.

def is_listed(ah: dict | str | None) -> bool:
    return isinstance(ah, dict) and ah.get("inStock", 0) > 0 and ah.get("minPrice") is not None


def format_current_price(ah: dict | str | None) -> str:
    if is_listed(ah):
        price_fmt = f"{ah['minPrice']:,}".replace(",", " ")
        return f"{price_fmt} ◈ (×{ah['inStock']} in stock)"
    if isinstance(ah, dict):
        return "Not listed"
    return "Unavailable"


def compute_price_stats(points: list[dict], current_price: int | None) -> dict:
    """The figures /price has always shown, from hourly points (hours with offers only):
    Min/Max are the lowest and highest floor price seen, Avg Price the mean of the hourly
    averages, and the change compares the current floor price with the oldest hour's average."""
    oldest_price = points[0]["avg"]
    change_pct = None
    if current_price is not None and oldest_price:
        change_pct = round((current_price - oldest_price) / oldest_price * 100, 1)
    return {
        "min_price": min(p["min"] for p in points),
        "max_price": max(p["max"] for p in points),
        "avg_price": round(sum(p["avg"] for p in points) / len(points)),
        "avg_stock": round(sum(p["stock"] for p in points) / len(points)),
        "change_pct": change_pct,
    }


def format_change(change_pct: float | None) -> str:
    if change_pct is None:
        return "—"
    if change_pct > 0:
        return f"📈 +{change_pct}%"
    if change_pct < 0:
        return f"📉 {change_pct}%"
    return "➡️ 0%"


# ── Caching.

class TTLCache:
    """Small cache with per-entry expiry and a bounded size (oldest-used entry evicted first).
    search_items runs in executor threads, so every access goes through the lock."""

    def __init__(self, ttl: float, max_size: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._max_size = max_size
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()

    def get(self, key: str) -> list[dict] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if self._clock() >= expires_at:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def set(self, key: str, value: list[dict]) -> None:
        with self._lock:
            self._entries[key] = (self._clock() + self._ttl, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_size:
                self._entries.popitem(last=False)

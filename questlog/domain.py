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


class LootEntry:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.state: dict[str, list[int]] | None = None


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

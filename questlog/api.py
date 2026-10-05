import json
import logging
import time

import requests

from questlog.domain import TTLCache

log = logging.getLogger(__name__)

BASE_URL = "https://questlog.gg/throne-and-liberty/api/trpc"
API_TIMEOUT = 8          # seconds before questlog times out
SEARCH_CACHE_TTL = 60    # seconds; item names barely change, so this only needs to absorb keystroke bursts
SEARCH_CACHE_SIZE = 512


def api_get(endpoint: str, input_data: dict) -> dict | None:
    try:
        r = requests.get(
            f"{BASE_URL}/{endpoint}",
            params={"input": json.dumps(input_data, separators=(",", ":"))},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=API_TIMEOUT
        )
        r.raise_for_status()
        return r.json()["result"]["data"]
    except requests.exceptions.Timeout:
        log.warning(f"API timeout [{endpoint}]")
        return "timeout"
    except Exception as e:
        log.error(f"API error [{endpoint}]: {e}")
        return None


_search_cache = TTLCache(SEARCH_CACHE_TTL, SEARCH_CACHE_SIZE)


def search_items(query: str) -> list[dict]:
    """Autocomplete fires on every keystroke and several members often type the same names,
    so successful lookups are cached. Failures are never cached: a transient API error must
    not turn into a minute of empty suggestions. The key ignores case and extra spaces, which
    the questlog search was checked not to care about."""
    key = " ".join(query.split()).casefold()
    cached = _search_cache.get(key)
    if cached is not None:
        return [dict(item) for item in cached]

    data = api_get("database.getItems", {
        "language": "en", "page": 1,
        "searchTerm": query, "mainCategory": "", "subCategory": ""
    })
    if not data or data == "timeout":
        return []
    results = [
        {"id": item["id"], "name": item["name"]}
        for item in data.get("pageData", [])
        if not item.get("isDisabled")
    ][:25]
    _search_cache.set(key, results)
    return [dict(item) for item in results]


def fetch_item(item_id: str) -> dict | str | None:
    return api_get("database.getItem", {"id": item_id, "language": "en"})


AH_REGION = "eu-f"


def fetch_market(auction_house_id: int | None) -> dict | str | None:
    """Current price of an item in AH_REGION, as {"minPrice": int | None, "inStock": int}.
    questlog.gg indexes the auction house by the item's auctionHouseId (not its id), and
    answers 400 for a null one, so items that can't be traded never reach the API.
    Returns None / "timeout" like api_get when the lookup failed."""
    if auction_house_id is None:
        return {"minPrice": None, "inStock": 0}
    data = api_get("auctionHouse.getItemMarket", {
        "auctionHouseId": auction_house_id, "potentialAbilityId": None
    })
    if data is None or data == "timeout":
        return data
    current = (data.get("current") or {}).get(AH_REGION) or {}
    return {"minPrice": current.get("minPrice"), "inStock": current.get("inStock") or 0}


def fetch_market_for_item(item_id: str) -> dict | str | None:
    """Blocking helper for callers that only know the item id: resolve its auctionHouseId, then its price."""
    item = fetch_item(item_id)
    if not isinstance(item, dict):
        return item
    return fetch_market(item.get("auctionHouseId"))


def fetch_price_history(auction_house_id: int, days: int, now_ms: float | None = None) -> list[dict] | str | None:
    """Price points for the last `days` days in AH_REGION, oldest first. Only periods that had
    offers are present. Each raw point is [time_ms, min, max, avg, last, in_stock], where the
    prices are the floor price sampled during that period.

    The hourly ranges (7d, 30d, 90d) are all capped at 168 points, i.e. the last 7 days, so
    anything longer is read from the daily "all" series and cut to the requested window."""
    range_name = "7d" if days <= 7 else "all"
    data = api_get("auctionHouse.getItemHistory", {
        "auctionHouseId": auction_house_id, "potentialAbilityId": None, "range": range_name
    })
    if data is None or data == "timeout":
        return data
    raw = (data.get("series") or {}).get(AH_REGION) or []
    points = [
        {"time": p[0], "min": p[1], "max": p[2], "avg": p[3], "last": p[4], "stock": p[5]}
        for p in raw if p and p[1] is not None
    ]
    if range_name == "all":
        cutoff = (time.time() * 1000 if now_ms is None else now_ms) - days * 86_400_000
        points = [point for point in points if point["time"] >= cutoff]
    return sorted(points, key=lambda point: point["time"])

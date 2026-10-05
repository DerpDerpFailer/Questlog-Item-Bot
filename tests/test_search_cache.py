import asyncio
import threading

import pytest

import bot
from questlog import api as questlog_api
from questlog import domain


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(questlog_api, "_search_cache", domain.TTLCache(60, 512))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeApi:
    """Stands in for api.api_get: records the search terms it receives and replays the
    programmed responses in order (the last one repeats)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.terms: list[str] = []

    def __call__(self, endpoint, input_data):
        self.terms.append(input_data["searchTerm"])
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def page(*items):
    return {"pageData": list(items)}


def item(item_id: str, name: str, disabled: bool = False) -> dict:
    return {"id": item_id, "name": name, "isDisabled": disabled}


def test_the_same_search_is_served_from_the_cache(monkeypatch):
    api = FakeApi(page(item("a", "Alpha")))
    monkeypatch.setattr(questlog_api, "api_get", api)

    assert questlog_api.search_items("alp") == [{"id": "a", "name": "Alpha"}]
    assert questlog_api.search_items("alp") == [{"id": "a", "name": "Alpha"}]
    assert api.terms == ["alp"]


def test_the_cache_ignores_case_and_extra_spaces(monkeypatch):
    api = FakeApi(page(item("a", "Ascended Bow")))
    monkeypatch.setattr(questlog_api, "api_get", api)

    questlog_api.search_items("Ascended Bow")
    questlog_api.search_items("  ascended   bow ")

    assert api.terms == ["Ascended Bow"]


def test_different_searches_are_not_mixed_up(monkeypatch):
    api = FakeApi(page(item("a", "Alpha")), page(item("b", "Beta")))
    monkeypatch.setattr(questlog_api, "api_get", api)

    assert questlog_api.search_items("alp") == [{"id": "a", "name": "Alpha"}]
    assert questlog_api.search_items("bet") == [{"id": "b", "name": "Beta"}]
    assert api.terms == ["alp", "bet"]


def test_failed_lookups_are_never_cached(monkeypatch):
    api = FakeApi(None, "timeout", page(item("a", "Alpha")))
    monkeypatch.setattr(questlog_api, "api_get", api)

    assert questlog_api.search_items("alp") == []
    assert questlog_api.search_items("alp") == []
    assert questlog_api.search_items("alp") == [{"id": "a", "name": "Alpha"}]
    assert questlog_api.search_items("alp") == [{"id": "a", "name": "Alpha"}]
    assert api.terms == ["alp", "alp", "alp"]


def test_an_empty_but_successful_result_is_cached(monkeypatch):
    api = FakeApi(page())
    monkeypatch.setattr(questlog_api, "api_get", api)

    assert questlog_api.search_items("zzz") == []
    assert questlog_api.search_items("zzz") == []
    assert api.terms == ["zzz"]


def test_disabled_items_are_filtered_and_results_are_capped_at_25(monkeypatch):
    items = [item(f"i{n}", f"Item {n}", disabled=(n % 5 == 0)) for n in range(40)]
    monkeypatch.setattr(questlog_api, "api_get", FakeApi(page(*items)))

    results = questlog_api.search_items("item")

    assert len(results) == 25
    assert all(int(r["id"][1:]) % 5 != 0 for r in results)


def test_callers_cannot_corrupt_the_cached_results(monkeypatch):
    monkeypatch.setattr(questlog_api, "api_get", FakeApi(page(item("a", "Alpha"))))

    first = questlog_api.search_items("alp")
    first[0]["name"] = "HACKED"
    first.append({"id": "x", "name": "Extra"})

    assert questlog_api.search_items("alp") == [{"id": "a", "name": "Alpha"}]


def test_a_cached_search_expires_after_the_ttl(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(questlog_api, "_search_cache", domain.TTLCache(60, 512, clock=clock))
    api = FakeApi(page(item("a", "Alpha")))
    monkeypatch.setattr(questlog_api, "api_get", api)

    questlog_api.search_items("alp")
    clock.now = 59
    questlog_api.search_items("alp")
    assert api.terms == ["alp"]

    clock.now = 61
    questlog_api.search_items("alp")
    assert api.terms == ["alp", "alp"]


def test_ttl_cache_entries_expire_exactly_at_the_ttl():
    clock = Clock()
    cache = domain.TTLCache(10, 5, clock=clock)
    cache.set("k", [{"id": "a"}])

    clock.now = 9.9
    assert cache.get("k") == [{"id": "a"}]
    clock.now = 10
    assert cache.get("k") is None


def test_ttl_cache_evicts_the_least_recently_used_entry_first():
    cache = domain.TTLCache(60, 2, clock=Clock())
    cache.set("a", [1])
    cache.set("b", [2])
    cache.get("a")
    cache.set("c", [3])

    assert cache.get("b") is None
    assert cache.get("a") == [1]
    assert cache.get("c") == [3]


def test_ttl_cache_survives_concurrent_access_from_threads():
    cache = domain.TTLCache(60, 16)
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(seed: int) -> None:
        try:
            barrier.wait()
            for n in range(500):
                key = f"k{(seed * 7 + n) % 40}"
                cache.set(key, [n])
                cache.get(key)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(seed,)) for seed in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(cache._entries) <= 16


def test_all_autocompletes_share_the_cache_and_still_ignore_short_input(monkeypatch):
    api = FakeApi(page(item("a", "Alpha")))
    monkeypatch.setattr(questlog_api, "api_get", api)

    async def run():
        first = await bot.item_autocomplete(None, "alp")
        second = await bot.price_autocomplete(None, "ALP")
        third = await bot.item_loot_autocomplete(None, "alp ")
        short = await bot.wishlist_autocomplete(None, "a")
        return first, second, third, short

    first, second, third, short = asyncio.run(run())

    assert [(c.name, c.value) for c in first] == [("Alpha", "a")]
    assert [(c.name, c.value) for c in second] == [("Alpha", "a")]
    assert [(c.name, c.value) for c in third] == [("Alpha", "a")]
    assert short == []
    assert api.terms == ["alp"]

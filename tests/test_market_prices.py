import asyncio
import pathlib
import types

import discord
import pytest

import bot

# Shapes captured from the live questlog.gg API (auctionHouse.getItemMarket / getItemHistory).
MARKET_LISTED = {
    "current": {
        "as-f": {"minPrice": None, "inStock": None, "updatedAt": 1790998241614},
        "eu-f": {"minPrice": 12950, "inStock": 2, "updatedAt": 1791085923387},
        "na-f": {"minPrice": 20000, "inStock": 2, "updatedAt": 1791083160653},
    },
    "stats": {"eu-f": {"median7d": 12950}},
    "abilities": [],
    "events": [],
}
MARKET_ONLY_LISTED_IN_NA = {
    "current": {
        "as-f": {"minPrice": None, "inStock": None, "updatedAt": 1},
        "eu-f": {"minPrice": None, "inStock": None, "updatedAt": 2},
        "na-f": {"minPrice": 20000, "inStock": 2, "updatedAt": 3},
    }
}
# [time_ms, min, max, avg, last, in_stock]: floor price sampled during the hour
RAW_SERIES = [
    [1790542800000, 999, 1199, 1115, 999, 11],
    [1790539200000, 1198, 1199, 1198, 1199, 10],
    None,
    [1790600000000, None, None, None, None, None],
    [1791129600000, 1298, 1298, 1298, 1298, 9],
]
ITEM = {
    "id": "sword2h_aa_t2_polymorph_001", "name": "Tevent's Warblade", "grade": 41,
    "icon": "/assets/Game/Image/Icon/X.X", "subCategory": "sword2h", "auctionHouseId": 10042047,
}


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "DATA_DIR", str(tmp_path))


class FakeApi:
    """Stands in for bot.api_get, keyed by endpoint; records every call."""

    def __init__(self, **responses):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, endpoint, input_data):
        self.calls.append((endpoint, input_data))
        return self.responses[endpoint]


class FakeFollowup:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, content=None, *, embed=None, **kwargs):
        self.sent.append({"content": content, "embed": embed})


class FakeResponse:
    def __init__(self):
        self.sent: list[str] = []

    async def defer(self, **kwargs):
        pass

    async def send_message(self, content=None, **kwargs):
        self.sent.append(content)

    async def edit_message(self, **kwargs):
        pass


class FakeInteraction:
    def __init__(self):
        self.guild_id = 1
        self.user = types.SimpleNamespace(id=7, name="member", mention="<@7>")
        self.response = FakeResponse()
        self.followup = FakeFollowup()


def never_called(name: str):
    def _fail(*args, **kwargs):
        raise AssertionError(f"{name} must not be called here")
    return _fail


def field(embed: discord.Embed, name: str) -> str:
    return next(f.value for f in embed.fields if name in f.name)


def test_the_removed_get_auction_item_endpoint_is_no_longer_referenced():
    assert "getAuctionItem" not in pathlib.Path(bot.__file__).read_text()


def test_fetch_market_asks_for_the_item_by_auction_house_id_and_reads_the_eu_floor(monkeypatch):
    api = FakeApi(**{"auctionHouse.getItemMarket": MARKET_LISTED})
    monkeypatch.setattr(bot, "api_get", api)

    assert bot.fetch_market(1769664410) == {"minPrice": 12950, "inStock": 2}
    assert api.calls == [
        ("auctionHouse.getItemMarket", {"auctionHouseId": 1769664410, "potentialAbilityId": None})
    ]


def test_a_listing_in_another_region_does_not_count(monkeypatch):
    monkeypatch.setattr(bot, "api_get", FakeApi(**{"auctionHouse.getItemMarket": MARKET_ONLY_LISTED_IN_NA}))
    assert bot.fetch_market(1) == {"minPrice": None, "inStock": 0}


def test_an_item_the_api_has_no_market_for_is_simply_not_listed(monkeypatch):
    monkeypatch.setattr(bot, "api_get", FakeApi(**{"auctionHouse.getItemMarket": {}}))
    assert bot.fetch_market(1741582256) == {"minPrice": None, "inStock": 0}


def test_an_item_without_an_auction_house_id_never_reaches_the_api(monkeypatch):
    monkeypatch.setattr(bot, "api_get", never_called("api_get"))
    assert bot.fetch_market(None) == {"minPrice": None, "inStock": 0}


@pytest.mark.parametrize("failure", [None, "timeout"])
def test_market_lookup_failures_are_passed_through(monkeypatch, failure):
    monkeypatch.setattr(bot, "api_get", FakeApi(**{"auctionHouse.getItemMarket": failure}))
    assert bot.fetch_market(1) == failure


def test_fetch_market_for_item_resolves_the_auction_house_id_first(monkeypatch):
    monkeypatch.setattr(bot, "fetch_item", lambda item_id: {"id": item_id, "auctionHouseId": 42})
    api = FakeApi(**{"auctionHouse.getItemMarket": MARKET_LISTED})
    monkeypatch.setattr(bot, "api_get", api)

    assert bot.fetch_market_for_item("some_item") == {"minPrice": 12950, "inStock": 2}
    assert api.calls[0][1]["auctionHouseId"] == 42


@pytest.mark.parametrize("failure", [None, "timeout"])
def test_fetch_market_for_item_passes_item_lookup_failures_through(monkeypatch, failure):
    monkeypatch.setattr(bot, "fetch_item", lambda item_id: failure)
    monkeypatch.setattr(bot, "api_get", never_called("api_get"))
    assert bot.fetch_market_for_item("some_item") == failure


@pytest.mark.parametrize("days, expected_range", [(7, "7d"), (30, "all")])
def test_fetch_price_history_reads_hourly_data_for_a_week_and_the_daily_series_beyond(monkeypatch, days, expected_range):
    api = FakeApi(**{"auctionHouse.getItemHistory": {"bucket": "hour", "series": {"eu-f": []}}})
    monkeypatch.setattr(bot, "api_get", api)

    assert bot.fetch_price_history(10042047, days) == []
    assert api.calls == [(
        "auctionHouse.getItemHistory",
        {"auctionHouseId": 10042047, "potentialAbilityId": None, "range": expected_range},
    )]


def test_the_daily_series_is_cut_to_the_requested_window(monkeypatch):
    day = 86_400_000
    now = 100 * day
    series = [[now - 40 * day, 5, 5, 5, 5, 1], [now - 29 * day, 6, 6, 6, 6, 1], [now - 2 * day, 7, 7, 7, 7, 1]]
    monkeypatch.setattr(
        bot, "api_get", FakeApi(**{"auctionHouse.getItemHistory": {"bucket": "day", "series": {"eu-f": series}}})
    )

    points = bot.fetch_price_history(1, 30, now_ms=now)

    assert [p["min"] for p in points] == [6, 7]


def test_history_points_are_decoded_filtered_and_sorted_oldest_first(monkeypatch):
    data = {"bucket": "hour", "series": {"eu-f": RAW_SERIES, "na-f": [[1, 5, 5, 5, 5, 5]]}}
    monkeypatch.setattr(bot, "api_get", FakeApi(**{"auctionHouse.getItemHistory": data}))

    points = bot.fetch_price_history(1, 7)

    assert [p["time"] for p in points] == [1790539200000, 1790542800000, 1791129600000]
    assert points[1] == {"time": 1790542800000, "min": 999, "max": 1199, "avg": 1115, "last": 999, "stock": 11}


@pytest.mark.parametrize("failure", [None, "timeout"])
def test_history_lookup_failures_are_passed_through(monkeypatch, failure):
    monkeypatch.setattr(bot, "api_get", FakeApi(**{"auctionHouse.getItemHistory": failure}))
    assert bot.fetch_price_history(1, 7) == failure


POINTS = [
    {"time": 1, "min": 1198, "max": 1199, "avg": 1198, "last": 1199, "stock": 10},
    {"time": 2, "min": 999, "max": 1199, "avg": 1115, "last": 999, "stock": 11},
    {"time": 3, "min": 998, "max": 999, "avg": 998, "last": 998, "stock": 11},
    {"time": 4, "min": 1298, "max": 1298, "avg": 1298, "last": 1298, "stock": 9},
]


def test_price_stats_keep_the_meaning_of_the_original_figures():
    stats = bot.compute_price_stats(POINTS, current_price=1298)

    assert stats == {
        "min_price": 998,
        "max_price": 1298,
        "avg_price": 1152,
        "avg_stock": 10,
        "change_pct": 8.3,
    }


def test_the_change_is_unknown_without_a_current_price_or_a_usable_reference():
    assert bot.compute_price_stats(POINTS, current_price=None)["change_pct"] is None
    zero_start = [dict(POINTS[0], avg=0)] + POINTS[1:]
    assert bot.compute_price_stats(zero_start, current_price=1298)["change_pct"] is None


def test_change_formatting():
    assert bot.format_change(None) == "—"
    assert bot.format_change(8.3) == "📈 +8.3%"
    assert bot.format_change(-4.5) == "📉 -4.5%"
    assert bot.format_change(0.0) == "➡️ 0%"


@pytest.mark.parametrize(
    "ah, expected_price, expected_embed",
    [
        ({"minPrice": 12950, "inStock": 2}, "12 950 ◈ (×2 in stock)", "🏪 **12 950 ◈** ×2"),
        ({"minPrice": None, "inStock": 0}, "Not listed", "🏪 *Not listed*"),
        (None, "Unavailable", "🏪 *Unavailable*"),
        ("timeout", "Unavailable", "🏪 *Unavailable*"),
    ],
)
def test_prices_are_formatted_and_a_failed_lookup_is_visible(ah, expected_price, expected_embed):
    assert bot.format_current_price(ah) == expected_price
    assert expected_embed in bot.build_embed(ITEM, ah).description


def test_item_asks_for_the_price_only_after_the_item_and_with_its_auction_house_id(monkeypatch):
    prices: list = []
    monkeypatch.setattr(bot, "fetch_item", lambda item_id: dict(ITEM))
    monkeypatch.setattr(bot, "fetch_market", lambda ahid: prices.append(ahid) or {"minPrice": 1298, "inStock": 9})
    interaction = FakeInteraction()

    asyncio.run(bot.item_command.callback(interaction, ITEM["id"]))

    assert prices == [10042047]
    assert "🏪 **1 298 ◈** ×9" in interaction.followup.sent[0]["embed"].description


@pytest.mark.parametrize("failure", [None, "timeout"])
def test_item_does_not_ask_for_a_price_when_the_item_lookup_failed(monkeypatch, failure):
    monkeypatch.setattr(bot, "fetch_item", lambda item_id: failure)
    monkeypatch.setattr(bot, "fetch_market", never_called("fetch_market"))
    interaction = FakeInteraction()

    asyncio.run(bot.item_command.callback(interaction, "whatever"))

    assert interaction.followup.sent[0]["embed"] is None


def test_item_loot_shows_the_price_too(monkeypatch):
    bot.save_guild_config(1, command_role_id=10, button_role_id=11)
    monkeypatch.setattr(bot, "has_role", lambda member, role_id: True)
    monkeypatch.setattr(bot, "fetch_item", lambda item_id: dict(ITEM))
    monkeypatch.setattr(bot, "fetch_market", lambda ahid: {"minPrice": 1298, "inStock": 9})
    interaction = FakeInteraction()

    asyncio.run(bot.item_loot_command.callback(interaction, ITEM["id"]))

    assert "🏪 **1 298 ◈** ×9" in interaction.followup.sent[0]["embed"].description


def run_price(monkeypatch, *, item=ITEM, market=None, history=None, days=7):
    monkeypatch.setattr(bot, "fetch_item", lambda item_id: item if item is None or item == "timeout" else dict(item))
    monkeypatch.setattr(bot, "fetch_market", lambda ahid: market)
    monkeypatch.setattr(bot, "fetch_price_history", lambda ahid, d: history)
    interaction = FakeInteraction()
    asyncio.run(bot.price_command.callback(interaction, ITEM["id"], days))
    return interaction.followup.sent[0]


def test_price_keeps_the_same_embed_layout_with_the_new_data(monkeypatch):
    sent = run_price(monkeypatch, market={"minPrice": 1298, "inStock": 9}, history=POINTS)
    embed = sent["embed"]

    assert embed.title == "Tevent's Warblade"
    assert embed.description == "🏪 **Auction House — EU** · Last 7 days"
    assert [f.name for f in embed.fields] == [
        "💰 Current Price", "📦 In Stock", "📊 Change", "⬇️ Min", "⬆️ Max", "〰️ Avg Price", "📦 Avg Stock",
    ]
    assert field(embed, "Current Price") == "**1 298 ◈**"
    assert field(embed, "In Stock") == "**9**"
    assert field(embed, "Change") == "**📈 +8.3%**"
    assert field(embed, "Min") == "998 ◈"
    assert field(embed, "Max") == "1 298 ◈"
    assert field(embed, "Avg Price") == "1 152 ◈"
    assert field(embed, "Avg Stock") == "10"


def test_price_of_an_item_nobody_sells_right_now_still_shows_its_history(monkeypatch):
    sent = run_price(monkeypatch, market={"minPrice": None, "inStock": 0}, history=POINTS, days=30)

    assert field(sent["embed"], "Current Price") == "**Not listed**"
    assert field(sent["embed"], "Change") == "**—**"
    assert sent["embed"].description.endswith("Last 30 days")


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"item": None}, "Item not found"),
        ({"item": "timeout"}, "taking too long"),
        ({"item": dict(ITEM, auctionHouseId=None)}, "can't be sold"),
        ({"market": "timeout", "history": []}, "taking too long"),
        ({"market": {"minPrice": None, "inStock": 0}, "history": "timeout"}, "taking too long"),
        ({"market": None, "history": []}, "unavailable right now"),
        ({"market": {"minPrice": 1, "inStock": 1}, "history": None}, "unavailable right now"),
        ({"market": {"minPrice": None, "inStock": 0}, "history": []}, "No price history"),
    ],
)
def test_price_explains_each_failure(monkeypatch, kwargs, expected):
    sent = run_price(monkeypatch, **kwargs)

    assert sent["embed"] is None
    assert expected in sent["content"]


class FakeMessage:
    id = 555

    def __init__(self):
        self.deleted = False

    async def delete(self):
        self.deleted = True


def run_distribution(monkeypatch, *, item_result, market_result=None):
    bot.save_guild_config(1, loot_log_channel_id=999)
    sent: list[discord.Embed] = []

    class Channel:
        async def send(self, embed):
            sent.append(embed)

    guild = types.SimpleNamespace(get_channel=lambda channel_id: Channel() if channel_id == 999 else None)
    interaction = FakeInteraction()
    interaction.guild = guild
    recipient = types.SimpleNamespace(id=9, name="winner", mention="<@9>")
    monkeypatch.setattr(bot, "fetch_item", lambda item_id: item_result)
    monkeypatch.setattr(bot, "fetch_market", lambda ahid: market_result)

    async def run():
        modal = bot.LootNoteModal(1, FakeMessage(), "Some Item", None, "some_item", None, recipient)
        modal.note._value = ""
        await modal.on_submit(interaction)

    asyncio.run(run())
    return sent[0]


def test_the_distribution_log_shows_the_current_price(monkeypatch):
    embed = run_distribution(
        monkeypatch, item_result={"auctionHouseId": 42}, market_result={"minPrice": 12950, "inStock": 2}
    )
    assert field(embed, "Current Price") == "12 950 ◈ (×2 in stock)"


@pytest.mark.parametrize("item_result", [None, "timeout"])
def test_the_distribution_log_says_unavailable_when_the_lookup_fails(monkeypatch, item_result):
    embed = run_distribution(monkeypatch, item_result=item_result)
    assert field(embed, "Current Price") == "Unavailable"

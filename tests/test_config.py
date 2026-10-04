import asyncio
import os
import time
import types

import discord
import pytest

import bot


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "DATA_DIR", str(tmp_path))
    return tmp_path


class FakeResponse:
    async def defer(self, **kwargs):
        pass

    async def send_message(self, *args, **kwargs):
        pass


class FakeFollowup:
    def __init__(self):
        self.messages: list[str] = []

    async def send(self, content=None, **kwargs):
        self.messages.append(content)


class FakeInteraction:
    def __init__(self, user_id: int):
        self.guild_id = 1
        self.user = types.SimpleNamespace(id=user_id, name=f"user{user_id}")
        self.response = FakeResponse()
        self.followup = FakeFollowup()


def slow_fetch_item(item_id: str) -> dict:
    time.sleep(0.2)
    return {"id": item_id, "name": item_id.upper()}


def not_found() -> discord.NotFound:
    response = types.SimpleNamespace(status=404, reason="Not Found")
    return discord.NotFound(response, "Unknown Member")


def test_save_merges_updates_without_clobbering_other_keys():
    bot.save_guild_config(1, command_role_id=10)
    bot.save_guild_config(1, wishlist_limit=3)
    assert bot.load_guild_config(1) == {"command_role_id": 10, "wishlist_limit": 3}


def test_failed_write_keeps_previous_config(monkeypatch, data_dir):
    bot.save_guild_config(1, wishlist_limit=3)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(bot.json, "dump", boom)
        with pytest.raises(OSError):
            bot.save_guild_config(1, wishlist_limit=9)

    assert bot.load_guild_config(1) == {"wishlist_limit": 3}
    assert not [name for name in os.listdir(data_dir) if name.endswith(".tmp")]


def test_corrupt_config_is_quarantined_and_never_overwritten(data_dir):
    (data_dir / "1.json").write_text('{"wishlists": {"42": [{"id": "a"')

    assert bot.load_guild_config(1) == {}
    quarantined = [name for name in os.listdir(data_dir) if ".corrupt-" in name]
    assert len(quarantined) == 1
    assert (data_dir / quarantined[0]).read_text().startswith('{"wishlists"')

    bot.save_guild_config(1, wishlist_limit=5)
    assert (data_dir / quarantined[0]).exists()


def test_unreadable_config_fails_loudly_instead_of_reading_as_empty(data_dir):
    (data_dir / "1.json").mkdir()
    with pytest.raises(OSError):
        bot.load_guild_config(1)


def test_try_add_wishlist_item_statuses():
    assert bot.try_add_wishlist_item(1, 7, "a", "A") == ("unconfigured", 0)
    bot.save_guild_config(1, wishlist_limit=2)
    assert bot.try_add_wishlist_item(1, 7, "a", "A") == ("added", 1)
    assert bot.try_add_wishlist_item(1, 7, "a", "A") == ("duplicate", 1)
    assert bot.try_add_wishlist_item(1, 7, "b", "B") == ("added", 2)
    assert bot.try_add_wishlist_item(1, 7, "c", "C") == ("full", 2)


def test_concurrent_adds_by_different_members_are_both_kept(monkeypatch):
    bot.save_guild_config(1, wishlist_limit=3)
    monkeypatch.setattr(bot, "fetch_item", slow_fetch_item)

    async def run():
        await asyncio.gather(
            bot.wishlist_command.callback(FakeInteraction(1), "a"),
            bot.wishlist_command.callback(FakeInteraction(2), "b"),
        )

    asyncio.run(run())

    wishlists = bot.load_guild_config(1)["wishlists"]
    assert [i["id"] for i in wishlists["1"]] == ["a"]
    assert [i["id"] for i in wishlists["2"]] == ["b"]


def test_concurrent_adds_by_same_member_cannot_exceed_the_limit(monkeypatch):
    bot.save_guild_config(1, wishlist_limit=1)
    monkeypatch.setattr(bot, "fetch_item", slow_fetch_item)
    first, second = FakeInteraction(1), FakeInteraction(1)

    async def run():
        await asyncio.gather(
            bot.wishlist_command.callback(first, "a"),
            bot.wishlist_command.callback(second, "b"),
        )

    asyncio.run(run())

    assert len(bot.load_guild_config(1)["wishlists"]["1"]) == 1
    outcomes = sorted(m.split()[0] for m in first.followup.messages + second.followup.messages)
    assert outcomes == ["✅", "❌"]


def test_cleanup_keeps_wishlists_added_while_it_was_running(monkeypatch):
    bot.save_guild_config(1, wishlist_limit=3, wishlists={"2": [{"id": "x", "name": "X"}]})

    async def instant_sleep(_):
        return None

    async def fake_fetch_user(user_id):
        return types.SimpleNamespace(name=f"gone{user_id}")

    monkeypatch.setattr(bot.asyncio, "sleep", instant_sleep)
    monkeypatch.setattr(bot.client, "fetch_user", fake_fetch_user)

    class FakeGuild:
        id = 1

        def get_member(self, user_id):
            return None

        async def fetch_member(self, user_id):
            wishlists = bot.load_guild_config(1)["wishlists"]
            wishlists["3"] = [{"id": "new", "name": "New"}]
            bot.save_guild_config(1, wishlists=wishlists)
            raise not_found()

    removed = asyncio.run(bot.clean_guild_wishlists(FakeGuild()))

    assert removed == [("2", "gone2")]
    wishlists = bot.load_guild_config(1)["wishlists"]
    assert "2" not in wishlists
    assert [i["id"] for i in wishlists["3"]] == ["new"]


def test_cleanup_leaves_members_untouched_on_transient_api_errors(monkeypatch):
    bot.save_guild_config(1, wishlists={"2": [{"id": "x", "name": "X"}]})

    async def instant_sleep(_):
        return None

    monkeypatch.setattr(bot.asyncio, "sleep", instant_sleep)

    class FakeGuild:
        id = 1

        def get_member(self, user_id):
            return None

        async def fetch_member(self, user_id):
            raise discord.HTTPException(types.SimpleNamespace(status=500, reason="boom"), "boom")

    assert asyncio.run(bot.clean_guild_wishlists(FakeGuild())) == []
    assert "2" in bot.load_guild_config(1)["wishlists"]

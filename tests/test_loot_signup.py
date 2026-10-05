import asyncio
import copy
import types

import discord
import pytest

import bot
from questlog import domain

def deep_copy_embed(embed: discord.Embed) -> discord.Embed:
    # Embed.copy() shares the internal fields list, which would leak edits between "clients".
    return discord.Embed.from_dict(copy.deepcopy(embed.to_dict()))


BUTTONS = {"pvp": "pvp_button", "pve": "pve_button", "alt": "alt_button", "greed": "greed_button"}


@pytest.fixture(autouse=True)
def loot_env(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "DATA_DIR", str(tmp_path))
    bot.save_guild_config(1, button_role_id=10)
    monkeypatch.setattr(bot, "has_role", lambda member, role_id: True)
    monkeypatch.setattr(bot, "LOOT_STATES", domain.LootStateStore())


class FakeServerMessage:
    """Models Discord's side: clicks carry a snapshot of the message, edits land after a
    network delay, and the last edit to land wins."""

    id = 555

    def __init__(self, signups: dict[str, list[int]] | None = None, delay: float = 0.05):
        embed = discord.Embed(title="Some Item")
        state = {key: [] for key, _, _ in domain.LOOT_CATEGORIES}
        state.update(signups or {})
        embed.add_field(name=domain.LOOT_FIELD_NAME, value=domain.format_loot_field(state), inline=False)
        self.embed = embed
        self.delay = delay
        self.fail_next_edit = False
        self.events: list[tuple[str, int]] = []

    def signups(self) -> dict[str, list[int]]:
        return domain.parse_loot_field(self.embed.fields[0].value)

    async def apply(self, embed: discord.Embed, user_id: int) -> None:
        await asyncio.sleep(self.delay)
        if self.fail_next_edit:
            self.fail_next_edit = False
            raise discord.HTTPException(types.SimpleNamespace(status=500, reason="boom"), "boom")
        self.embed = deep_copy_embed(embed)
        self.events.append(("edit_done", user_id))


class FakeComponentResponse:
    def __init__(self, server: FakeServerMessage, user_id: int):
        self._server = server
        self._user_id = user_id
        self._done = False
        self.sent: list[str] = []

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **kwargs):
        self._done = True
        self._server.events.append(("defer", self._user_id))

    async def edit_message(self, *, embed=None, view=None):
        self._done = True
        await self._server.apply(embed, self._user_id)

    async def send_message(self, content=None, **kwargs):
        self._done = True
        self.sent.append(content)


class FakeClick:
    def __init__(self, server: FakeServerMessage, user_id: int):
        self._server = server
        self._user_id = user_id
        self.guild_id = 1
        self.user = types.SimpleNamespace(id=user_id, name=f"user{user_id}")
        self.message = types.SimpleNamespace(id=server.id, embeds=[deep_copy_embed(server.embed)])
        self.response = FakeComponentResponse(server, user_id)

    async def edit_original_response(self, *, embed=None, view=None):
        await self._server.apply(embed, self._user_id)


async def click(interaction: FakeClick, category: str) -> None:
    view = bot.LootView()
    await getattr(view, BUTTONS[category]).callback(interaction)


def test_toggle_adds_moves_and_removes_a_signup_without_mutating_its_input():
    empty = {key: [] for key, _, _ in domain.LOOT_CATEGORIES}

    added = domain.toggle_loot_signup(empty, 1, "pvp")
    assert added["pvp"] == [1]
    assert empty["pvp"] == []

    moved = domain.toggle_loot_signup(added, 1, "greed")
    assert moved["pvp"] == [] and moved["greed"] == [1]

    removed = domain.toggle_loot_signup(moved, 1, "greed")
    assert removed == empty


def test_simultaneous_clicks_made_from_the_same_stale_snapshot_are_all_kept():
    server = FakeServerMessage()
    clicks = [(FakeClick(server, 1), "pvp"), (FakeClick(server, 2), "pve"), (FakeClick(server, 3), "greed")]

    async def run():
        await asyncio.gather(*(click(interaction, category) for interaction, category in clicks))

    asyncio.run(run())

    signups = server.signups()
    assert signups["pvp"] == [1]
    assert signups["pve"] == [2]
    assert signups["greed"] == [3]


def test_a_burst_of_clicks_is_acknowledged_before_the_first_edit_lands():
    server = FakeServerMessage()
    clicks = [(FakeClick(server, uid), "pvp") for uid in range(1, 6)]

    async def run():
        await asyncio.gather(*(click(interaction, category) for interaction, category in clicks))

    asyncio.run(run())

    kinds = [kind for kind, _ in server.events]
    assert kinds.count("defer") == 5
    assert max(i for i, kind in enumerate(kinds) if kind == "defer") < kinds.index("edit_done")
    assert sorted(server.signups()["pvp"]) == [1, 2, 3, 4, 5]


def test_the_first_click_after_a_restart_builds_on_the_signups_already_in_the_embed():
    server = FakeServerMessage(signups={"pve": [9]})

    asyncio.run(click(FakeClick(server, 1), "pvp"))

    signups = server.signups()
    assert signups["pve"] == [9]
    assert signups["pvp"] == [1]


def test_a_failed_edit_does_not_poison_the_remembered_state():
    server = FakeServerMessage()

    async def run():
        server.fail_next_edit = True
        with pytest.raises(discord.HTTPException):
            await click(FakeClick(server, 1), "pvp")
        await click(FakeClick(server, 2), "pve")

    asyncio.run(run())

    signups = server.signups()
    assert signups["pvp"] == []
    assert signups["pve"] == [2]


def test_a_member_without_the_button_role_is_refused_before_anything_is_acknowledged(monkeypatch):
    monkeypatch.setattr(bot, "has_role", lambda member, role_id: False)
    server = FakeServerMessage()
    interaction = FakeClick(server, 1)

    asyncio.run(click(interaction, "pvp"))

    assert interaction.response.sent == ["❌ You don't have permission to click these buttons."]
    assert server.events == []
    assert server.signups()["pvp"] == []


def test_the_state_store_evicts_the_oldest_messages_first():
    store = domain.LootStateStore(max_size=2)
    for message_id in (1, 2, 3):
        store.entry(message_id).state = {"pvp": [message_id]}

    assert store.entry(2).state == {"pvp": [2]}
    assert store.entry(3).state == {"pvp": [3]}
    assert store.entry(1).state is None


def test_the_state_store_never_evicts_a_message_that_is_being_edited():
    store = domain.LootStateStore(max_size=2)

    async def run():
        busy = store.entry(1)
        async with busy.lock:
            store.entry(2)
            store.entry(3)
            return store.entry(1) is busy

    assert asyncio.run(run())

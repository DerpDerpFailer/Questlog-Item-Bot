import asyncio
import types

import discord
import pytest

import bot
from questlog import domain


@pytest.fixture(autouse=True)
def loot_env(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "DATA_DIR", str(tmp_path))
    bot.save_guild_config(1, command_role_id=20, button_role_id=10, loot_log_channel_id=999)
    monkeypatch.setattr(bot, "has_role", lambda member, role_id: True)
    monkeypatch.setattr(bot, "fetch_item", lambda name: {"id": "some_item", "name": "Some Item", "auctionHouseId": 1})
    monkeypatch.setattr(bot, "fetch_market", lambda ahid: None)
    monkeypatch.setattr(bot, "build_embed", lambda item, ah: discord.Embed(title=item["name"]))


class FakeResponse:
    def __init__(self):
        self.deferred: list[dict] = []
        self.modals: list[discord.ui.Modal] = []
        self.sent: list[str] = []

    async def defer(self, **kwargs):
        self.deferred.append(kwargs)

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def send_message(self, content=None, **kwargs):
        self.sent.append(content)


class FakeFollowup:
    def __init__(self):
        self.posts: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.posts.append({"content": content, **kwargs})


def make_interaction():
    return types.SimpleNamespace(
        guild_id=1,
        user=types.SimpleNamespace(id=7, name="staff", mention="<@7>"),
        response=FakeResponse(),
        followup=FakeFollowup(),
    )


def submit(note: str):
    interaction = make_interaction()
    modal = bot.LootPostModal("some_item")
    modal.note._value = note
    asyncio.run(modal.on_submit(interaction))
    return interaction


def test_item_loot_opens_the_note_modal_before_any_lookup(monkeypatch):
    monkeypatch.setattr(bot, "fetch_item", lambda name: pytest.fail("looked up before the modal"))
    interaction = make_interaction()

    asyncio.run(bot.item_loot_command.callback(interaction, "some_item"))

    [modal] = interaction.response.modals
    assert isinstance(modal, bot.LootPostModal)
    assert modal.item_name == "some_item"
    assert modal.note.required is False
    assert interaction.response.deferred == []


def test_item_loot_refuses_without_the_command_role_and_opens_nothing(monkeypatch):
    monkeypatch.setattr(bot, "has_role", lambda member, role_id: False)
    interaction = make_interaction()

    asyncio.run(bot.item_loot_command.callback(interaction, "some_item"))

    assert interaction.response.modals == []
    assert interaction.response.sent == ["❌ You don't have permission to use this command."]


def test_a_note_is_shown_above_the_signups():
    interaction = submit("  Only for DD, healers can't need this item  ")

    # A modal opened by a slash command has no message to update: it must defer with thinking.
    assert interaction.response.deferred == [{"thinking": True}]
    [post] = interaction.followup.posts
    names = [f.name for f in post["embed"].fields]
    assert names == [domain.LOOT_NOTE_FIELD_NAME, domain.LOOT_FIELD_NAME]
    assert post["embed"].fields[0].value == "Only for DD, healers can't need this item"
    assert isinstance(post["view"], bot.LootView)


@pytest.mark.parametrize("note", ["", "   \n "])
def test_an_empty_note_adds_no_field(note):
    [post] = submit(note).followup.posts
    assert [f.name for f in post["embed"].fields] == [domain.LOOT_FIELD_NAME]


def test_the_note_fits_in_an_embed_field():
    assert bot.LootPostModal("x").note.max_length <= domain.EMBED_FIELD_MAX_CHARS


def test_the_distribution_report_carries_the_loot_note(monkeypatch):
    sent: list[discord.Embed] = []

    class Channel:
        async def send(self, embed):
            sent.append(embed)

    class Message:
        async def delete(self):
            pass

    monkeypatch.setattr(bot, "fetch_market_for_item", lambda item_id: None)
    interaction = make_interaction()
    interaction.guild = types.SimpleNamespace(get_channel=lambda channel_id: Channel())

    async def edit_message(**kwargs):
        pass

    interaction.response.edit_message = edit_message
    recipient = types.SimpleNamespace(id=2, name="winner", mention="<@2>")
    modal = bot.LootNoteModal(1, Message(), "Some Item", None, None, None, recipient, None, "Only for DD")
    modal.note._value = "won the roll"

    asyncio.run(modal.on_submit(interaction))

    [report] = sent
    fields = {f.name: f.value for f in report.fields}
    assert fields["Loot note"] == "Only for DD"
    assert fields["Note"] == "won the roll"


def test_distributed_reads_the_note_from_the_post():
    embed = discord.Embed(title="Some Item")
    embed.add_field(name=domain.LOOT_NOTE_FIELD_NAME, value="Only for DD", inline=False)
    embed.add_field(name=domain.LOOT_FIELD_NAME, value="x", inline=False)
    interaction = make_interaction()
    interaction.message = types.SimpleNamespace(id=1, embeds=[embed])
    views = []

    async def send_message(content=None, *, view=None, **kwargs):
        views.append(view)

    interaction.response.send_message = send_message

    async def run():
        view = bot.LootView()
        await view.distributed_button.callback(interaction)

    asyncio.run(run())
    assert views[0].loot_note == "Only for DD"

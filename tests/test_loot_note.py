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


class FakeRole:
    def __init__(self, role_id: int, name: str, mentionable: bool = True):
        self.id, self.name, self.mentionable = role_id, name, mentionable
        self.mention = f"<@&{role_id}>"

    def is_default(self) -> bool:
        return self.name == "@everyone"


class FakeGuild:
    def __init__(self):
        self.roles = [
            FakeRole(1, "@everyone"), FakeRole(50, "Raid Lead"), FakeRole(51, "Raid"),
            FakeRole(52, "Hidden", mentionable=False),
        ]
        self.members = [
            types.SimpleNamespace(id=100, name="alice", display_name="Ali", global_name="Alice"),
            types.SimpleNamespace(id=101, name="bob", display_name="bob", global_name=None),
            types.SimpleNamespace(id=102, name="bobby", display_name="bobby", global_name=None),
        ]
        self.queries: list[str] = []

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    async def query_members(self, query, limit):
        # Like Discord: prefix match on the username.
        self.queries.append(query)
        return [m for m in self.members if m.name.startswith(query.lower())][:limit]


def make_interaction():
    interaction = types.SimpleNamespace(
        guild_id=1,
        guild=FakeGuild(),
        app_permissions=types.SimpleNamespace(mention_everyone=False),
        user=types.SimpleNamespace(id=7, name="staff", mention="<@7>"),
        response=FakeResponse(),
        followup=FakeFollowup(),
        original=[],
    )

    async def edit_original_response(content=None, **kwargs):
        interaction.original.append(content)

    interaction.edit_original_response = edit_original_response
    return interaction


def submit(note: str, interaction=None):
    interaction = interaction or make_interaction()
    modal = bot.LootPostModal("some_item")
    modal.note._value = note
    asyncio.run(modal.on_submit(interaction))
    return interaction


# ── The command and the modal ────────────────────────────────────────────────

def test_item_loot_opens_the_note_modal_before_any_lookup(monkeypatch):
    monkeypatch.setattr(bot, "fetch_item", lambda name: pytest.fail("looked up before the modal"))
    interaction = make_interaction()

    asyncio.run(bot.item_loot_command.callback(interaction, "some_item"))

    [modal] = interaction.response.modals
    assert isinstance(modal, bot.LootPostModal)
    assert modal.item_name == "some_item"
    assert modal.note.required is False
    assert interaction.response.deferred == []


def test_the_note_placeholder_fits_discords_limit():
    assert len(bot.LootPostModal("x").note.placeholder) <= 100


def test_item_loot_refuses_without_the_command_role_and_opens_nothing(monkeypatch):
    monkeypatch.setattr(bot, "has_role", lambda member, role_id: False)
    interaction = make_interaction()

    asyncio.run(bot.item_loot_command.callback(interaction, "some_item"))

    assert interaction.response.modals == []
    assert interaction.response.sent == ["❌ You don't have permission to use this command."]


def test_the_note_is_the_message_content_above_the_embed():
    interaction = submit("  Only for DD, healers can't need this item  ")

    # Ephemeral thinking, filled in before the post: the post must be a new message to ping.
    assert interaction.response.deferred == [{"thinking": True, "ephemeral": True}]
    assert interaction.original == ["✅ Posted **Some Item**."]
    [post] = interaction.followup.posts
    assert post["content"] == "Only for DD, healers can't need this item"
    assert [f.name for f in post["embed"].fields] == [domain.LOOT_FIELD_NAME]
    assert isinstance(post["view"], bot.LootView)
    assert post.get("ephemeral", False) is False


@pytest.mark.parametrize("note", ["", "   \n "])
def test_an_empty_note_posts_no_content_and_pings_nobody(note):
    interaction = submit(note)
    [post] = interaction.followup.posts
    assert post["content"] is None
    assert post["allowed_mentions"].users is False and post["allowed_mentions"].roles is False
    assert interaction.guild.queries == []


def test_an_unknown_item_is_reported_privately_and_nothing_is_posted(monkeypatch):
    monkeypatch.setattr(bot, "fetch_item", lambda name: None)
    interaction = submit("Only for DD")
    assert interaction.followup.posts == []
    assert interaction.original[0].startswith("❌ Item not found")


# ── Mentions ──────────────────────────────────────────────────────────────────

def test_roles_and_members_typed_with_at_are_resolved_and_pinged():
    interaction = submit("@Raid Lead and @raid: @alice @Bob, not @bo or @everyone")

    [post] = interaction.followup.posts
    assert post["content"] == "<@&50> and <@&51>: <@100> <@101>, not @bo or @everyone"
    allowed = post["allowed_mentions"]
    assert allowed.everyone is False
    assert sorted(o.id for o in allowed.roles) == [50, 51]
    assert sorted(o.id for o in allowed.users) == [100, 101]
    # "@bo" matches bob and bobby by prefix but neither exactly: left alone and reported.
    assert "`@bo`" in interaction.original[0]


def test_an_unmentionable_role_is_flagged_unless_the_bot_can_ping_any_role():
    interaction = submit("@Hidden")
    assert "role not mentionable" in interaction.original[0]

    interaction = make_interaction()
    interaction.app_permissions.mention_everyone = True
    submit("@Hidden", interaction)
    assert interaction.original == ["✅ Posted **Some Item**."]


def test_raw_mentions_and_email_addresses_are_left_untouched():
    interaction = submit("<@100> <@&50> mail me at bob@example.com")
    [post] = interaction.followup.posts
    assert post["content"] == "<@100> <@&50> mail me at bob@example.com"
    assert interaction.guild.queries == []


def test_a_note_too_long_once_resolved_is_refused():
    interaction = submit("@raid " * 300)  # 1800 chars typed, 2100 once resolved
    assert interaction.followup.posts == []
    assert "too long" in interaction.original[0]


def test_a_failing_member_lookup_leaves_the_text_as_is(monkeypatch):
    interaction = make_interaction()

    async def boom(query, limit):
        raise asyncio.TimeoutError

    interaction.guild.query_members = boom
    submit("@alice", interaction)
    [post] = interaction.followup.posts
    assert post["content"] == "@alice"


# ── Distribution ──────────────────────────────────────────────────────────────

def run_report(monkeypatch, loot_note: str) -> dict[str, str]:
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
    modal = bot.LootNoteModal(1, Message(), "Some Item", None, None, None, recipient, None, loot_note)
    modal.note._value = "won the roll"

    asyncio.run(modal.on_submit(interaction))
    [report] = sent
    return {f.name: f.value for f in report.fields}


def test_the_distribution_report_carries_the_loot_note(monkeypatch):
    fields = run_report(monkeypatch, "Only for DD <@&50>")
    assert fields["Loot note"] == "Only for DD <@&50>"
    assert fields["Note"] == "won the roll"


def test_a_long_loot_note_is_cut_to_fit_the_report_field(monkeypatch):
    fields = run_report(monkeypatch, "x" * 1500)
    assert len(fields["Loot note"]) == domain.EMBED_FIELD_MAX_CHARS


@pytest.mark.parametrize("as_field", [False, True])
def test_distributed_reads_the_note_from_the_post(as_field):
    embed = discord.Embed(title="Some Item")
    if as_field:  # posts made while the note was an embed field
        embed.add_field(name=domain.LOOT_NOTE_FIELD_NAME, value="Only for DD", inline=False)
    embed.add_field(name=domain.LOOT_FIELD_NAME, value="x", inline=False)
    interaction = make_interaction()
    interaction.message = types.SimpleNamespace(id=1, embeds=[embed], content="" if as_field else "Only for DD")
    views = []

    async def send_message(content=None, *, view=None, **kwargs):
        views.append(view)

    interaction.response.send_message = send_message

    asyncio.run(bot.LootView().distributed_button.callback(interaction))
    assert views[0].loot_note == "Only for DD"

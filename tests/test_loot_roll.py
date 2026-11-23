import asyncio
import copy
import types

import discord
import pytest
from discord import app_commands

import bot
from questlog import domain

SIGNUP_BUTTONS = ("pvp_button", "pve_button", "alt_button", "greed_button")


class ScriptedDice:
    """Replaces bot.ROLL_RNG: hands out the given rolls in order, then fails loudly."""

    def __init__(self, *rolls: int):
        self.rolls = list(rolls)

    def randint(self, low: int, high: int) -> int:
        assert (low, high) == (1, domain.LOOT_ROLL_MAX)
        return self.rolls.pop(0)


def dice(*rolls: int):
    return ScriptedDice(*rolls).randint


def deep_copy_embed(embed: discord.Embed) -> discord.Embed:
    return discord.Embed.from_dict(copy.deepcopy(embed.to_dict()))


@pytest.fixture(autouse=True)
def loot_env(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "DATA_DIR", str(tmp_path))
    bot.save_guild_config(1, command_role_id=20, button_role_id=10, loot_log_channel_id=999)
    monkeypatch.setattr(bot, "has_role", lambda member, role_id: True)
    monkeypatch.setattr(bot, "LOOT_STATES", domain.LootStateStore())


class FakeServerMessage:
    """Discord's side of one loot post: edits land after a delay, the last one wins."""

    id = 555

    def __init__(self, signups: dict[str, list[int]] | None = None, delay: float = 0.02):
        embed = discord.Embed(title="Some Item", url="https://questlog.gg/throne-and-liberty/en/db/item/some_item")
        state = {key: [] for key, _, _ in domain.LOOT_CATEGORIES}
        state.update(signups or {})
        embed.add_field(name=domain.LOOT_FIELD_NAME, value=domain.format_loot_field(state), inline=False)
        self.embed = embed
        self.view: discord.ui.View | None = None
        self.delay = delay
        self.announcements: list[dict] = []

    async def apply(self, embed: discord.Embed, view) -> None:
        await asyncio.sleep(self.delay)
        self.embed = deep_copy_embed(embed)
        self.view = view

    def field(self, name: str) -> str | None:
        return next((f.value for f in self.embed.fields if f.name == name), None)


class FakeResponse:
    def __init__(self):
        self._done = False
        self.sent: list[str] = []
        self.modals: list[discord.ui.Modal] = []
        self.views: list[discord.ui.View] = []

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **kwargs):
        self._done = True

    async def send_message(self, content=None, *, view=None, **kwargs):
        self._done = True
        self.sent.append(content)
        if view is not None:
            self.views.append(view)

    async def send_modal(self, modal):
        self._done = True
        self.modals.append(modal)


class FakeFollowup:
    def __init__(self, server: FakeServerMessage):
        self._server = server
        self.ephemeral: list[str] = []

    async def send(self, content=None, *, ephemeral=False, allowed_mentions=None, **kwargs):
        if ephemeral:
            self.ephemeral.append(content)
        else:
            self._server.announcements.append({"content": content, "allowed_mentions": allowed_mentions})


class FakeClick:
    def __init__(self, server: FakeServerMessage, user_id: int = 7):
        self._server = server
        self.guild_id = 1
        self.user = types.SimpleNamespace(id=user_id, name=f"user{user_id}", mention=f"<@{user_id}>")
        self.message = types.SimpleNamespace(id=server.id, embeds=[deep_copy_embed(server.embed)])
        self.response = FakeResponse()
        self.followup = FakeFollowup(server)

    async def edit_original_response(self, *, embed=None, view=None):
        await self._server.apply(embed, view)


async def press(interaction: FakeClick, button: str) -> None:
    await getattr(bot.LootView(), button).callback(interaction)


def roll(server: FakeServerMessage, *rolls: int, monkeypatch, user_id: int = 7) -> FakeClick:
    monkeypatch.setattr(bot, "ROLL_RNG", ScriptedDice(*rolls))
    interaction = FakeClick(server, user_id)
    asyncio.run(press(interaction, "roll_button"))
    return interaction


def reroll(server: FakeServerMessage, reason: str, *rolls: int, monkeypatch) -> FakeClick:
    monkeypatch.setattr(bot, "ROLL_RNG", ScriptedDice(*rolls))
    interaction = FakeClick(server, 8)

    async def run():
        await press(interaction, "roll_button")
        modal = interaction.response.modals[0]
        modal.reason._value = reason
        submit = FakeClick(server, 8)
        await modal.on_submit(submit)
        return submit

    return asyncio.run(run())


# ── Pure roll logic ───────────────────────────────────────────────────────────

def test_each_category_rolls_only_among_its_own_signups_in_signup_order():
    result = domain.roll_loot({"pvp": [1, 2, 3], "pve": [4, 5], "alt": [], "greed": []}, dice(65, 32, 78, 12, 87))

    assert result == {
        "pvp": [(1, (65,)), (2, (32,)), (3, (78,))],
        "pve": [(4, (12,)), (5, (87,))],
    }
    assert domain.roll_winner(result["pvp"]) == 3
    assert domain.roll_winner(result["pve"]) == 5


def test_a_tie_for_first_is_rerolled_among_the_tied_members_only():
    result = domain.roll_loot({"pvp": [1, 2, 3]}, dice(90, 40, 90, 20, 20, 55, 10))

    assert result["pvp"] == [(1, (90, 20, 55)), (2, (40,)), (3, (90, 20, 10))]
    assert domain.roll_winner(result["pvp"]) == 1


def test_a_tie_below_first_place_is_left_alone():
    result = domain.roll_loot({"pvp": [1, 2, 3]}, dice(40, 40, 90))

    assert result["pvp"] == [(1, (40,)), (2, (40,)), (3, (90,))]


def test_the_field_shows_scores_and_winners_and_reads_back():
    result = domain.roll_loot({"pvp": [1, 2], "greed": [9]}, dice(65, 78, 50))
    value = domain.format_roll_field(result, rolled_by=7, rolled_at=1700000000)

    assert value.splitlines() == [
        "**Main PvP:** <@1> (65) · <@2> (78) → 🏆 <@2>",
        "**Greed:** 🏆 <@9> (only one sign-up)",
        "Rolled by <@7> <t:1700000000:f>",
    ]
    assert domain.parse_roll_field(value) == {"winners": {"pvp": 2, "greed": 9}, "rolled_by": 7, "rolled_at": 1700000000}


@pytest.mark.parametrize("signups, expected", [
    ({"pvp": [1], "pve": [2], "greed": [3]}, 1),
    ({"pve": [2], "greed": [3]}, 2),
    ({"alt": [4], "greed": [3]}, 4),
    ({"greed": [3]}, 3),
])
def test_the_preselected_winner_follows_the_category_priority(signups, expected):
    result = domain.roll_loot(signups, dice(*range(1, 10)))
    value = domain.format_roll_field(result, rolled_by=7, rolled_at=1)

    assert domain.preselected_winner(value) == expected


def test_no_roll_means_no_preselection():
    assert domain.preselected_winner(None) is None


def test_a_reroll_keeps_its_reason_and_only_the_roll_it_replaces():
    first = domain.format_roll_field(domain.roll_loot({"pvp": [1, 2]}, dice(90, 10)), rolled_by=7, rolled_at=100)
    second = domain.format_roll_field(
        domain.roll_loot({"pvp": [1, 2]}, dice(10, 90)), rolled_by=8, rolled_at=200, reason="wrong\ncategory", previous=first
    )
    third = domain.format_roll_field(
        domain.roll_loot({"pvp": [1, 2]}, dice(50, 60)), rolled_by=8, rolled_at=300, reason="again", previous=second
    )

    assert "Re-rolled by <@8> <t:200:f> — reason: wrong category" in second
    assert "Previous: Main PvP 🏆 <@1> (by <@7> <t:100:f>)" in second
    assert third.count("Previous:") == 1
    assert "Previous: Main PvP 🏆 <@2> (by <@8> <t:200:f>)" in third
    assert domain.parse_roll_field(third)["winners"] == {"pvp": 2}


def test_a_crowded_roll_falls_back_to_a_compact_field_that_still_names_the_winners():
    signups = {"pvp": list(range(10**17, 10**17 + 40)), "pve": list(range(2 * 10**17, 2 * 10**17 + 5))}
    result = domain.roll_loot(signups, dice(*range(1, 46)))
    value = domain.format_roll_field(result, rolled_by=7, rolled_at=1, reason="r" * 200, previous=None)

    assert len(value) <= domain.EMBED_FIELD_MAX_CHARS
    assert "+ 39 other(s)" in value
    assert domain.parse_roll_field(value)["winners"] == {"pvp": 10**17 + 39, "pve": 2 * 10**17 + 4}


@pytest.mark.parametrize("mode, expected", [("winners", [2, 9]), ("all", [1, 2, 9]), ("none", [])])
def test_ping_targets_follow_the_configured_mode(mode, expected):
    result = domain.roll_loot({"pvp": [1, 2], "greed": [9]}, dice(10, 20, 30))

    assert domain.roll_ping_targets(result, mode) == expected


# ── Roll button ───────────────────────────────────────────────────────────────

def test_only_the_staff_can_roll(monkeypatch):
    monkeypatch.setattr(bot, "has_role", lambda member, role_id: role_id != 20)
    server = FakeServerMessage(signups={"pvp": [1]})

    interaction = roll(server, monkeypatch=monkeypatch)

    assert interaction.response.sent == ["❌ You don't have permission to roll this item."]
    assert server.field(domain.LOOT_ROLL_FIELD_NAME) is None


def test_rolling_without_signups_is_refused(monkeypatch):
    server = FakeServerMessage()

    interaction = roll(server, monkeypatch=monkeypatch)

    assert interaction.response.sent == ["⚠️ Nobody signed up for this item yet."]
    assert server.view is None


def test_a_roll_writes_the_result_and_greys_out_signups_and_roll(monkeypatch):
    server = FakeServerMessage(signups={"pvp": [1, 2, 3], "pve": [4, 5]})

    roll(server, 65, 32, 78, 12, 87, monkeypatch=monkeypatch)

    value = server.field(domain.LOOT_ROLL_FIELD_NAME)
    assert value.startswith("**Main PvP:** <@1> (65) · <@2> (32) · <@3> (78) → 🏆 <@3>\n**Main PvE:** <@4> (12) · <@5> (87) → 🏆 <@5>")
    assert all(getattr(server.view, name).disabled for name in SIGNUP_BUTTONS)
    assert server.view.roll_button.disabled
    assert not server.view.distributed_button.disabled
    assert domain.parse_loot_field(server.field(domain.LOOT_FIELD_NAME))["pvp"] == [1, 2, 3]


def test_with_rerolls_enabled_the_roll_button_stays_clickable(monkeypatch):
    bot.save_guild_config(1, loot_reroll_enabled=True)
    server = FakeServerMessage(signups={"pvp": [1]})

    roll(server, 50, monkeypatch=monkeypatch)

    assert not server.view.roll_button.disabled
    assert all(getattr(server.view, name).disabled for name in SIGNUP_BUTTONS)


def test_greying_out_one_message_never_touches_the_persistent_view():
    bot.LootView(rolled=True)
    fresh = bot.LootView()

    assert not any(item.disabled for item in fresh.children)


def test_the_buttons_keep_their_custom_ids_and_rows():
    view = bot.LootView()

    assert [(b.custom_id, b.row) for b in view.children] == [
        ("loot_pvp", 0), ("loot_pve", 0), ("loot_alt", 0), ("loot_greed", 0), ("loot_roll", 1), ("loot_distributed", 1),
    ]


def test_a_signup_racing_a_roll_is_refused_once_the_roll_landed(monkeypatch):
    server = FakeServerMessage(signups={"pvp": [1]})
    late = FakeClick(server, 2)  # snapshot taken before the roll, so its embed has no roll field

    roll(server, 50, monkeypatch=monkeypatch)
    asyncio.run(press(late, "pve_button"))

    assert late.followup.ephemeral == ["🔒 Sign-ups are closed: this item was already rolled."]
    assert domain.parse_loot_field(server.field(domain.LOOT_FIELD_NAME))["pve"] == []


def test_a_signup_on_a_rolled_post_is_refused_after_a_restart(monkeypatch):
    server = FakeServerMessage(signups={"pvp": [1]})
    roll(server, 50, monkeypatch=monkeypatch)
    monkeypatch.setattr(bot, "LOOT_STATES", domain.LootStateStore())

    late = FakeClick(server, 2)
    asyncio.run(press(late, "pve_button"))

    assert late.followup.ephemeral == ["🔒 Sign-ups are closed: this item was already rolled."]


def test_two_simultaneous_roll_clicks_roll_only_once(monkeypatch):
    server = FakeServerMessage(signups={"pvp": [1, 2]})
    monkeypatch.setattr(bot, "ROLL_RNG", ScriptedDice(10, 90, 99, 1))
    first, second = FakeClick(server, 7), FakeClick(server, 8)

    async def run():
        await asyncio.gather(press(first, "roll_button"), press(second, "roll_button"))

    asyncio.run(run())

    assert "🏆 <@2>" in server.field(domain.LOOT_ROLL_FIELD_NAME)
    assert first.followup.ephemeral + second.followup.ephemeral == ["⚠️ This item was already rolled."]
    assert len(server.announcements) == 1


def test_a_second_roll_is_refused_when_rerolls_are_disabled(monkeypatch):
    server = FakeServerMessage(signups={"pvp": [1]})
    roll(server, 50, monkeypatch=monkeypatch)

    again = roll(server, monkeypatch=monkeypatch)

    assert again.response.sent[0].startswith("🔒 This item was already rolled, and re-rolls are disabled")
    assert again.response.modals == []


def test_a_reroll_asks_for_a_reason_then_replaces_the_result(monkeypatch):
    bot.save_guild_config(1, loot_reroll_enabled=True)
    server = FakeServerMessage(signups={"pvp": [1, 2]})
    roll(server, 90, 10, monkeypatch=monkeypatch)

    reroll(server, "  wrong   category ", 10, 90, monkeypatch=monkeypatch)

    value = server.field(domain.LOOT_ROLL_FIELD_NAME)
    assert "→ 🏆 <@2>" in value
    assert "Re-rolled by <@8>" in value and "reason: wrong category" in value
    assert "Previous: Main PvP 🏆 <@1> (by <@7>" in value
    assert server.announcements[-1]["content"] == "🎲 Re-roll for **Some Item** — Main PvP: <@2>\nReason: wrong category"


def test_a_reroll_submitted_after_rerolls_were_disabled_is_refused(monkeypatch):
    bot.save_guild_config(1, loot_reroll_enabled=True)
    server = FakeServerMessage(signups={"pvp": [1, 2]})
    roll(server, 90, 10, monkeypatch=monkeypatch)
    before = server.field(domain.LOOT_ROLL_FIELD_NAME)
    interaction = FakeClick(server, 8)
    asyncio.run(press(interaction, "roll_button"))
    modal = interaction.response.modals[0]
    modal.reason._value = "late"
    bot.save_guild_config(1, loot_reroll_enabled=False)

    submit = FakeClick(server, 8)
    asyncio.run(modal.on_submit(submit))

    assert submit.followup.ephemeral == ["🔒 Re-rolls are disabled on this server."]
    assert server.field(domain.LOOT_ROLL_FIELD_NAME) == before


def test_the_reroll_reason_is_required_and_bounded():
    reason = bot.RerollReasonModal().reason

    assert reason.required and reason.min_length == 3 and reason.max_length == 200


# ── Announcement pings ────────────────────────────────────────────────────────

def test_by_default_the_announcement_pings_the_winners_only(monkeypatch):
    server = FakeServerMessage(signups={"pvp": [1, 2], "pve": [4]})

    roll(server, 10, 90, 50, monkeypatch=monkeypatch)

    [announcement] = server.announcements
    assert announcement["content"] == "🎲 Roll for **Some Item** — Main PvP: <@2> · Main PvE: <@4>"
    mentions = announcement["allowed_mentions"]
    assert [u.id for u in mentions.users] == [2, 4]
    assert mentions.everyone is False and mentions.roles is False


def test_the_all_mode_pings_every_participant(monkeypatch):
    bot.save_guild_config(1, loot_roll_ping="all")
    server = FakeServerMessage(signups={"pvp": [1, 2], "pve": [4]})

    roll(server, 10, 90, 50, monkeypatch=monkeypatch)

    [announcement] = server.announcements
    assert announcement["content"].endswith("\nParticipants: <@1> <@2> <@4>")
    assert [u.id for u in announcement["allowed_mentions"].users] == [1, 2, 4]


def test_the_none_mode_posts_no_announcement(monkeypatch):
    bot.save_guild_config(1, loot_roll_ping="none")
    server = FakeServerMessage(signups={"pvp": [1]})

    roll(server, 50, monkeypatch=monkeypatch)

    assert server.announcements == []
    assert server.field(domain.LOOT_ROLL_FIELD_NAME) is not None


def test_a_reason_cannot_ping_everyone(monkeypatch):
    bot.save_guild_config(1, loot_reroll_enabled=True)
    server = FakeServerMessage(signups={"pvp": [1]})
    roll(server, 50, monkeypatch=monkeypatch)

    reroll(server, "@everyone look", 60, monkeypatch=monkeypatch)

    announcement = server.announcements[-1]
    assert "@everyone" not in announcement["content"].replace("@​everyone", "")
    assert announcement["allowed_mentions"].everyone is False


# ── Distribution ──────────────────────────────────────────────────────────────

def test_distributed_preselects_the_top_priority_winner(monkeypatch):
    server = FakeServerMessage(signups={"pve": [4, 5], "greed": [9]})
    roll(server, 12, 87, 50, monkeypatch=monkeypatch)

    interaction = FakeClick(server, 7)
    asyncio.run(press(interaction, "distributed_button"))

    [view] = interaction.response.views
    assert [v.id for v in view.select_recipient.default_values] == [5]
    assert view.roll == server.field(domain.LOOT_ROLL_FIELD_NAME)


def test_distributed_without_a_roll_preselects_nobody():
    server = FakeServerMessage(signups={"pvp": [1]})

    interaction = FakeClick(server, 7)
    asyncio.run(press(interaction, "distributed_button"))

    [view] = interaction.response.views
    assert view.select_recipient.default_values == []
    assert view.roll is None


def test_the_distribution_report_carries_the_roll(monkeypatch):
    sent: list[discord.Embed] = []

    class Channel:
        async def send(self, embed):
            sent.append(embed)

    class Message:
        async def delete(self):
            pass

    monkeypatch.setattr(bot, "fetch_market_for_item", lambda item_id: None)
    interaction = types.SimpleNamespace(
        guild=types.SimpleNamespace(get_channel=lambda channel_id: Channel()),
        user=types.SimpleNamespace(id=7, name="staff", mention="<@7>"),
        response=FakeResponse(),
    )

    async def edit_message(**kwargs):
        pass

    interaction.response.edit_message = edit_message
    recipient = types.SimpleNamespace(id=2, name="winner", mention="<@2>")
    roll_value = "**Main PvP:** <@1> (10) · <@2> (90) → 🏆 <@2>\nRe-rolled by <@8> <t:1:f> — reason: oops"
    modal = bot.LootNoteModal(1, Message(), "Some Item", None, None, None, recipient, roll_value)
    modal.note._value = ""

    asyncio.run(modal.on_submit(interaction))

    [report] = sent
    assert next(f.value for f in report.fields if f.name == domain.LOOT_ROLL_FIELD_NAME) == roll_value


# ── /item-setup ───────────────────────────────────────────────────────────────

def test_item_setup_stores_the_roll_options():
    interaction = types.SimpleNamespace(
        guild_id=1, user=types.SimpleNamespace(id=7, name="admin"), response=FakeResponse()
    )

    asyncio.run(bot.item_setup_command.callback(
        interaction, reroll=True, roll_ping=app_commands.Choice(name="All participants", value="all")
    ))

    config = bot.load_guild_config(1)
    assert config["loot_reroll_enabled"] is True
    assert config["loot_roll_ping"] == "all"
    assert interaction.response.sent == ["✅ Configured: Re-rolls → enabled · Roll announcement pings → All participants"]


def test_item_setup_still_requires_at_least_one_option():
    interaction = types.SimpleNamespace(guild_id=1, user=types.SimpleNamespace(id=7, name="admin"), response=FakeResponse())

    asyncio.run(bot.item_setup_command.callback(interaction))

    assert interaction.response.sent[0].startswith("⚠️ Provide at least one of")

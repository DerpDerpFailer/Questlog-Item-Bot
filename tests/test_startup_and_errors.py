import asyncio
import types

import discord
import pytest
from discord import app_commands

import bot


class FakeResponse:
    def __init__(self, done: bool = False, fail: Exception | None = None):
        self._done = done
        self.fail = fail
        self.sent: list[str] = []

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content=None, **kwargs):
        if self.fail:
            raise self.fail
        self.sent.append(content)


class FakeFollowup:
    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.sent: list[str] = []

    async def send(self, content=None, **kwargs):
        if self.fail:
            raise self.fail
        self.sent.append(content)


def make_interaction(done: bool = False, fail: Exception | None = None, command: str | None = None):
    return types.SimpleNamespace(
        guild_id=1,
        user=types.SimpleNamespace(id=7),
        response=FakeResponse(done=done, fail=fail),
        followup=FakeFollowup(fail=fail),
        command=types.SimpleNamespace(name=command) if command else None,
    )


def http_error(cls: type[discord.HTTPException], status: int) -> discord.HTTPException:
    return cls(types.SimpleNamespace(status=status, reason="x"), "x")


def test_error_uses_the_initial_response_when_the_interaction_is_still_open():
    interaction = make_interaction(done=False)
    asyncio.run(bot.report_interaction_error(interaction, "/x", RuntimeError("boom")))
    assert interaction.response.sent == [bot.GENERIC_ERROR_MESSAGE]
    assert interaction.followup.sent == []


def test_error_uses_a_followup_after_the_interaction_was_already_answered():
    interaction = make_interaction(done=True)
    asyncio.run(bot.report_interaction_error(interaction, "/x", RuntimeError("boom")))
    assert interaction.followup.sent == [bot.GENERIC_ERROR_MESSAGE]
    assert interaction.response.sent == []


def test_forbidden_errors_point_at_bot_permissions():
    interaction = make_interaction()
    asyncio.run(bot.report_interaction_error(interaction, "/x", http_error(discord.Forbidden, 403)))
    assert interaction.response.sent == [bot.FORBIDDEN_ERROR_MESSAGE]


def test_a_failing_error_message_delivery_never_raises():
    interaction = make_interaction(fail=http_error(discord.NotFound, 404))
    asyncio.run(bot.report_interaction_error(interaction, "/x", RuntimeError("boom")))


def test_the_command_tree_error_handler_is_registered_and_unwraps_the_original_error():
    assert bot.tree.on_error is bot.on_tree_error
    interaction = make_interaction(command="wishlist")
    wrapped = app_commands.CommandInvokeError(
        types.SimpleNamespace(name="wishlist"), http_error(discord.Forbidden, 403)
    )
    asyncio.run(bot.tree.on_error(interaction, wrapped))
    assert interaction.response.sent == [bot.FORBIDDEN_ERROR_MESSAGE]


@pytest.mark.parametrize("cls", [bot.LootView, bot.LootDistributeView, bot.WishlistRemoveView, bot.WishlistExportView])
def test_every_view_reports_errors(cls):
    assert issubclass(cls, bot.ErrorReportingView)


def test_the_note_modal_reports_errors():
    assert issubclass(bot.LootNoteModal, bot.ErrorReportingModal)


def test_view_and_modal_errors_reach_the_user():
    async def run():
        view_interaction = make_interaction()
        await bot.LootView().on_error(view_interaction, RuntimeError("boom"), None)
        modal_interaction = make_interaction()
        modal = bot.LootNoteModal(1, None, "Item", None, None, None, None)
        await modal.on_error(modal_interaction, http_error(discord.Forbidden, 403))
        return view_interaction, modal_interaction

    view_interaction, modal_interaction = asyncio.run(run())
    assert view_interaction.response.sent == [bot.GENERIC_ERROR_MESSAGE]
    assert modal_interaction.response.sent == [bot.FORBIDDEN_ERROR_MESSAGE]


def test_setup_hook_survives_a_failing_command_sync(monkeypatch):
    calls: list[str] = []

    async def failing_sync():
        raise RuntimeError("rate limited")

    monkeypatch.setattr(bot.tree, "sync", failing_sync)
    monkeypatch.setattr(bot, "load_stat_formats", lambda: calls.append("stats"))
    monkeypatch.setattr(bot.weekly_wishlist_cleanup, "start", lambda: calls.append("task"))

    asyncio.run(bot.client.setup_hook())

    assert calls == ["stats", "task"]


def test_setup_hook_registers_the_persistent_loot_view_and_starts_the_cleanup_once(monkeypatch):
    started: list[bool] = []

    async def fake_sync():
        return [object()] * 9

    monkeypatch.setattr(bot.tree, "sync", fake_sync)
    monkeypatch.setattr(bot, "load_stat_formats", lambda: None)
    monkeypatch.setattr(bot.weekly_wishlist_cleanup, "start", lambda: started.append(True))

    asyncio.run(bot.client.setup_hook())

    assert started == [True]
    assert any(isinstance(view, bot.LootView) for view in bot.client.persistent_views)


def test_on_ready_no_longer_resyncs_commands(monkeypatch):
    synced: list[bool] = []

    async def fake_sync():
        synced.append(True)
        return []

    monkeypatch.setattr(bot.tree, "sync", fake_sync)
    asyncio.run(bot.on_ready())
    assert synced == []


def test_cleanup_task_waits_for_the_gateway_before_its_first_iteration(monkeypatch):
    waited: list[bool] = []

    async def fake_wait_until_ready():
        waited.append(True)

    monkeypatch.setattr(bot.client, "wait_until_ready", fake_wait_until_ready)
    asyncio.run(bot.wait_for_ready_before_cleanup())

    assert waited == [True]
    assert bot.weekly_wishlist_cleanup._before_loop is bot.wait_for_ready_before_cleanup

import asyncio
import logging
import types

import discord
import pytest
from discord import app_commands
from discord.utils import maybe_coroutine

import bot

ADMIN_ONLY = [bot.item_setup_command, bot.wishlist_setup_command]


class FakeResponse:
    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.sent: list[str] = []

    def is_done(self) -> bool:
        return False

    async def send_message(self, content=None, **kwargs):
        if self.fail:
            raise self.fail
        self.sent.append(content)


def interaction_with(permissions: discord.Permissions, fail: Exception | None = None):
    return types.SimpleNamespace(
        permissions=permissions,
        guild_id=1,
        user=types.SimpleNamespace(id=7, name="member"),
        command=types.SimpleNamespace(name="item-setup"),
        response=FakeResponse(fail=fail),
        followup=types.SimpleNamespace(),
    )


async def run_checks(command, interaction) -> None:
    for predicate in command.checks:
        await maybe_coroutine(predicate, interaction)


def everything_but_administrator() -> discord.Permissions:
    permissions = discord.Permissions.all()
    permissions.administrator = False
    return permissions


@pytest.mark.parametrize("command", ADMIN_ONLY, ids=lambda c: c.name)
def test_an_administrator_passes_the_check(command):
    asyncio.run(run_checks(command, interaction_with(discord.Permissions(administrator=True))))


@pytest.mark.parametrize("command", ADMIN_ONLY, ids=lambda c: c.name)
def test_a_member_with_every_permission_except_administrator_is_refused(command):
    interaction = interaction_with(everything_but_administrator())

    with pytest.raises(app_commands.MissingPermissions) as refused:
        asyncio.run(run_checks(command, interaction))

    assert refused.value.missing_permissions == ["administrator"]


@pytest.mark.parametrize("command", ADMIN_ONLY, ids=lambda c: c.name)
def test_empty_permissions_such_as_outside_a_server_are_refused(command):
    with pytest.raises(app_commands.MissingPermissions):
        asyncio.run(run_checks(command, interaction_with(discord.Permissions.none())))


def test_every_command_that_is_admin_only_by_default_also_enforces_it_in_code():
    declared = [
        command
        for command in bot.tree.get_commands()
        if command.default_permissions is not None and command.default_permissions.administrator
    ]

    assert {command.name for command in declared} == {"item-setup", "wishlist-setup"}
    assert all(command.checks for command in declared)


def test_a_refused_admin_check_replies_clearly_and_logs_one_line_without_a_traceback(caplog):
    interaction = interaction_with(discord.Permissions.none())
    error = app_commands.MissingPermissions(["administrator"])

    with caplog.at_level(logging.INFO):
        asyncio.run(bot.tree.on_error(interaction, error))

    assert interaction.response.sent == [bot.ADMIN_REQUIRED_MESSAGE]
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert "[DENIED] member (7) → /item-setup" in record.getMessage()
    assert "missing: administrator" in record.getMessage()
    assert record.exc_info is None


def test_a_failing_denial_reply_never_raises():
    failure = discord.HTTPException(types.SimpleNamespace(status=404, reason="x"), "x")
    interaction = interaction_with(discord.Permissions.none(), fail=failure)

    asyncio.run(bot.tree.on_error(interaction, app_commands.MissingPermissions(["administrator"])))

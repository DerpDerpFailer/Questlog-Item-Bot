import threading
import time

import pytest
import requests

import bot

OLD = {"old_stat": {"name": "Old"}}
NEW = {"new_stat": {"name": "New"}}


@pytest.fixture(autouse=True)
def stat_state(monkeypatch):
    monkeypatch.setattr(bot, "_stat_formats", dict(OLD))
    monkeypatch.setattr(bot, "_stat_formats_loaded_at", 0.0)
    monkeypatch.setattr(bot, "_stat_formats_attempted_at", 0.0, raising=False)
    monkeypatch.setattr(bot, "_stat_formats_refreshing", False, raising=False)


class Loader:
    """Stands in for bot.load_stat_formats. `block` holds it until `release` is set;
    `update` controls whether it behaves like a successful load or a failed one."""

    def __init__(self, block: bool = False, update: bool = True):
        self.block = block
        self.update = update
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def __call__(self) -> None:
        self.calls += 1
        self.started.set()
        if self.block:
            self.release.wait(timeout=2)
        if self.update:
            bot._stat_formats = dict(NEW)
            bot._stat_formats_loaded_at = time.time()
        self.finished.set()


def wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def settle(loader: Loader) -> None:
    assert loader.finished.wait(timeout=2)
    assert wait_until(lambda: not bot._stat_formats_refreshing)


def test_stale_formats_are_served_immediately_while_a_refresh_runs_in_the_background(monkeypatch):
    loader = Loader(block=True)
    monkeypatch.setattr(bot, "load_stat_formats", loader)

    started_at = time.monotonic()
    result = bot.get_stat_formats()
    elapsed = time.monotonic() - started_at

    assert result == OLD
    assert elapsed < 0.5
    assert loader.started.wait(timeout=2)
    loader.release.set()
    settle(loader)


def test_the_refreshed_formats_are_served_once_the_background_load_is_done(monkeypatch):
    loader = Loader()
    monkeypatch.setattr(bot, "load_stat_formats", loader)

    bot.get_stat_formats()
    settle(loader)

    assert bot.get_stat_formats() == NEW


def test_a_burst_of_calls_starts_a_single_refresh(monkeypatch):
    loader = Loader(block=True)
    monkeypatch.setattr(bot, "load_stat_formats", loader)

    for _ in range(5):
        bot.get_stat_formats()
    assert loader.started.wait(timeout=2)
    loader.release.set()
    settle(loader)

    assert loader.calls == 1


def test_a_failed_refresh_is_retried_later_not_on_every_call(monkeypatch):
    loader = Loader(update=False)
    monkeypatch.setattr(bot, "load_stat_formats", loader)

    bot.get_stat_formats()
    settle(loader)
    bot.get_stat_formats()
    bot.get_stat_formats()
    assert loader.calls == 1

    loader.finished.clear()
    bot._stat_formats_attempted_at = time.time() - bot.STAT_FORMAT_RETRY - 1
    bot.get_stat_formats()
    settle(loader)
    assert loader.calls == 2


def test_fresh_formats_do_not_trigger_any_refresh(monkeypatch):
    loader = Loader()
    monkeypatch.setattr(bot, "load_stat_formats", loader)
    monkeypatch.setattr(bot, "_stat_formats_loaded_at", time.time())

    assert bot.get_stat_formats() == OLD
    assert bot.get_stat_formats() == OLD
    assert loader.calls == 0


def test_load_keeps_the_previous_formats_when_the_api_fails(monkeypatch):
    def failing_get(*args, **kwargs):
        raise requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(bot.requests, "get", failing_get)
    monkeypatch.setattr(bot, "_stat_formats_loaded_at", 123.0)

    bot.load_stat_formats()

    assert bot._stat_formats == OLD
    assert bot._stat_formats_loaded_at == 123.0


def test_format_stat_never_waits_on_the_network(monkeypatch):
    loader = Loader(block=True)
    monkeypatch.setattr(bot, "load_stat_formats", loader)
    monkeypatch.setattr(
        bot, "_stat_formats", {"hp_max": {"name": "Max HP", "multiplier": 1, "valueFormat": "{0}"}}
    )

    started_at = time.monotonic()
    text = bot.format_stat("hp_max", 150)
    elapsed = time.monotonic() - started_at

    assert text == "Max HP: 150"
    assert elapsed < 0.5
    loader.release.set()
    settle(loader)

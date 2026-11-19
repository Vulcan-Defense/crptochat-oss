"""A newer release found on its own: the client asks, shows a badge and checks again later."""

import asyncio

import pytest
from test_tui_features import World, lines, until
from test_tui_features import choose as _choose

from crptochat import crypto, tui, update
from crptochat.store import load_prefs
from crptochat.tui import ChoiceScreen


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CRPTOCHAT_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(crypto, "SCRYPT_N", 2 ** 10)
    return tmp_path


@pytest.fixture
def release(monkeypatch):
    """A fake site with a newer release; apply() only records that it was called."""
    state = {"info": {"version": "9.9.9", "commit": "abc1234", "built": 2_000_000_000, "signed": True},
             "applied": 0, "checks": 0}

    def latest(repo, key=""):
        state["checks"] += 1
        return dict(state["info"])

    def apply(repo, key=""):
        state["applied"] += 1
        return {**state["info"], "path": "/tmp/crptochat"}
    monkeypatch.setattr(update, "latest", latest)
    monkeypatch.setattr(update, "apply", apply)
    monkeypatch.setattr(update, "current", lambda: {"version": "1.16.0", "commit": "0000000", "built": 1})
    monkeypatch.setattr(update, "can_self_update", lambda: True)
    return state


async def choose(pilot, app, ident):
    await pilot.pause(0.1)          # let the dialog finish mounting before picking an option
    await _choose(pilot, app, ident)


def topbar(app):
    return str(app.query_one("#topbar").render())


def test_found_on_its_own_it_asks_and_installs(home, release):
    async def main():
        world = World(home)
        app = await world.start()
        async with app.run_test(size=(140, 40)) as pilot:
            await until(pilot, lambda: app.client.online)
            await until(pilot, lambda: isinstance(app.screen, ChoiceScreen))     # the startup check asked
            assert "9.9.9 DISPONÍVEL" in topbar(app)
            await choose(pilot, app, "now")
            await until(pilot, lambda: release["applied"] == 1)
            await until(pilot, lambda: any("9.9.9 (abc1234) instalado" in line for line in lines(app, tui.CONSOLE)))
            assert "DISPONÍVEL" not in topbar(app)
            await pilot.press("ctrl+q")
        await world.stop()
    asyncio.run(main())


def test_later_asks_again_and_skip_stays_quiet(home, release):
    async def main():
        world = World(home)
        app = await world.start()
        async with app.run_test(size=(140, 40)) as pilot:
            await until(pilot, lambda: app.client.online)
            await until(pilot, lambda: isinstance(app.screen, ChoiceScreen))
            await choose(pilot, app, "later")
            assert release["applied"] == 0 and "9.9.9 DISPONÍVEL" in topbar(app)

            app.periodic_update_check()                     # the 6-hour check: "depois" asks again
            await until(pilot, lambda: isinstance(app.screen, ChoiceScreen))
            await choose(pilot, app, "skip")
            assert load_prefs()["update_skip"] == "9.9.9|abc1234|2000000000"

            before = release["checks"]
            app._update_asked.clear()                       # even in a new run, the skipped one is not offered
            app.periodic_update_check()
            await until(pilot, lambda: release["checks"] > before)
            await pilot.pause(0.2)
            assert not isinstance(app.screen, ChoiceScreen)
            assert "9.9.9 DISPONÍVEL" in topbar(app)        # the badge stays, /update still installs it

            # a newer one than the skipped release is offered again
            release["info"] = {"version": "9.9.10", "commit": "def5678", "built": 2_000_000_100, "signed": True}
            app.periodic_update_check()
            await until(pilot, lambda: isinstance(app.screen, ChoiceScreen))
            await choose(pilot, app, "later")
            await pilot.press("ctrl+q")
        await world.stop()
    asyncio.run(main())


def test_update_off_disables_the_automatic_checks(home, release):
    async def main():
        from crptochat.store import save_prefs
        save_prefs({"update_check": False})
        world = World(home)
        app = await world.start()
        async with app.run_test(size=(140, 40)) as pilot:
            await until(pilot, lambda: app.client.online)
            app.periodic_update_check()
            await pilot.pause(0.3)
            assert release["checks"] == 0 and not isinstance(app.screen, ChoiceScreen)
            assert "DISPONÍVEL" not in topbar(app)
            await pilot.press("ctrl+q")
        await world.stop()
    asyncio.run(main())


def test_the_check_runs_every_six_hours():
    assert tui.UPDATE_EVERY == 6 * 3600

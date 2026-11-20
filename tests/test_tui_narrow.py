"""The interface on narrow screens (a phone in the browser client, a small
terminal): side panels go, the top bar becomes the window list."""

import asyncio

import pytest
from test_tui_features import World, choose, lines, type_line, until

from crptochat import crypto, themes, tui
from crptochat.tui import ChoiceScreen


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CRPTOCHAT_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(crypto, "SCRYPT_N", 2 ** 10)
    yield tmp_path
    tui.set_palette(themes.DEFAULT)


def shown(app, ident):
    return app.query_one(ident).display and app.query_one(ident).region.width > 0


def test_phone_width_keeps_the_chat_and_lists_windows_on_top(home, tmp_path):
    async def main():
        world = World(home)
        app = await world.start(app_nick="neo")
        async with app.run_test(size=(46, 40)) as pilot:
            await until(pilot, lambda: app.client.online)
            assert app.has_class("-narrow") and app.has_class("-compact")
            assert not shown(app, "#buffers") and not shown(app, "#members")
            log = app.query_one("#log")
            assert log.region.width >= 40                       # the chat gets the whole width
            top = str(app.query_one("#topbar").render())
            assert "◈ console" in top and "ON" in top and "☰" in top
            assert "neo →" not in str(app.query_one("#prompt").render())   # prompt without the nick
            assert "toque no topo" in str(app.query_one("#hints").render())

            # a direct message elsewhere shows on the top bar with its count
            bob = await world.peer("bob")
            await bob.send_dm("neo", "oi pelo celular")
            await until(pilot, lambda: "@bob" in app.buffers and app.buffers["@bob"].unread)
            await pilot.pause(0.05)
            assert "@bob 1" in str(app.query_one("#topbar").render())

            # touching the top bar opens the window picker
            await pilot.click("#topbar")
            await until(pilot, lambda: isinstance(app.screen, ChoiceScreen))
            assert app.screen.query_one("#dialog").region.width <= 46        # the dialog fits too
            await pilot.press("escape")
            await until(pilot, lambda: not isinstance(app.screen, ChoiceScreen))
            await pilot.click("#topbar")
            await choose(pilot, app, "@bob")
            assert app.current == "@bob"
            assert any("oi pelo celular" in line and "     bob ▏" in line for line in lines(app))   # short nick column
            top = str(app.query_one("#topbar").render())
            assert top.startswith(" @bob")

            await type_line(pilot, "resposta curta")
            await until(pilot, lambda: any(d.get("text") == "resposta curta" for k, d in bob.events if k == "message"))
            app.save_screenshot(filename="narrow.svg", path=str(tmp_path))
            await type_line(pilot, "/quit")
        await world.stop()
    asyncio.run(main())


def test_layout_follows_the_width(home):
    async def main():
        world = World(home)
        app = await world.start(app_nick="neo")
        async with app.run_test(size=(140, 40)) as pilot:
            await until(pilot, lambda: app.client.online)
            assert not app.has_class("-compact") and shown(app, "#buffers") and shown(app, "#members")
            assert "neo →" in str(app.query_one("#prompt").render())

            await pilot.resize_terminal(90, 40)                  # tablet / small window: no members panel
            await until(pilot, lambda: app.has_class("-compact"))
            await pilot.pause(0.05)
            assert shown(app, "#buffers") and not shown(app, "#members") and not app.has_class("-narrow")

            await pilot.resize_terminal(50, 40)                  # phone
            await until(pilot, lambda: app.has_class("-narrow"))
            await pilot.pause(0.05)
            assert not shown(app, "#buffers") and "☰" in str(app.query_one("#topbar").render())

            await pilot.resize_terminal(140, 40)                 # and back
            await until(pilot, lambda: not app.has_class("-narrow") and not app.has_class("-compact"))
            await pilot.pause(0.05)
            assert shown(app, "#buffers") and shown(app, "#members")
            assert "CRPTOCHAT" in str(app.query_one("#topbar").render())

            # the menu also reaches the windows
            await pilot.press("escape")
            await choose(pilot, app, "windows")
            await choose(pilot, app, tui.CONSOLE)
            assert app.current == tui.CONSOLE
            await type_line(pilot, "/quit")
        await world.stop()
    asyncio.run(main())

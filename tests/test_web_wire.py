"""The browser client's connection pieces that run in plain CPython
(web/py/crptoweb/wire.py) and what the interface shows about TLS there."""

import asyncio
import os
import sys

import pytest
from test_tui_features import World, type_line, until

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web", "py"))
from crptoweb import wire  # noqa: E402

from crptochat import crypto, themes, tui  # noqa: E402
from crptochat.protocol import MAX_LINE  # noqa: E402


@pytest.mark.parametrize("url, expected", [
    ("wss://chat.exemplo.org", ("chat.exemplo.org", 443, True)),
    ("wss://chat.exemplo.org:8443/ws", ("chat.exemplo.org", 8443, True)),
    ("ws://localhost:7778", ("localhost", 7778, False)),
    ("ws://127.0.0.1", ("127.0.0.1", 80, False)),
])
def test_the_url_says_whether_it_is_tls(url, expected):
    assert wire.check_url(url) == expected


@pytest.mark.parametrize("url", ["ws://chat.exemplo.org", "http://x.org", "wss://u:p@x.org", "wss://x.org/?a=1",
                                 "wss://", "chat.exemplo.org"])
def test_bad_urls(url):
    with pytest.raises(ValueError):
        wire.check_url(url)


def test_the_limit_is_in_utf8_bytes_like_the_server():
    assert wire.MAX_LINE == MAX_LINE
    assert wire.frame_bytes('{"t":"ping"}') == b'{"t":"ping"}\n'
    fits = "é" * (MAX_LINE // 2)                  # MAX_LINE bytes, half as many UTF-16 units
    assert len(wire.frame_bytes(fits)) == MAX_LINE + 1
    with pytest.raises(wire.FrameTooBig):
        wire.frame_bytes(fits + "é")               # fewer than MAX_LINE characters, more than MAX_LINE bytes
    with pytest.raises(wire.FrameTooBig):
        wire.frame_bytes("a" * (MAX_LINE + 1))


def test_a_lone_surrogate_is_dropped_not_raised():
    assert wire.frame_bytes('{"t":"msg","x":"\ud800"}') is None
    assert wire.frame_bytes("😀") == "😀".encode() + b"\n"       # a proper pair is fine


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CRPTOCHAT_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(crypto, "SCRYPT_N", 2 ** 10)
    yield tmp_path
    tui.set_palette(themes.DEFAULT)


def test_the_interface_shows_tls_for_the_browsers_wss(home):
    async def main():
        world = World(home)
        app = await world.start(app_nick="neo")
        async with app.run_test(size=(140, 30)) as pilot:
            await until(pilot, lambda: app.client.online)
            assert "SEM TLS" in str(app.query_one("#topbar").render())
            app.client.external_tls = True             # what boot.py passes for a wss:// address
            app.render_topbar()
            top = str(app.query_one("#topbar").render())
            assert "TLS · E2EE" in top and "SEM TLS" not in top
            assert app.client.tls is False              # the client still opens no TLS of its own
            await type_line(pilot, "/quit")
        await world.stop()
    asyncio.run(main())


def test_a_connection_that_does_not_open_says_what_to_check():
    """The browser hides why a WebSocket did not open (403 for a refused origin,
    410, a closed port: all code 1006), so the message lists the causes, with the
    exact --ws-origin the server needs for this page."""
    text = wire.connect_failure("wss://chat.exemplo.org", "https://crptochat.acpt.app.br")
    assert text.startswith("não foi possível conectar em wss://chat.exemplo.org.")
    assert "--ws-origin https://crptochat.acpt.app.br" in text and "403" in text
    assert "--ws-port" in text and "410" in text
    assert "este site não tem servidor de chat" in text


def test_an_invite_link_carries_the_server_in_the_fragment():
    page = "https://crptochat.acpt.app.br/web/"
    link = wire.invite_link(page, "wss://chat.exemplo.org/ws")
    assert link == "https://crptochat.acpt.app.br/web/#servidor=wss://chat.exemplo.org/ws"
    assert wire.invite_link(page + "#velho", "wss://chat.exemplo.org") == page + "#servidor=wss://chat.exemplo.org"
    assert wire.invite_from_fragment(link.split("#", 1)[1]) == "wss://chat.exemplo.org/ws"
    assert wire.invite_from_fragment("#servidor=wss%3A%2F%2Fchat.exemplo.org%3A8443") == "wss://chat.exemplo.org:8443"
    with pytest.raises(ValueError):
        wire.invite_link(page, "ws://chat.exemplo.org")                        # plain ws only to localhost


@pytest.mark.parametrize("fragment", ["", "#", "#outra=coisa", "#servidor=", "#servidor=ws://evil.example",
                                      "#servidor=javascript:alert(1)", "#servidor=https://x.org",
                                      "#servidor=wss://u:p@x.org", "#servidor=wss://x.org/?q=1"])
def test_a_bad_invite_fills_nothing(fragment):
    assert wire.invite_from_fragment(fragment) is None


def test_convite_in_the_terminal_gives_the_command(home):
    async def main():
        world = World(home)
        app = await world.start()
        async with app.run_test(size=(140, 36)) as pilot:
            await until(pilot, lambda: app.client.online)
            await type_line(pilot, "/convite")
            shown = "\n".join(line.plain for line in app.buffers[app.current].lines)
            assert f"crptochat 127.0.0.1:{world.port} --no-tls" in shown           # the test server has no TLS
            app.client.tls, app.client.pin = True, "ab" * 32                     # as with a TLS server, pinned
            await type_line(pilot, "/convite")
            shown = "\n".join(line.plain for line in app.buffers[app.current].lines)
            assert f"crptochat 127.0.0.1:{world.port} --pin {'ab' * 32}" in shown
        await world.stop()
    asyncio.run(main())


def test_convite_in_the_browser_gives_the_link(home, monkeypatch):
    monkeypatch.setattr(tui, "IN_BROWSER", True)
    monkeypatch.setattr(tui, "WEB_INVITE", "https://crptochat.acpt.app.br/web/#servidor=wss://chat.exemplo.org")

    async def main():
        world = World(home)
        app = await world.start()
        async with app.run_test(size=(140, 36)) as pilot:
            await until(pilot, lambda: app.client.online)
            await type_line(pilot, "/convite")
            shown = "\n".join(line.plain for line in app.buffers[app.current].lines)
            assert "https://crptochat.acpt.app.br/web/#servidor=wss://chat.exemplo.org" in shown
            assert "crptochat 127.0.0.1" not in shown
        await world.stop()
    asyncio.run(main())

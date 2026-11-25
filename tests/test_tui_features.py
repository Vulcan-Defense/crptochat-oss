"""The new interface features, driven through the real TUI against a real server."""

import asyncio
import os

import pytest
from textual.widgets import OptionList

from crptochat import crypto, themes, tui
from crptochat.client import ChatClient
from crptochat.server import Server
from crptochat.store import Identity, Store, load_prefs
from crptochat.tui import ChatApp, ChoiceScreen, FormScreen


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CRPTOCHAT_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(crypto, "SCRYPT_N", 2 ** 10)
    yield tmp_path
    tui.set_palette(themes.DEFAULT)


async def until(pilot, cond, timeout=8):
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await pilot.pause(0.02)


def text(widget) -> str:
    return str(widget.render())


def lines(app, name=None):
    return [line.plain for line in app.buffers[name or app.current].lines]


class World:
    """A server, a few scripted peers and the app under test."""

    def __init__(self, home):
        self.home = home
        self.peers = {}

    async def start(self, app_nick="neo", guest=False):
        self.server = Server(str(self.home / "srv"), reg_limit=0)
        self.port = await self.server.start("127.0.0.1", 0)
        ident = Identity("anon", crypto.ed_generate()) if guest else Identity.create(app_nick, "pw")
        self.app = ChatApp(lambda ev: ChatClient("127.0.0.1", self.port, ident, Store(f"127.0.0.1:{self.port}"), ev,
                                                 tls=False, guest=guest))
        return self.app

    async def peer(self, nick):
        events = []
        client = ChatClient("127.0.0.1", self.port, Identity.create(nick, "pw"), Store("p-" + nick),
                            lambda k, d: events.append((k, d)), tls=False)
        client.events = events
        self.peers[nick] = (client, asyncio.create_task(client.run()))
        for _ in range(400):
            if client.online:
                break
            await asyncio.sleep(0.01)
        return client

    async def stop(self):
        for client, task in self.peers.values():
            await client.close()
            task.cancel()
        await self.server.stop()


async def choose(pilot, app, ident):
    await until(pilot, lambda: isinstance(app.screen, ChoiceScreen))
    screen = app.screen
    options = screen.query_one(OptionList)
    options.highlighted = options.get_option_index(ident)
    await pilot.press("enter")
    await until(pilot, lambda: app.screen is not screen)


async def type_line(pilot, line):
    pilot.app.input.value = line
    await pilot.press("enter")
    await pilot.pause(0.05)


def test_themes_preview_apply_and_persist(home):
    async def main():
        world = World(home)
        app = await world.start()
        async with app.run_test(size=(120, 36)) as pilot:
            await until(pilot, lambda: app.client.online)
            await type_line(pilot, "/join #lobby")
            await until(pilot, lambda: app.current == "#lobby")
            await type_line(pilot, "uma linha antes da troca")
            neon = themes.palette("neon")[0]
            assert app.get_css_variables()["cc-bg"] == neon["BG"]

            # menu: arrows preview live, Esc restores the previous theme
            await pilot.press("escape")
            await choose(pilot, app, "theme")
            await until(pilot, lambda: isinstance(app.screen, ChoiceScreen))
            options = app.screen.query_one(OptionList)
            options.highlighted = options.get_option_index("matrix")
            await pilot.pause(0.1)
            assert tui.THEME == "matrix" and tui.CYAN == themes.palette("matrix")[0]["CYAN"]
            await pilot.press("escape")
            await until(pilot, lambda: not app.in_dialog)
            assert tui.THEME == "neon" and "theme" not in load_prefs()

            # applying recolours what is already on screen, the CSS and the prefs
            await pilot.press("escape")
            await choose(pilot, app, "theme")
            await choose(pilot, app, "synthwave")
            synth = themes.palette("synthwave")[0]
            assert tui.THEME == "synthwave" and load_prefs()["theme"] == "synthwave"
            assert app.get_css_variables()["cc-bg"] == synth["BG"]
            old_colours = set(themes.palette("neon")[0].values()) | set(themes.palette("neon")[1])
            for buf in app.buffers.values():
                for line in buf.lines:
                    styles = [str(line.style)] + [str(span.style) for span in line.spans]
                    assert not any(colour in style for style in styles for colour in old_colours), line.plain

            await type_line(pilot, "/theme papel")
            assert tui.THEME == "papel" and not themes.is_dark("papel")
            await type_line(pilot, "/theme inexistente")
            assert any("tema desconhecido" in line for line in lines(app))
            await type_line(pilot, "/quit")

        # a new session starts with the saved theme
        app2 = ChatApp(lambda ev: ChatClient("127.0.0.1", world.port, Identity.create("trinity", "pw"),
                                             Store("x"), ev, tls=False))
        assert tui.THEME == "papel"
        async with app2.run_test(size=(100, 30)) as pilot:
            assert app2.get_css_variables()["cc-bg"] == themes.palette("papel")[0]["BG"]
            await type_line(pilot, "/quit")
        await world.stop()
    asyncio.run(main())


def test_every_theme_is_complete():
    for name in themes.THEMES:
        slots, nicks = themes.palette(name)
        colours = list(slots.values()) + nicks
        assert len(nicks) == 10 and set(slots) == set(themes.SLOTS)
        assert len(set(colours)) == len(colours), name          # needed for live recolouring
        assert all(len(c) == 7 and c.startswith("#") for c in colours)


def test_friends_away_and_roles_in_the_interface(home):
    async def main():
        world = World(home)
        app = await world.start()
        async with app.run_test(size=(130, 40)) as pilot:
            await until(pilot, lambda: app.client.online)
            trinity = await world.peer("trinity")

            # friends: added by command, shown in the sidebar with presence
            await type_line(pilot, "/friend add ghost")
            assert any("não existe" in line for line in lines(app))
            await type_line(pilot, "/friend add Trinity")
            await until(pilot, lambda: app.presence.get("trinity") == "on")
            assert app.client.store.friends() == ["trinity"]
            assert "amigos" in text(app.query_one("#buffers")) and "trinity" in text(app.query_one("#buffers"))
            await trinity.set_away("almoçando")
            await app.poll_presence()
            assert app.presence["trinity"] == "away"
            await trinity.close()
            world.peers["trinity"][1].cancel()
            await until(pilot, lambda: "trinity" not in world.server.sessions)
            await app.poll_presence()
            assert "seu amigo trinity saiu" in " ".join(lines(app, tui.CONSOLE))

            # clicking a friend opens the conversation
            app.switch(tui.CONSOLE)
            row = app.query_one("#buffers").rows.index("@trinity")
            await pilot.click("#buffers", offset=(4, row + 1))
            await until(pilot, lambda: app.current == "@trinity")

            # away: badge, then back
            await type_line(pilot, "/away reunião")
            await until(pilot, lambda: app.client.away)
            assert "AUSENTE" in text(app.query_one("#topbar"))
            await type_line(pilot, "/back")
            await until(pilot, lambda: not app.client.away)

            # roles in the member list and moderation through the menu
            bob = await world.peer("bob")
            await type_line(pilot, "/create #ops")
            await until(pilot, lambda: app.current == "#ops")
            await bob.join("#ops")
            await until(pilot, lambda: "bob" in app.client.channels["#ops"].members)
            members = text(app.query_one("#members"))
            assert "~neo" in members and "bob" in members
            await pilot.press("escape")
            await choose(pilot, app, "mod")
            await choose(pilot, app, "bob")
            await choose(pilot, app, "op")
            await until(pilot, lambda: bob.channels["#ops"].roles.get("bob") == "op")
            await until(pilot, lambda: "@bob" in text(app.query_one("#members")))
            assert any("bob agora é operador" in line for line in lines(app))

            # policies through the menu: toggles and numbers
            await pilot.press("escape")
            await choose(pilot, app, "policy")
            await choose(pilot, app, "noanon")
            await until(pilot, lambda: bob.channels["#ops"].policy.get("noanon"))
            await pilot.press("escape")
            await choose(pilot, app, "policy")
            await choose(pilot, app, "slow")
            await until(pilot, lambda: isinstance(app.screen, FormScreen))
            app.screen.query_one("#f0").value = "15"
            await pilot.press("enter")
            await until(pilot, lambda: bob.channels["#ops"].policy.get("slow") == 15)
            await type_line(pilot, "/policy")
            assert any("slow" in line and "15" in line for line in lines(app))

            # kick through a command, ban through the menu
            await type_line(pilot, "/deop bob")
            await until(pilot, lambda: "bob" not in app.client.channels["#ops"].roles)
            await type_line(pilot, "/kick bob")
            await until(pilot, lambda: "#ops" not in bob.channels)
            await bob.join("#ops")
            await until(pilot, lambda: "bob" in app.client.channels["#ops"].members)
            await pilot.press("escape")
            await choose(pilot, app, "mod")
            await choose(pilot, app, "bob")
            await choose(pilot, app, "ban")
            await until(pilot, lambda: "#ops" not in bob.channels)
            await type_line(pilot, "/bans")
            assert any("bob" in line for line in lines(app))

            # someone without a role is told why
            await type_line(pilot, "/join #lobby")
            await until(pilot, lambda: app.current == "#lobby")
            await type_line(pilot, "/kick bob")
            assert any("operadores" in line for line in lines(app))

            # server staff only
            await pilot.press("escape")
            await choose(pilot, app, "staff")
            assert any("equipe do servidor" in line for line in lines(app))
            world.server.admin({"op": "role", "nick": "neo", "role": "admin"})
            await until(pilot, lambda: app.client.role == "admin")
            assert "ADMIN" in text(app.query_one("#topbar"))
            await type_line(pilot, "/gban bob 7d flood")
            await until(pilot, lambda: any(k == "fatal" for k, _ in bob.events))

            world.server.admin({"op": "notice", "text": "reinício às 22h"})
            await until(pilot, lambda: any("AVISO DO SERVIDOR" in line for line in lines(app, tui.CONSOLE)))
            await type_line(pilot, "/quit")
        await world.stop()
    asyncio.run(main())


def test_anonymous_session_in_the_interface(home):
    async def main():
        world = World(home)
        app = await world.start(guest=True)
        async with app.run_test(size=(120, 36)) as pilot:
            await until(pilot, lambda: app.client.online)
            assert app.client.nick.startswith("anon-") and "ANÔNIMO" in text(app.query_one("#topbar"))
            await type_line(pilot, "/create #meu")
            assert any("anônimos não podem criar" in line for line in lines(app))
            await type_line(pilot, "/friends")
            assert any("nick registrado" in line for line in lines(app))
            await type_line(pilot, "/join #lobby")
            await until(pilot, lambda: app.current == "#lobby")
            await type_line(pilot, "/quit")
        assert not os.listdir(os.path.join(str(home / "cfg"), "identities"))
        await world.stop()
    asyncio.run(main())


def test_a_direct_conversation_with_someone_unchecked_starts_with_a_warning(home):
    """First contact is trust on first use: the key came from the server. The
    conversation says so until the safety numbers are compared (/verify)."""
    async def main():
        world = World(home)
        app = await world.start()
        async with app.run_test(size=(120, 36)) as pilot:
            await until(pilot, lambda: app.client.online)
            await world.peer("bob")
            carol = await world.peer("carol")
            await type_line(pilot, "/msg bob oi")
            await until(pilot, lambda: "@bob" in app.buffers and any("oi" in ln for ln in lines(app, "@bob")))
            warning = [ln for ln in lines(app, "@bob") if "ainda não foi conferido" in ln]
            assert len(warning) == 1 and "/verify bob" in warning[0]
            assert lines(app, "@bob").index(warning[0]) == 0             # the first thing in the conversation

            app.client.store.accept_key("carol", carol.identity.ik, verified=True)   # already checked: no warning
            await type_line(pilot, "/msg carol oi")
            await until(pilot, lambda: "@carol" in app.buffers and any("oi" in ln for ln in lines(app, "@carol")))
            assert not any("ainda não foi conferido" in ln for ln in lines(app, "@carol"))
        await world.stop()
    asyncio.run(main())


def test_the_message_box_grows_with_long_text_and_sends_it_whole(home):
    """A long message wraps inside the box, which grows (up to 5 lines, then it
    scrolls) instead of sliding sideways; Enter sends all of it."""
    async def main():
        world = World(home)
        app = await world.start()
        async with app.run_test(size=(100, 36)) as pilot:
            await until(pilot, lambda: app.client.online)
            await type_line(pilot, "/join #lobby")
            await until(pilot, lambda: app.current == "#lobby")
            box, bar = app.input, app.query_one("#inputbar")
            assert box.size.height == 1 and bar.outer_size.height == 3          # empty: one line, as before

            long = ("a sozinho. Quem está na 1.16 ou anterior instala de novo pelo site. "
                    "Novidade: agora tem cliente web no navegador, sem instalar nada. ") * 2
            box.value = long
            await pilot.pause(0.1)
            assert 2 <= box.size.height <= 5 and bar.outer_size.height == box.size.height + 2   # grew, wrapped
            assert box.wrapped_document.height == box.size.height

            box.value = long * 6                                                  # huge: capped at 5, scrolls
            await pilot.pause(0.1)
            assert box.size.height == 5

            box.value = long
            await pilot.pause(0.05)
            await pilot.press("enter")
            await until(pilot, lambda: any(long.strip()[:40] in ln for ln in lines(app, "#lobby")))
            assert box.value == "" and box.size.height == 1                       # back to one line
            sent = "".join(ln for ln in lines(app, "#lobby") if "a sozinho" in ln or "navegador" in ln)
            assert "sem instalar nada" in sent

            box.value = ""
            box.insert("linha um\nlinha dois\r\nlinha três")                     # a paste with line breaks
            await pilot.pause(0.05)
            assert box.value == "linha um\nlinha dois\nlinha três"                # kept, CR/CRLF normalised

            # up/down move inside a wrapped message; the history only from its first/last row
            box.value = long
            await pilot.pause(0.05)
            rows = box.wrapped_document.height
            assert rows > 1
            await pilot.press("up")
            assert box.value == long                                             # moved up a row, no history
            for _ in range(rows):
                await pilot.press("up")
            assert box.value != long                                             # top row: previous message
        await world.stop()
    asyncio.run(main())


def test_a_burst_of_keys_ending_in_enter_sends_the_whole_line(home):
    """A terminal (or the browser, or a fast typist) delivers "texto\\r" in one
    read: every key is queued before the first is handled. Enter must still come
    after the letters, or the line goes out empty (what the binary smoke caught)."""
    from textual import events

    async def main():
        world = World(home)
        app = await world.start()
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.client.online)
            await type_line(pilot, "/join #lobby")
            await until(pilot, lambda: app.current == "#lobby")
            for ch in "rajada":
                app.post_message(events.Key(ch, ch))
            app.post_message(events.Key("enter", "\r"))
            await until(pilot, lambda: any("rajada" in ln for ln in lines(app, "#lobby")))
            assert app.input.value == ""
        await world.stop()
    asyncio.run(main())


def test_in_the_browser_there_is_no_update_check(home, monkeypatch):
    """The site serves each new version of the browser client: no check on connect,
    and /update explains instead of reaching for the network (Pyodide has no
    direct socket for it anyway)."""
    from crptochat import update

    def no_network(*a, **kw):
        raise AssertionError("o navegador não deve consultar atualizações")
    monkeypatch.setattr(tui, "IN_BROWSER", True)
    monkeypatch.setattr(update, "latest", no_network)
    monkeypatch.setattr(update, "apply", no_network)

    async def main():
        world = World(home)
        app = await world.start()
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.client.online)
            await pilot.pause(0.2)
            app.periodic_update_check()
            await type_line(pilot, "/update")
            await until(pilot, lambda: any("quem atualiza é o site" in ln for ln in lines(app)))
        await world.stop()
    asyncio.run(main())


def test_shift_enter_and_ctrl_j_start_a_new_line_and_enter_sends_it_all(home):
    """Shift+Enter (terminals with the kitty keyboard protocol, and the browser)
    and Ctrl+J (any terminal) break the line; Enter sends the whole message, which
    the other side shows on several lines, under the first."""
    from textual import events

    async def main():
        world = World(home)
        app = await world.start()
        async with app.run_test(size=(110, 36)) as pilot:
            await until(pilot, lambda: app.client.online)
            bob = await world.peer("bob")
            await type_line(pilot, "/join #lobby")
            await bob.join("#lobby")
            await until(pilot, lambda: "bob" in app.client.channels["#lobby"].members)

            for ch in "um":
                await pilot.press(ch)
            await pilot.press("shift+enter")
            for ch in "dois":
                await pilot.press(ch)
            await pilot.press("ctrl+j")
            for ch in "tres":
                await pilot.press(ch)
            assert app.input.value == "um\ndois\ntres"
            await pilot.press("enter")
            await until(pilot, lambda: any(k == "message" and d["text"] == "um\ndois\ntres" for k, d in bob.events))
            assert app.input.value == ""

            # a burst (one terminal read) with the kitty sequence for Shift+Enter in the middle
            for key, ch in [("a", "a"), ("shift+enter", None), ("b", "b"), ("enter", "\r")]:
                app.post_message(events.Key(key, ch))
            await until(pilot, lambda: any(k == "message" and d["text"] == "a\nb" for k, d in bob.events))

            # what bob sends on several lines shows on several lines, aligned under the text
            bob.send_message("#lobby", "primeira\nsegunda\n\n\n\nterceira")
            await until(pilot, lambda: any("primeira" in ln for ln in lines(app, "#lobby")))
            shown = next(ln for ln in lines(app, "#lobby") if "primeira" in ln).split("\n")
            assert [ln.split("▏", 1)[1] for ln in shown] == ["primeira", "segunda", "", "terceira"]   # one blank kept
            assert len({ln.index("▏") for ln in shown}) == 1                               # aligned

            # commands stay on one line
            app.input.value = "/msg bob uma\nlinha só"
            await pilot.press("enter")
            await until(pilot, lambda: any(k == "message" and d["text"] == "uma linha só" for k, d in bob.events))
        await world.stop()
    asyncio.run(main())


def test_multiline_text_is_cleaned_and_bounded():
    clean_ml = tui.clean_multiline
    assert clean_ml("a\r\nb\rc") == "a\nb\nc"
    assert clean_ml("\n\n  x  \n\n\n\ny\n\n") == "  x\n\ny"                         # no flooding with blank lines
    assert clean_ml("ok\x1b[2J\u202edanger\nfim") == "ok[2Jdanger\nfim"              # escapes and bidi still go
    many = clean_ml("\n".join(f"l{i}" for i in range(100)))
    shown = many.split("\n")
    assert len(shown) == tui.MAX_SHOWN_LINES and shown[-1] == "… (+71 linhas)"


def test_a_reply_to_a_multiline_message_quotes_it_on_one_line(home):
    """The quote inside «…» stays on the reply's line: line breaks and the runs of
    spaces they leave become single spaces before it is cut to 40 characters."""
    async def main():
        world = World(home)
        app = await world.start()
        async with app.run_test(size=(120, 36)) as pilot:
            await until(pilot, lambda: app.client.online)
            bob = await world.peer("bob")
            await type_line(pilot, "/join #lobby")
            await bob.join("#lobby")
            await until(pilot, lambda: "bob" in app.client.channels["#lobby"].members)
            bob.send_message("#lobby", "primeira linha\nsegunda\n\n\nterceira linha bem comprida aqui")
            await until(pilot, lambda: any("primeira linha" in ln for ln in lines(app, "#lobby")))
            await type_line(pilot, "/reply bob entendi")
            await until(pilot, lambda: any("entendi" in ln for ln in lines(app, "#lobby")))
            reply = next(ln for ln in lines(app, "#lobby") if "entendi" in ln)
            assert "\n" not in reply
            assert "«primeira linha segunda terceira linha be» entendi" in reply   # 40 chars, single spaces
        await world.stop()
    asyncio.run(main())

"""Notices pinned by the server's administration at the top of #lobby."""

import asyncio
import time

import pytest
from test_e2e import Harness, run, until
from test_tui_features import World, lines, type_line
from test_tui_features import until as ui_until

from crptochat import crypto, tui
from crptochat.client import ChatError


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CRPTOCHAT_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(crypto, "SCRYPT_N", 2 ** 10)
    return tmp_path


def events(client, kind):
    return [d for k, d in client.events if k == kind]


def test_only_admins_pin_and_everyone_gets_them(home):
    async def main():
        async with Harness(home) as h:
            admin, user = await h.client("chefe"), await h.client("bob")
            h.server.db.execute("UPDATE users SET role='admin' WHERE nick_lower='chefe'")
            h.server.sessions["chefe"].role = "admin"
            admin.role = "admin"

            with pytest.raises(ChatError) as err:
                await user.announce("eu também quero")
            assert err.value.code == "denied"

            ident = await admin.announce("o /update agora vem do GitHub", "important")
            await until(lambda: user.announcements)
            item = user.announcements[0]
            assert item["id"] == ident and item["level"] == "important" and item["by"] == "chefe"
            new = [d for d in events(user, "announcements") if d["new"]]
            assert new and new[-1]["new"][0]["text"] == "o /update agora vem do GitHub"

            # someone connecting later receives it with the welcome, without an alert
            late = await h.client("carol")
            await until(lambda: late.announcements)
            first = events(late, "announcements")[0]
            assert first["on_connect"] and first["new"] == [] and len(first["items"]) == 1

            # removed for everyone; the moderator role is not enough
            await admin.announce_del(ident)
            await until(lambda: user.announcements == [] and late.announcements == [])
            h.server.sessions["bob"].role = "mod"
            with pytest.raises(ChatError):
                await user.announce("moderador não fixa recado")
    run(main())


def test_validation_limits_and_expiry(home):
    async def main():
        async with Harness(home) as h:
            server = h.server
            for bad in ("", "   ", "x" * 501):
                with pytest.raises(Exception):
                    server.add_announcement(bad)
            with pytest.raises(Exception):
                server.add_announcement("ok", level="urgente")
            with pytest.raises(Exception):
                server.add_announcement("ok", days=366)
            ident = server.add_announcement("linha 1\n\x1b[31mlinha 2", days=1)["announcement"]
            assert server.announcements()[0]["text"] == "linha 1 [31mlinha 2"          # one line, no escape codes
            for i in range(9):
                server.add_announcement(f"recado {i}")
            with pytest.raises(Exception):
                server.add_announcement("o décimo primeiro")               # at most 10 pinned

            viewer = await h.client("viewer")
            await until(lambda: len(viewer.announcements) == 10)
            server.db.execute("UPDATE announcements SET expires=? WHERE id=?", (int(time.time()) - 1, ident))
            server.db.commit()
            assert server.purge_announcements() == 1
            await until(lambda: len(viewer.announcements) == 9)

            # the console (local socket) pins as "administração"
            reply = server.admin({"op": "announce", "text": "manutenção às 22h", "level": "info", "days": 0})
            assert reply["ok"]
            server.remove_announcement(server.announcements()[0]["id"])
            assert "error" in server.admin({"op": "announce_del", "announcement": 99999})
    run(main())


def test_the_client_does_not_trust_what_the_server_pins(home):
    async def main():
        async with Harness(home) as h:
            c = await h.client("neo")
            c._set_announcements([{"id": "x", "text": "sem id numérico"}, {"id": 1, "text": ""},
                                  {"id": 2, "text": "a" * 5000, "level": "nuclear", "by": "b" * 200},
                                  "lixo"] + [{"id": i, "text": "t"} for i in range(10, 40)])
            assert len(c.announcements) == 17 and all(isinstance(a["id"], int) for a in c.announcements)  # bounded
            big = c.announcements[0]
            assert big["id"] == 2 and len(big["text"]) == 500 and big["level"] == "info" and len(big["by"]) == 40
    run(main())


def test_pinned_at_the_top_of_lobby_in_the_interface(home, tmp_path):
    async def main():
        world = World(home)
        app = await world.start(app_nick="chefe")
        async with app.run_test(size=(140, 40)) as pilot:
            await ui_until(pilot, lambda: app.client.online)
            world.server.db.execute("UPDATE users SET role='admin' WHERE nick_lower='chefe'")
            world.server.sessions["chefe"].role = "admin"
            app.client.role = "admin"
            bob = await world.peer("bob")
            await type_line(pilot, "/join #lobby")
            await ui_until(pilot, lambda: app.current == "#lobby")
            pinned = app.query_one("#pinned")
            assert not pinned.has_class("show")

            await type_line(pilot, "/announce importante O /update agora busca a release do GitHub; atualize!")
            await ui_until(pilot, lambda: pinned.has_class("show"))
            assert pinned.has_class("important")
            assert "IMPORTANTE" in str(pinned.render()) and "release do GitHub" in str(pinned.render())
            assert "RECADOS DA ADMINISTRAÇÃO" in str(pinned.border_title)
            assert any("RECADO IMPORTANTE" in line for line in lines(app, "#lobby"))
            await type_line(pilot, "/announce --dias 3 manutenção sábado às 22h")
            await ui_until(pilot, lambda: len(app.client.announcements) == 2)
            await ui_until(pilot, lambda: "manutenção sábado" in str(pinned.render()))
            await pilot.pause(0.1)
            app.save_screenshot(filename="lobby.svg", path=str(tmp_path))

            # only in #lobby
            app.switch(tui.CONSOLE)
            await pilot.pause(0.05)
            assert not pinned.has_class("show")
            app.switch("#lobby")
            await pilot.pause(0.05)
            assert pinned.has_class("show")

            # someone else got it too; and only admins can pin
            await ui_until(pilot, lambda: len(bob.announcements) == 2)
            ident = app.client.announcements[0]["id"]
            await type_line(pilot, f"/announce del {ident}")
            await ui_until(pilot, lambda: len(app.client.announcements) == 1)
            assert not pinned.has_class("important")
            app.client.role = "user"
            await type_line(pilot, "/announce tentativa")
            assert any("só admins" in line for line in lines(app))
            await type_line(pilot, "/quit")
        await world.stop()
    asyncio.run(main())

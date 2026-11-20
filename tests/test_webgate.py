"""The WebSocket entrance for the browser client: same protocol, same rules."""

import asyncio
import json
import ssl

import pytest

websockets = pytest.importorskip("websockets")
from test_e2e import Harness, messages, run, until  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402
from websockets.exceptions import ConnectionClosed, InvalidStatus  # noqa: E402

from crptochat import client as client_mod  # noqa: E402
from crptochat import crypto  # noqa: E402
from crptochat.protocol import MAX_LINE  # noqa: E402
from crptochat.server import ensure_cert, make_ssl_context  # noqa: E402
from crptochat.webgate import WebGate, check_origin, client_address  # noqa: E402

ORIGIN = "https://app.exemplo.org"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CRPTOCHAT_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(crypto, "SCRYPT_N", 2 ** 10)
    return tmp_path


class WSStream:
    """Reader/writer pair over a WebSocket, standing in for the TCP streams the
    terminal client uses: lets a real ChatClient talk through the gate."""

    def __init__(self, conn):
        self.conn = conn
        self.buf = b""
        self.out: asyncio.Queue = asyncio.Queue()
        self.pump = asyncio.create_task(self._pump())

    async def _pump(self):
        try:
            while True:
                line = await self.out.get()
                if line is None:
                    break
                await self.conn.send(line)
        except ConnectionClosed:
            pass

    async def readline(self):
        try:
            return (await self.conn.recv()).encode() + b"\n"
        except ConnectionClosed:
            return b""

    def write(self, data):
        self.buf += data
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            self.out.put_nowait(line.decode())

    async def drain(self):
        pass

    def get_extra_info(self, name, default=None):
        return ("127.0.0.1", 0) if name in ("peername", "sockname") else default

    def close(self):
        self.out.put_nowait(None)
        asyncio.ensure_future(self.conn.close())

    def is_closing(self):
        return self.conn.state.name in ("CLOSING", "CLOSED")

    async def wait_closed(self):
        await self.conn.wait_closed()


async def gate_for(h, origins=(ORIGIN,), ssl_ctx=None):
    gate = WebGate(h.server, list(origins), ssl_ctx, behind_proxy=ssl_ctx is None)
    port = await gate.start("127.0.0.1", 0)
    return gate, port


def test_a_browser_connection_talks_to_a_terminal_one(home, monkeypatch):
    async def main():
        async with Harness(home) as h:
            gate, ws_port = await gate_for(h)
            real_open = asyncio.open_connection

            async def open_connection(host, port, **kw):
                if port == ws_port:                       # this client goes through the gate
                    s = WSStream(await connect(f"ws://127.0.0.1:{ws_port}", origin=ORIGIN))
                    return s, s
                return await real_open(host, port, **kw)

            monkeypatch.setattr(client_mod.asyncio, "open_connection", open_connection)
            alice = await h.client("alice")                         # terminal, TCP
            h.port, tcp_port = ws_port, h.port
            bob = await h.client("bob")                             # browser, WebSocket
            h.port = tcp_port
            assert alice.online and bob.online

            await alice.send_dm("bob", "oi do terminal")
            await until(lambda: messages(bob, "@alice"))
            await bob.send_dm("alice", "oi do navegador")
            await until(lambda: messages(alice, "@bob"))
            assert messages(bob, "@alice") == [("alice", "oi do terminal")]
            assert messages(alice, "@bob") == [("bob", "oi do navegador")]
            assert not any("terminal" in f or "navegador" in f for f in h.wire)   # still E2E
            await gate.stop()
    run(main())


def test_only_the_configured_origins_connect(home):
    async def main():
        async with Harness(home) as h:
            gate, port = await gate_for(h)
            for origin in ("https://evil.example", None, "https://app.exemplo.org.evil.example"):
                with pytest.raises(InvalidStatus) as err:
                    async with connect(f"ws://127.0.0.1:{port}", origin=origin):
                        pass
                assert err.value.response.status_code == 403
            async with connect(f"ws://127.0.0.1:{port}", origin=ORIGIN) as ws:
                welcome = json.loads(await ws.recv())
                assert welcome["t"] == "welcome" and welcome["nonce"]
                await ws.send(json.dumps({"t": "ping", "id": 7}))
                assert json.loads(await ws.recv()) == {"t": "ok", "id": 7}
                await ws.send(json.dumps({"t": "join", "id": 8, "chan": "#lobby"}))
                assert json.loads(await ws.recv())["code"] == "unauth"     # same rules as TCP
            await gate.stop()
    run(main())


@pytest.mark.parametrize("bad", ["binary", "newline", "huge"])
def test_frames_that_break_the_one_message_one_line_rule_close_the_connection(home, bad):
    async def main():
        async with Harness(home) as h:
            gate, port = await gate_for(h)
            async with connect(f"ws://127.0.0.1:{port}", origin=ORIGIN, max_size=None) as ws:
                await ws.recv()                                             # welcome
                payload = {"binary": b'{"t":"ping","id":1}',
                           "newline": '{"t":"ping","id":1}\n{"t":"ping","id":2}',
                           "huge": '{"t":"ping","x":"' + "a" * MAX_LINE + '"}'}[bad]
                await ws.send(payload)
                with pytest.raises(ConnectionClosed):
                    while True:
                        assert json.loads(await ws.recv()).get("id") is None   # never answered
            await until(lambda: h.server.open_conns == 0)
            await gate.stop()
    run(main())


def test_per_address_limits_use_the_proxy_address(home):
    async def main():
        async with Harness(home) as h:
            h.server.limits.set("conns_per_ip", 1)
            gate, port = await gate_for(h)
            url = f"ws://127.0.0.1:{port}"
            hdr = lambda ip: {"X-Forwarded-For": f"10.9.9.9, {ip}"}    # noqa: E731 - the proxy appends the last one
            async with connect(url, origin=ORIGIN, additional_headers=hdr("198.51.100.7")) as first:
                assert json.loads(await first.recv())["your_ip"] == "198.51.100.7"
                async with connect(url, origin=ORIGIN, additional_headers=hdr("198.51.100.7")) as second:
                    assert json.loads(await second.recv())["code"] == "too_many_conns"
                async with connect(url, origin=ORIGIN, additional_headers=hdr("203.0.113.5")) as other:
                    assert json.loads(await other.recv())["t"] == "welcome"
            await gate.stop()
    run(main())


def test_wss_with_its_own_certificate(home, tmp_path):
    async def main():
        async with Harness(home) as h:
            (tmp_path / "web").mkdir()
            cert, key = ensure_cert(str(tmp_path / "web"))
            gate, port = await gate_for(h, ssl_ctx=make_ssl_context(cert, key))
            ctx = ssl.create_default_context(cafile=cert)
            ctx.check_hostname = False                    # test certificate, issued for "crptochat"
            async with connect(f"wss://127.0.0.1:{port}", origin=ORIGIN, ssl=ctx) as ws:
                assert json.loads(await ws.recv())["t"] == "welcome"
            assert gate.behind_proxy is False
            await gate.stop()
    run(main())


def test_configuration_is_refused_when_unsafe(home):
    async def main():
        async with Harness(home) as h:
            with pytest.raises(ValueError, match="origem"):
                WebGate(h.server, [], None, True)
            plain = WebGate(h.server, [ORIGIN], None, True)
            with pytest.raises(ValueError, match="loopback"):
                await plain.start("0.0.0.0", 0)  # nosec B104 - must be refused
    run(main())


@pytest.mark.parametrize("origin", ["http://app.exemplo.org", "https://app.exemplo.org/", "https://a.org/app",
                                    "*", "", "https://user:pw@a.org", " https://a.org", "file://x"])
def test_bad_origins(origin):
    with pytest.raises(ValueError):
        check_origin(origin)


def test_good_origins():
    for origin in ("https://app.exemplo.org", "https://a.org:8443", "http://localhost:5173", "http://127.0.0.1:8000"):
        assert check_origin(origin) == origin


def test_client_address():
    assert client_address(("127.0.0.1", 5), "1.2.3.4, 5.6.7.8", True) == "5.6.7.8"
    assert client_address(("127.0.0.1", 5), "2001:db8::1", True) == "2001:db8::1"
    assert client_address(("127.0.0.1", 5), "1.2.3.4, lixo", True) == "127.0.0.1"
    assert client_address(("127.0.0.1", 5), None, True) == "127.0.0.1"
    assert client_address(("192.0.2.1", 5), "1.2.3.4", True) == "192.0.2.1"     # only a loopback proxy is trusted
    assert client_address(("127.0.0.1", 5), "1.2.3.4", False) == "127.0.0.1"    # TLS mode: no proxy at all

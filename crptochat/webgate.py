"""WebSocket entrance to the relay, for the browser client.

Browsers cannot open a raw TCP/TLS socket, so the server can also listen for
WebSocket connections. Each WebSocket text message carries exactly one frame of
the usual protocol (the JSON line, without the newline), and the connection is
handed to the same ``Server._handle`` as a TCP one: authentication, rate limits,
bans, queues and everything else are shared. Nothing about the content changes;
the server still only relays end-to-end encrypted blobs.

It is off unless ``--ws-port`` is given, and then:

* only pages from the origins given with ``--ws-origin`` may connect (the
  browser always sends ``Origin``; a missing or different one gets HTTP 403),
  which stops any other site from opening a connection with the visitor's
  browser;
* with ``--ws-cert``/``--ws-key`` it speaks TLS itself (browsers need a
  certificate from a public CA, not the self-signed one the terminal pins);
  without them it speaks plain WebSocket and may only listen on a loopback
  address, behind a reverse proxy that does TLS; the client's address then
  comes from the proxy's ``X-Forwarded-For`` (the last entry, which the proxy
  wrote), so per-address limits and bans keep working;
* frames are limited to the protocol's MAX_LINE, compression is off, and
  binary messages or messages holding a line break close the connection.

Needs the optional dependency ``websockets`` (``pip install crptochat[web]``).
"""

from __future__ import annotations

import ipaddress
import ssl
from typing import TYPE_CHECKING

from .protocol import MAX_LINE

if TYPE_CHECKING:
    from .server import Server

MAX_ORIGINS = 16


class WebSocketUnavailable(RuntimeError):
    pass


def _ws():
    try:
        from websockets.asyncio.server import serve
        from websockets.exceptions import ConnectionClosed
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise WebSocketUnavailable(
            "o acesso por WebSocket precisa do pacote websockets: pip install 'crptochat[web]'") from exc
    return serve, ConnectionClosed


def check_origin(origin: str) -> str:
    """An exact origin: scheme https (or http only for localhost), host, optional port; no path."""
    from urllib.parse import urlsplit
    parts = urlsplit(origin)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if (parts.scheme not in ("https", "http") or (parts.scheme == "http" and not local) or not parts.hostname
            or parts.path not in ("",) or parts.query or parts.fragment or parts.username or parts.password
            or origin != origin.strip() or origin.endswith("/")):
        raise ValueError(f"origem inválida para --ws-origin: {origin!r} (use https://host[:porta], sem barra no fim)")
    return origin


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def client_address(peer: tuple | None, forwarded: str | None, behind_proxy: bool) -> str:
    """The address the limits and bans apply to.

    Behind the proxy (plain WebSocket on loopback), the proxy appends the real
    client to X-Forwarded-For: the last entry is the only one it vouches for.
    Anything that is not a valid IP falls back to the peer, and a peer that is
    not loopback is never trusted to say who the client is."""
    ip = peer[0] if peer else "?"
    if behind_proxy and forwarded and is_loopback(ip):
        last = forwarded.split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(last))
        except ValueError:
            pass
    return ip


class _Reader:
    """``readline()`` over WebSocket messages: one message is one protocol line."""

    def __init__(self, conn, closed_exc):
        self.conn = conn
        self.closed_exc = closed_exc

    async def readline(self) -> bytes:
        try:
            msg = await self.conn.recv()
        except self.closed_exc:
            return b""
        if not isinstance(msg, str):
            await self.conn.close(1003, "só mensagens de texto")
            return b""
        data = msg.encode("utf-8")
        if b"\n" in data or b"\r" in data:
            await self.conn.close(1008, "uma mensagem é um frame, sem quebra de linha")
            return b""
        return data + b"\n"


class _Writer:
    """The ``StreamWriter`` calls Session uses, sending each line as one text message."""

    def __init__(self, conn, address: str, closed_exc):
        self.conn = conn
        self.address = address
        self.closed_exc = closed_exc
        self.pending: list[bytes] = []

    def get_extra_info(self, name, default=None):
        if name == "peername":
            return (self.address, 0)
        return default

    def write(self, data: bytes) -> None:
        self.pending.append(data)

    async def drain(self) -> None:
        pending, self.pending = self.pending, []
        try:
            for data in pending:
                await self.conn.send(data.rstrip(b"\n").decode("ascii"))
        except self.closed_exc as exc:
            raise ConnectionResetError("websocket fechado") from exc

    def close(self) -> None:
        import asyncio
        asyncio.ensure_future(self.conn.close())


class WebGate:
    def __init__(self, server: "Server", origins: list[str], ssl_ctx: ssl.SSLContext | None, behind_proxy: bool):
        if not origins:
            raise ValueError("--ws-port exige ao menos um --ws-origin (a origem da página do cliente web)")
        if len(origins) > MAX_ORIGINS:
            raise ValueError(f"no máximo {MAX_ORIGINS} --ws-origin")
        self.server = server
        self.origins = [check_origin(o) for o in origins]
        self.ssl_ctx = ssl_ctx
        self.behind_proxy = behind_proxy
        self._ws_server = None

    async def start(self, host: str, port: int) -> int:
        if self.ssl_ctx is None and not is_loopback(host):
            raise ValueError("WebSocket sem TLS só escuta em loopback, atrás de um proxy reverso com TLS "
                             "(ou informe --ws-cert e --ws-key)")
        serve, closed_exc = _ws()

        async def handler(conn):
            req = conn.request
            address = client_address(conn.remote_address, req.headers.get("X-Forwarded-For") if req else None,
                                     self.behind_proxy)
            await self.server._handle(_Reader(conn, closed_exc), _Writer(conn, address, closed_exc))
            await conn.close()

        self._ws_server = await serve(
            handler, host, port, ssl=self.ssl_ctx, origins=self.origins,
            max_size=MAX_LINE, max_queue=16, compression=None,
            ping_interval=30, ping_timeout=30, close_timeout=5,
            server_header=None)
        return self._ws_server.sockets[0].getsockname()[1] if self._ws_server.sockets else port

    async def stop(self) -> None:
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()

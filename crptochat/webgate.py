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
  address, behind a reverse proxy that does TLS, unless ``--ws-behind-proxy``
  opts in to plain WebSocket on a non-loopback bind inside a private network;
  with a trusted reverse proxy (``--ws-trusted-proxy``), the client's address
  comes from ``X-Real-IP`` or the last entry of ``X-Forwarded-For``;
* frames are limited to the protocol's MAX_LINE, compression is off, and
  binary messages or messages holding a line break close the connection.

Needs the optional dependency ``websockets`` (``pip install crptochat[web]``).
"""

from __future__ import annotations

import ipaddress
import ssl
from urllib.parse import quote, urlsplit
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


INVITE_PAGE = "/web/"      # where an origin serves the browser client (as crptochat.acpt.app.br does)


def check_public_url(url: str) -> str:
    """The public address of this WebSocket entrance, as people type it on the
    page: wss:// (ws:// only for this machine), host, optional port and path, and
    nothing else. Same rules as the page's own check (web/py/crptoweb/wire.py)."""
    parts = urlsplit(url)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme != "wss" and not (parts.scheme == "ws" and local):
        raise ValueError("--ws-public-url precisa ser wss:// (ws:// só para localhost)")
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("--ws-public-url inválido")
    return url


def invite_links(origins: list[str], public_url: str) -> list[str]:
    """One invite per accepted origin: the browser client's page with this server
    filled in. The address goes after #, which the browser never sends to the site;
    the page fills the field and connects only when the person presses enter."""
    url = check_public_url(public_url)
    return [f"{origin}{INVITE_PAGE}#servidor={quote(url, safe=':/')}" for origin in origins]


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


def parse_trusted_proxy(spec: str):
    """A host address or CIDR accepted as a reverse proxy for X-Forwarded-For."""
    spec = spec.strip()
    if not spec:
        raise ValueError("endereço vazio em --ws-trusted-proxy")
    try:
        if "/" in spec:
            return ipaddress.ip_network(spec, strict=False)
        addr = ipaddress.ip_address(spec)
        return ipaddress.ip_network(f"{addr}/{addr.max_prefixlen}", strict=False)
    except ValueError as exc:
        raise ValueError(f"--ws-trusted-proxy inválido: {spec!r}") from exc


def peer_trusted(peer: tuple | None, networks: tuple) -> bool:
    if not peer or not networks:
        return False
    try:
        addr = ipaddress.ip_address(peer[0])
    except ValueError:
        return False
    return any(addr in net for net in networks)


def client_address(peer: tuple | None, forwarded: str | None, real_ip: str | None,
                   *, behind_loopback: bool, trusted_networks: tuple) -> str:
    """The address the limits and bans apply to.

    Behind a loopback proxy, the proxy appends the real client to
    X-Forwarded-For: the last entry is the only one it vouches for.
    With ``--ws-behind-proxy``, the peer must fall in ``--ws-trusted-proxy``
    before any forwarded header is believed."""
    ip = peer[0] if peer else "?"
    use_forwarded = False
    if behind_loopback and is_loopback(ip):
        use_forwarded = True
    elif peer_trusted(peer, trusted_networks):
        use_forwarded = True
    if not use_forwarded:
        return ip
    if real_ip:
        try:
            return str(ipaddress.ip_address(real_ip.strip()))
        except ValueError:
            pass
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(last))
        except ValueError:
            pass
    return ip


def validate_plain_ws_bind(host: str, plain_remote: bool, ssl_ctx: ssl.SSLContext | None) -> None:
    if ssl_ctx is not None:
        return
    if not is_loopback(host) and not plain_remote:
        raise ValueError("WebSocket sem TLS só escuta em loopback, atrás de um proxy reverso com TLS "
                         "(use --ws-behind-proxy para escutar em rede privada com proxy de confiança, "
                         "ou informe --ws-cert e --ws-key)")


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
    def __init__(self, server: "Server", origins: list[str], ssl_ctx: ssl.SSLContext | None,
                 *, plain_remote: bool, trusted_proxies: list[str]):
        if not origins:
            raise ValueError("--ws-port exige ao menos um --ws-origin (a origem da página do cliente web)")
        if len(origins) > MAX_ORIGINS:
            raise ValueError(f"no máximo {MAX_ORIGINS} --ws-origin")
        if plain_remote and ssl_ctx is not None:
            raise ValueError("--ws-behind-proxy não combina com --ws-cert/--ws-key")
        self.server = server
        self.origins = [check_origin(o) for o in origins]
        self.ssl_ctx = ssl_ctx
        self.plain_remote = plain_remote
        self.behind_loopback = ssl_ctx is None
        self._trusted = tuple(parse_trusted_proxy(s) for s in trusted_proxies)
        self._ws_server = None

    async def start(self, host: str, port: int) -> int:
        validate_plain_ws_bind(host, self.plain_remote, self.ssl_ctx)
        serve, closed_exc = _ws()

        async def handler(conn):
            req = conn.request
            headers = req.headers if req else {}
            address = client_address(
                conn.remote_address, headers.get("X-Forwarded-For"), headers.get("X-Real-IP"),
                behind_loopback=self.behind_loopback, trusted_networks=self._trusted)
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
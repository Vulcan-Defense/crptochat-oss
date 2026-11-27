"""The connection to the server over the browser's WebSocket.

The client code reads and writes JSON lines through an asyncio reader/writer
pair; this offers the same calls over a WebSocket, one message per line, the
way the server's WebSocket entrance (crptochat/webgate.py) expects. TLS is the
browser's: a wss:// URL is checked against the public CAs the browser trusts.
"""

from __future__ import annotations

import asyncio

from pyodide.ffi import create_proxy
from js import WebSocket, console

from .wire import FrameTooBig, connect_failure, frame_bytes


def _page_origin() -> str:
    try:
        from js import location
        return str(location.origin)
    except Exception:              # no page (smoke test under Node)
        return "a origem desta página"


class WebSocketStream:
    def __init__(self, ws, url: str = "") -> None:
        self.ws = ws
        self.url = url
        self.inbox: asyncio.Queue[bytes] = asyncio.Queue()
        self.buf = b""
        self.closed = False
        self.opened = asyncio.get_running_loop().create_future()
        # every handler is in place before the socket can deliver anything: the
        # server greets right after the handshake, before this coroutine resumes
        self._proxies = [create_proxy(self._on_open), create_proxy(self._on_message), create_proxy(self._on_close)]
        ws.onopen, ws.onmessage, ws.onclose = self._proxies
        ws.onerror = self._proxies[2]

    @classmethod
    async def open(cls, url: str, timeout: float = 15) -> "WebSocketStream":
        stream = cls(WebSocket.new(url), url)
        try:
            await asyncio.wait_for(asyncio.shield(stream.opened), timeout)
        except BaseException:
            stream.close()
            raise
        return stream

    def _on_open(self, _event) -> None:
        if not self.opened.done():
            self.opened.set_result(True)

    # -------------------------------------------------------- JS callbacks

    def _on_message(self, event) -> None:
        # runs inside a JS callback: nothing here may raise
        data = event.data
        if not isinstance(data, str):
            self.close()                      # binary: not this protocol
            return
        try:
            line = frame_bytes(data)
        except FrameTooBig:
            console.warn("crptochat: frame do servidor acima do limite do protocolo; conexão encerrada")
            self.close()
            return
        if line is None:
            console.warn("crptochat: frame do servidor com texto inválido (não é UTF-8); descartado")
            return
        self.inbox.put_nowait(line)

    def _on_close(self, _event) -> None:
        if not self.opened.done():
            self.opened.set_exception(ConnectionError(connect_failure(self.url or "o servidor", _page_origin())))
        if not self.closed:
            self.closed = True
            self.inbox.put_nowait(b"")
            asyncio.get_running_loop().call_soon(self._release)    # not from inside its own callback

    def _release(self) -> None:
        self.ws.onopen = self.ws.onmessage = self.ws.onclose = self.ws.onerror = None
        for proxy in self._proxies:
            proxy.destroy()
        self._proxies = []

    # ------------------------------------------- asyncio.StreamReader side

    async def readline(self) -> bytes:
        if self.closed and self.inbox.empty():
            return b""
        return await self.inbox.get()

    # ------------------------------------------- asyncio.StreamWriter side

    def write(self, data: bytes) -> None:
        self.buf += data
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            if not self.closed:
                self.ws.send(line.decode("utf-8"))

    async def drain(self) -> None:
        if self.closed:
            raise ConnectionResetError("conexão encerrada")

    def get_extra_info(self, name, default=None):
        return default

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.ws.close()
            self.inbox.put_nowait(b"")
            asyncio.get_running_loop().call_soon(self._release)

    async def wait_closed(self) -> None:
        return None

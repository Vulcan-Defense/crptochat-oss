"""The parts of the browser's connection that need neither JS nor Pyodide
(tested in plain CPython): the server URL and turning a WebSocket text message
back into a protocol line."""

from __future__ import annotations

from urllib.parse import urlsplit

MAX_LINE = 256 * 1024      # the protocol's limit for one frame, in bytes (crptochat.protocol.MAX_LINE)
LOCAL = ("localhost", "127.0.0.1", "::1")


class FrameTooBig(ValueError):
    pass


def check_url(url: str) -> tuple[str, int, bool]:
    """(host, port, tls) of a server address. wss:// is TLS done by the browser,
    against the public CAs it trusts (no pinning, unlike the terminal's TOFU);
    plain ws:// only for this machine."""
    parts = urlsplit(url)
    local = parts.hostname in LOCAL
    if parts.scheme != "wss" and not (parts.scheme == "ws" and local):
        raise ValueError("use wss:// (ws:// só para localhost)")
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("endereço do servidor inválido")
    tls = parts.scheme == "wss"
    return parts.hostname, parts.port or (443 if tls else 80), tls


def frame_bytes(text: str) -> bytes | None:
    """The line for one WebSocket text message.

    The size is checked in UTF-8 bytes, as on the server (a JS string's length
    counts UTF-16 units). None for text that cannot be UTF-8 (a lone surrogate
    from a broken peer): the caller drops that message and goes on. Raises
    FrameTooBig above the protocol's limit."""
    if len(text) > MAX_LINE:                 # no UTF-16 unit is under 1 byte: too big already
        raise FrameTooBig(len(text))
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(data) > MAX_LINE:
        raise FrameTooBig(len(data))
    return data + b"\n"


def connect_failure(url: str, page_origin: str) -> str:
    """What to say when the WebSocket to the server does not open.

    The browser hides why (a 403 for an origin the server does not accept, a 410,
    a refused port, a bad certificate: all the same close, code 1006, so a page
    cannot use WebSocket to probe a network); so this lists what to check, the
    origin the server must accept first among them."""
    return (f"não foi possível conectar em {url}. O navegador não informa o motivo; confira:\n"
            "• o endereço: o wss:// do servidor que você usa (este site não tem servidor de chat);\n"
            "• se o servidor está no ar e com a entrada WebSocket ligada (--ws-port);\n"
            f"• se ele aceita esta página: --ws-origin {page_origin} (sem isso ele recusa com 403);\n"
            "• um servidor desativado (410) ou um proxy fora do ar aparecem igual.")

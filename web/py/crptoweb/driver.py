"""Textual driver for xterm.js: output goes to the terminal in the page, keys,
mouse and paste come back from it as the same escape sequences a real terminal
sends, parsed by Textual's own XTermParser."""

from __future__ import annotations

import asyncio

from textual import events
from textual._xterm_parser import XTermParser
from textual.driver import Driver
from textual.geometry import Size

# set by boot.py before the app starts: an object with write(str), size() -> [cols, rows],
# and set_handlers(on_data, on_resize)
TERMINAL = None


class XtermDriver(Driver):
    def __init__(self, app, *, debug: bool = False, mouse: bool = True, size=None) -> None:
        super().__init__(app, debug=debug, mouse=mouse, size=size)
        self._out: list[str] = []
        self._parser = XTermParser(debug)
        self._tick_handle: asyncio.TimerHandle | None = None
        self._input = True

    # ------------------------------------------------------------- output

    def write(self, data: str) -> None:
        self._out.append(data)

    def flush(self) -> None:
        if self._out:
            data, self._out = "".join(self._out), []
            TERMINAL.write(data)

    # -------------------------------------------------------------- input

    def _feed(self, text: str) -> None:
        if not self._input:
            return
        for event in self._parser.feed(text):
            self.process_message(event)
        # a lone ESC is only known to be the Escape key once nothing follows it
        if self._tick_handle:
            self._tick_handle.cancel()
        self._tick_handle = self._loop.call_later(0.1, self._tick)

    def _tick(self) -> None:
        self._tick_handle = None
        for event in self._parser.tick():
            self.process_message(event)

    def _resize(self, cols: int, rows: int) -> None:
        size = Size(int(cols), int(rows))
        self.process_message(events.Resize(size, size))

    # ---------------------------------------------------------- lifecycle

    def start_application_mode(self) -> None:
        self.write("\x1b[?1049h")          # alternate screen
        self.write("\x1b[?25l")            # hide the cursor
        self.write("\x1b[?7l")             # no line wrap
        if self._mouse:
            self.write("\x1b[?1000h\x1b[?1003h\x1b[?1015h\x1b[?1006h")
        self.write("\x1b[?2004h")          # bracketed paste
        self.flush()
        from pyodide.ffi import create_proxy      # JS keeps these callbacks after this call returns
        self._proxies = [create_proxy(self._feed), create_proxy(self._resize)]
        TERMINAL.set_handlers(*self._proxies)
        cols, rows = TERMINAL.size()
        self._resize(cols, rows)

    def disable_input(self) -> None:
        self._input = False

    def stop_application_mode(self) -> None:
        self._input = False
        if self._mouse:
            self.write("\x1b[?1000l\x1b[?1003l\x1b[?1015l\x1b[?1006l")
        self.write("\x1b[?2004l\x1b[?7h\x1b[?25h\x1b[?1049l")
        self.flush()
        TERMINAL.set_handlers(None, None)
        for proxy in getattr(self, "_proxies", []):
            proxy.destroy()
        self._proxies = []

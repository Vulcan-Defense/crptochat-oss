"""The crptochat terminal client running inside the browser (Pyodide).

The chat code is the same package the terminal uses; this only supplies what a
browser lacks: a Textual driver that draws into xterm.js (driver.py), the
connection to the server over WebSocket instead of TCP (net.py), and the start
up that the command line does on a computer (boot.py).
"""

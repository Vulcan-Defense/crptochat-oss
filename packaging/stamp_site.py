#!/usr/bin/env python3
"""Substitui __VERSION__ e __PIN__ nas páginas estáticas do site."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_RE = re.compile(r'^__version__ = "(\d+(?:\.\d+){1,3})"$', re.M)


def read_version(init_py: Path) -> str:
    text = init_py.read_text(encoding="utf-8")
    m = VERSION_RE.search(text)
    if not m:
        raise SystemExit(f"versão não encontrada em {init_py}")
    return m.group(1)


def stamp_file(path: Path, *, version: str | None = None, pin: str | None = None) -> bool:
    text = path.read_text(encoding="utf-8")
    changed = False
    if version is not None and "__VERSION__" in text:
        text = text.replace("__VERSION__", version)
        changed = True
    if pin is not None and "__PIN__" in text:
        text = text.replace("__PIN__", pin)
        changed = True
    if changed:
        path.write_text(text, encoding="utf-8")
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--init", type=Path, default=ROOT / "crptochat" / "__init__.py")
    ap.add_argument("--version", help="versão (padrão: crptochat.__version__)")
    ap.add_argument("--pin", help="fingerprint TLS (SHA-256 hex) para __PIN__")
    ap.add_argument("paths", nargs="*", type=Path,
                    default=[ROOT / "site" / "downloads" / "index.html"])
    args = ap.parse_args(argv)
    version = args.version or read_version(args.init)
    changed = 0
    for path in args.paths:
        if not path.is_file():
            print(f"::error::arquivo ausente: {path}", file=sys.stderr)
            return 1
        if stamp_file(path, version=version, pin=args.pin):
            changed += 1
            extra = f", pin" if args.pin else ""
            print(f"atualizado {path} -> v{version}{extra}")
        elif f"v{version}" not in path.read_text(encoding="utf-8"):
            if "__VERSION__" in path.read_text(encoding="utf-8"):
                print(f"::error::{path} ainda tem __VERSION__", file=sys.stderr)
                return 1
    if not changed:
        print(f"nada a substituir (já v{version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
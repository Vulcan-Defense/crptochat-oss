#!/usr/bin/env python3
"""Substitui __VERSION__ nas páginas estáticas do site pela versão do pacote."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_RE = re.compile(r'^__version__ = "(\d+(?:\.\d+){1,3})"$', re.M)
PLACEHOLDER = "__VERSION__"


def read_version(init_py: Path) -> str:
    text = init_py.read_text(encoding="utf-8")
    m = VERSION_RE.search(text)
    if not m:
        raise SystemExit(f"versão não encontrada em {init_py}")
    return m.group(1)


def stamp_file(path: Path, version: str) -> bool:
    text = path.read_text(encoding="utf-8")
    if PLACEHOLDER not in text:
        return False
    path.write_text(text.replace(PLACEHOLDER, version), encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--init", type=Path, default=ROOT / "crptochat" / "__init__.py")
    ap.add_argument("paths", nargs="*", type=Path,
                    default=[ROOT / "site" / "downloads" / "index.html"])
    args = ap.parse_args(argv)
    version = read_version(args.init)
    changed = 0
    for path in args.paths:
        if not path.is_file():
            print(f"::error::arquivo ausente: {path}", file=sys.stderr)
            return 1
        if stamp_file(path, version):
            changed += 1
            print(f"atualizado {path} -> v{version}")
        elif f"v{version}" not in path.read_text(encoding="utf-8"):
            print(f"::error::{path} não tem {PLACEHOLDER} nem v{version}", file=sys.stderr)
            return 1
    if not changed:
        print(f"nada a substituir (já v{version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
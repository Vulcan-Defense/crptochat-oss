#!/usr/bin/env python3
"""Fails if crptochat/__init__.py has a lower version than at REF.

    python packaging/check_version.py REF

Used by CI against the base branch of a pull request and against the previous
commit of a push, so a merge (or a conflict resolution) can never take the
version back, e.g. 1.21.0 -> 1.20.1 when two PRs bumped it.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404 - runs git with fixed arguments
import sys

VERSION_RE = re.compile(r'^__version__ = "(\d+(?:\.\d+){1,3})"$', re.M)


def parse(text: str) -> tuple[int, ...]:
    m = VERSION_RE.search(text)
    if not m:
        raise ValueError("__version__ não encontrado")
    return tuple(int(p) for p in m.group(1).split("."))


def check(current: str, previous: str) -> str | None:
    """None if fine, else why not."""
    now, before = parse(current), parse(previous)
    if now < before:
        return (f"a versão voltou atrás: {'.'.join(map(str, before))} -> {'.'.join(map(str, now))} "
                "(numa resolução de conflito, fique com a maior)")
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    with open("crptochat/__init__.py") as fh:
        current = fh.read()
    previous = subprocess.run(["git", "show", f"{argv[0]}:crptochat/__init__.py"],  # nosec B603 B607
                              check=True, capture_output=True, text=True).stdout
    problem = check(current, previous)
    if problem:
        print(f"::error::{problem}")
        return 1
    print(f"versão ok: {'.'.join(map(str, parse(previous)))} -> {'.'.join(map(str, parse(current)))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

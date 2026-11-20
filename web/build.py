#!/usr/bin/env python3
"""Build web/dist/: the static site of the browser client.

    python3 web/fetch_vendor.py      # once: downloads and verifies vendor/
    python3 web/build.py             # page + app.zip (crptochat + crptoweb) + vendor/

app.zip holds the crptochat package from this repository (the same code as the
terminal client) and web/py/crptoweb; the zip is deterministic (fixed dates and
order), so the same commit builds the same bytes. dist/SHA256SUMS lists every
file, for comparing what a host serves with what was built.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIST = os.path.join(HERE, "dist")
FIXED = (1980, 1, 1, 0, 0, 0)
SKIP_MODULES = {"server.py", "webgate.py", "admin.py", "dash.py", "stats.py", "selftest.py"}   # server side


def app_zip(path: str) -> None:
    entries = []
    for base, pkg in ((ROOT, "crptochat"), (os.path.join(HERE, "py"), "crptoweb")):
        for name in sorted(os.listdir(os.path.join(base, pkg))):
            if name.endswith(".py") and not (pkg == "crptochat" and name in SKIP_MODULES):
                entries.append((f"{pkg}/{name}", os.path.join(base, pkg, name)))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for arc, src in entries:
            info = zipfile.ZipInfo(arc, FIXED)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(src, "rb") as fh:
                zf.writestr(info, fh.read())


def main() -> int:
    if not os.path.isfile(os.path.join(HERE, "vendor", "manifest.json")):
        print("falta web/vendor/: rode antes python3 web/fetch_vendor.py", file=sys.stderr)
        return 1
    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    shutil.copytree(os.path.join(HERE, "src"), DIST)
    shutil.copytree(os.path.join(HERE, "vendor"), os.path.join(DIST, "vendor"))
    app_zip(os.path.join(DIST, "app.zip"))
    sums = []
    for base, _dirs, files in os.walk(DIST):
        for name in files:
            path = os.path.join(base, name)
            with open(path, "rb") as fh:
                sums.append(f"{hashlib.sha256(fh.read()).hexdigest()}  {os.path.relpath(path, DIST)}")
    with open(os.path.join(DIST, "SHA256SUMS"), "w") as fh:
        fh.write("\n".join(sorted(sums, key=lambda s: s.split("  ")[1])) + "\n")
    total = sum(os.path.getsize(os.path.join(b, f)) for b, _d, fs in os.walk(DIST) for f in fs)
    print(f"web/dist/ pronto: {len(sums)} arquivos, {total / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())

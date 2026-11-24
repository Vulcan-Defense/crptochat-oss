#!/usr/bin/env python3
"""Build rápido para desenvolvimento em web/dist-dev/ (sem Node, sem pré-compilação).

    python3 web/build_dev.py
    python3 -m http.server 8124 --bind 127.0.0.1 --directory web/dist-dev

Serve para iterar no cliente web sem rodar o Node/precompile.mjs: o Python
chega em fonte, a stdlib vai inteira e o resultado é maior e mais lento que o
build definitivo. Não é reprodutível e não deve ser publicado. O build de
produção é web/build.py, em web/dist/; este script nunca apaga nem substitui
essa pasta.

Requer apenas web/vendor/ (python3 web/fetch_vendor.py, uma vez).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEV = os.path.join(HERE, "dist-dev")
FIXED = (1980, 1, 1, 0, 0, 0)
SKIP_MODULES = {"server.py", "webgate.py", "admin.py", "dash.py", "stats.py", "selftest.py"}  # server side


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def zipped(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, data in entries:
            info = zipfile.ZipInfo(name, FIXED)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return buf.getvalue()


def gz(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=9, mtime=0)


def source_zips() -> tuple[bytes, bytes]:
    """site.zip.gz com as wheels em fonte e o app; later.zip.gz com Pygments.

    Os caminhos das wheels são os mesmos em que o Pyodide instala pacotes
    puros, então o zip pode ser desembrulhado em '/' como no build definitivo.
    """
    manifest_path = os.path.join(HERE, "vendor", "manifest.json")
    lock_path = os.path.join(HERE, "vendor", "pyodide", "pyodide-lock.json")
    stdlib_path = os.path.join(HERE, "vendor", "pyodide", "python_stdlib.zip")
    for path in (manifest_path, lock_path, stdlib_path):
        if not os.path.isfile(path):
            fail(f"falta {path}: rode antes python3 web/fetch_vendor.py")
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    with open(lock_path, encoding="utf-8") as fh:
        lock = json.load(fh)
    major, minor = lock["info"]["python"].split(".")[:2]
    site_prefix = f"lib/python{major}.{minor}/site-packages/"

    site, later = [], []
    wheels_dir = os.path.join(HERE, "vendor", "wheels")
    for wheel in manifest["wheels"]:
        with zipfile.ZipFile(os.path.join(wheels_dir, wheel)) as wf:
            for item in wf.infolist():
                name = item.filename
                if item.is_dir() or ".dist-info" in name or name.endswith(".pyc"):
                    continue
                data = wf.read(item)
                # Pygments só coloriza código: vai no zip carregado depois
                (later if name.startswith("pygments/") else site).append(
                    (name if name.startswith("pygments/") else site_prefix + name, data))
    for base, pkg in ((ROOT, "crptochat"), (os.path.join(HERE, "py"), "crptoweb")):
        for name in sorted(os.listdir(os.path.join(base, pkg))):
            if name.endswith(".py") and not (pkg == "crptochat" and name in SKIP_MODULES):
                with open(os.path.join(base, pkg, name), "rb") as fh:
                    site.append((f"app/{pkg}/{name}", fh.read()))
    return gz(zipped(site)), gz(zipped(later))


def build_id(stage: str) -> tuple[str, list[str]]:
    """Mesmo esquema do build.py: um id por conteúdo, para o service worker
    trocar de build quando o código muda."""
    files = sorted(os.path.relpath(os.path.join(base, name), stage).replace(os.sep, "/")
                   for base, _dirs, names in os.walk(stage) for name in names)
    digest = hashlib.sha256()
    for rel in files:
        with open(os.path.join(stage, rel), "rb") as fh:
            digest.update(rel.encode() + b"\0" + hashlib.sha256(fh.read()).digest())
    return digest.hexdigest()[:16], files


def main() -> int:
    site_zip, later_zip = source_zips()          # falha antes de apagar qualquer coisa
    if os.path.isdir(DEV):
        shutil.rmtree(DEV)                       # só web/dist-dev/, nunca web/dist/
    stage = os.path.join(DEV, "b", "_")
    shutil.copytree(os.path.join(HERE, "src"), stage,
                    ignore=shutil.ignore_patterns("index.html", "sw.js"))
    shutil.copytree(os.path.join(HERE, "vendor"), os.path.join(stage, "vendor"))
    py_dir = os.path.join(stage, "py")
    os.makedirs(py_dir, exist_ok=True)
    shutil.copy2(os.path.join(HERE, "vendor", "pyodide", "python_stdlib.zip"),
                 os.path.join(py_dir, "python_stdlib.zip"))
    with open(os.path.join(py_dir, "site.zip.gz"), "wb") as fh:
        fh.write(site_zip)
    # A stdlib completa já vem no boot, então a parte "restante" é vazia.
    with open(os.path.join(py_dir, "stdlib-rest.zip.gz"), "wb") as fh:
        fh.write(gz(zipped([])))
    with open(os.path.join(py_dir, "later.zip.gz"), "wb") as fh:
        fh.write(later_zip)

    ident, files = build_id(stage)
    base = f"b/{ident}"
    os.rename(stage, os.path.join(DEV, base))

    with open(os.path.join(HERE, "src", "index.html"), encoding="utf-8") as fh:
        page = fh.read()
    if page.count("__BUILD_DIR__") < 3:
        fail("src/index.html sem __BUILD_DIR__ nos links")
    with open(os.path.join(DEV, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(page.replace("__BUILD_DIR__", base))
    with open(os.path.join(HERE, "src", "sw.js"), encoding="utf-8") as fh:
        sw = fh.read()
    cached = ["index.html"] + [f"{base}/{rel}" for rel in files]
    with open(os.path.join(DEV, "sw.js"), "w", encoding="utf-8") as fh:
        fh.write(sw.replace('"__BUILD__"', json.dumps(ident)).replace("__FILES__", json.dumps(cached)))

    sums = []
    for root, _dirs, names in os.walk(DEV):
        for name in names:
            path = os.path.join(root, name)
            with open(path, "rb") as fh:
                sums.append(f"{hashlib.sha256(fh.read()).hexdigest()}  {os.path.relpath(path, DEV)}")
    with open(os.path.join(DEV, "SHA256SUMS"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(sorted(sums, key=lambda s: s.split("  ")[1])) + "\n")
    total = sum(os.path.getsize(os.path.join(root, name))
                for root, _dirs, names in os.walk(DEV) for name in names)
    print(f"web/dist-dev/ pronto: build {ident}, {len(sums)} arquivos, {total / 1e6:.1f} MB")
    print("build de desenvolvimento: não publique; o build de produção é web/dist/ (web/build.py)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

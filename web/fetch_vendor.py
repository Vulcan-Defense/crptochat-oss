#!/usr/bin/env python3
"""Download and verify everything the browser client needs, into web/vendor/.

Nothing is loaded from a CDN at run time: the page serves these files itself.
Every file is pinned in vendor.lock.json and checked before it is kept:

* npm tarballs (Pyodide core, xterm.js) by their sha512 "integrity";
* pure-Python wheels from PyPI by sha256;
* Pyodide's compiled wheels (cryptography, cffi...) by the sha256 in
  pyodide-lock.json, which itself comes from the verified npm tarball.

    python3 web/fetch_vendor.py           # download and verify (idempotent)
    python3 web/fetch_vendor.py --lock    # re-resolve versions and rewrite the lock (maintainers)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = os.path.join(HERE, "vendor.lock.json")
VENDOR = os.path.join(HERE, "vendor")

# what --lock resolves
PYODIDE = "314.0.8"
NPM = {"pyodide": PYODIDE, "@xterm/xterm": "6.0.0", "@xterm/addon-fit": "0.11.0"}
PYPI = {"textual": "8.2.8", "rich": "15.0.0", "markdown-it-py": "4.2.0", "mdurl": "0.1.2",
        "mdit-py-plugins": "0.6.1", "platformdirs": "4.14.5", "pygments": "2.21.0",
        "typing-extensions": "4.16.0"}
PYODIDE_PACKAGES = ["cryptography"]          # plus their dependencies, from pyodide-lock.json
PYODIDE_CDN = "https://cdn.jsdelivr.net/pyodide/v{version}/full/"

# files kept from each npm tarball (path inside "package/" -> path under vendor/)
NPM_FILES = {
    "pyodide": {f: f"pyodide/{f}" for f in ("pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm",
                                           "python_stdlib.zip", "pyodide-lock.json")},
    "@xterm/xterm": {"lib/xterm.mjs": "xterm/xterm.mjs", "css/xterm.css": "xterm/xterm.css"},
    "@xterm/addon-fit": {"lib/addon-fit.mjs": "xterm/addon-fit.mjs"},
}
MAX = 64 * 1024 * 1024


def get(url: str) -> bytes:
    if not url.startswith("https://"):
        raise SystemExit(f"recusado (não é https): {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "crptochat-web-vendor"})
    # https only (checked above)
    with urllib.request.urlopen(req, timeout=120) as resp:  # nosec B310
        data = resp.read(MAX + 1)
    if len(data) > MAX:
        raise SystemExit(f"grande demais: {url}")
    return data


def check_integrity(data: bytes, integrity: str, what: str) -> None:
    algo, _, digest = integrity.partition("-")
    if algo != "sha512" or base64.b64decode(digest) != hashlib.sha512(data).digest():
        raise SystemExit(f"INTEGRIDADE NÃO CONFERE: {what}")


def check_sha256(data: bytes, sha256: str, what: str) -> None:
    if hashlib.sha256(data).hexdigest() != sha256:
        raise SystemExit(f"SHA-256 NÃO CONFERE: {what}")


def resolve() -> dict:
    lock: dict = {"npm": {}, "pypi": {}, "pyodide": {"version": PYODIDE, "packages": {}}}
    for name, version in NPM.items():
        meta = json.loads(get(f"https://registry.npmjs.org/{name}/{version}"))
        lock["npm"][name] = {"version": version, "url": meta["dist"]["tarball"],
                             "integrity": meta["dist"]["integrity"]}
    for name, version in PYPI.items():
        meta = json.loads(get(f"https://pypi.org/pypi/{name}/{version}/json"))
        wheel = [u for u in meta["urls"] if u["packagetype"] == "bdist_wheel" and u["filename"].endswith("-none-any.whl")]
        if len(wheel) != 1:
            raise SystemExit(f"{name} {version}: esperava um wheel puro")
        lock["pypi"][name] = {"version": version, "file": wheel[0]["filename"], "url": wheel[0]["url"],
                              "sha256": wheel[0]["digests"]["sha256"]}
    plock = json.loads(npm_files("pyodide", lock["npm"]["pyodide"])["pyodide/pyodide-lock.json"])
    todo, seen = list(PYODIDE_PACKAGES), set()
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        pkg = plock["packages"][name]
        lock["pyodide"]["packages"][name] = {"version": pkg["version"], "file": pkg["file_name"],
                                             "sha256": pkg["sha256"]}
        todo.extend(pkg.get("depends", []))
    return lock


def npm_files(name: str, entry: dict) -> dict[str, bytes]:
    data = get(entry["url"])
    check_integrity(data, entry["integrity"], f"{name}@{entry['version']}")
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for inner, dest in NPM_FILES[name].items():
            member = tar.getmember("package/" + inner)
            if not member.isfile():
                raise SystemExit(f"{name}: {inner} não é arquivo")
            out[dest] = tar.extractfile(member).read()
    return out


def write(rel: str, data: bytes) -> None:
    path = os.path.join(VENDOR, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def fetch(lock: dict) -> None:
    if os.path.isdir(VENDOR):
        shutil.rmtree(VENDOR)
    files = {}
    for name, entry in lock["npm"].items():
        files.update(npm_files(name, entry))
    plock = json.loads(files["pyodide/pyodide-lock.json"])
    if lock["npm"]["pyodide"]["version"] != lock["pyodide"]["version"]:
        raise SystemExit("versão do Pyodide do npm diferente da dos pacotes")
    base = PYODIDE_CDN.format(version=lock["pyodide"]["version"])
    for name, entry in lock["pyodide"]["packages"].items():
        expected = plock["packages"][name]
        if (expected["file_name"], expected["sha256"]) != (entry["file"], entry["sha256"]):
            raise SystemExit(f"{name}: lock e pyodide-lock.json divergem")
        data = get(base + entry["file"])
        check_sha256(data, entry["sha256"], entry["file"])
        files["pyodide/" + entry["file"]] = data
    wheels = []
    for name, entry in lock["pypi"].items():
        data = get(entry["url"])
        check_sha256(data, entry["sha256"], entry["file"])
        files["wheels/" + entry["file"]] = data
        wheels.append(entry["file"])
    for rel, data in files.items():
        write(rel, data)
    manifest = {"pyodide_packages": sorted(lock["pyodide"]["packages"]), "wheels": sorted(wheels)}
    write("manifest.json", json.dumps(manifest, indent=1).encode())
    total = sum(len(d) for d in files.values())
    print(f"vendor/ pronto: {len(files)} arquivos, {total / 1e6:.1f} MB, todos conferidos")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lock", action="store_true", help="resolve as versões e reescreve vendor.lock.json")
    args = ap.parse_args()
    if args.lock:
        lock = resolve()
        with open(LOCK, "w") as fh:
            json.dump(lock, fh, indent=1, sort_keys=True)
            fh.write("\n")
        print(f"lock escrito: {LOCK}")
    with open(LOCK) as fh:
        fetch(json.load(fh))


if __name__ == "__main__":
    sys.exit(main())

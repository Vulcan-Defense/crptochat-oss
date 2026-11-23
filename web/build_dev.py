#!/usr/bin/env python3
import gzip
import io
import json
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIST = os.path.join(HERE, "dist")
BUILD_DIR = os.path.join(DIST, "b", "dev")
SKIP_MODULES = {"server.py", "webgate.py", "admin.py", "dash.py", "stats.py", "selftest.py"}

def build_site_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Add wheel contents under app/
        wheels_dir = os.path.join(HERE, "vendor", "wheels")
        if os.path.isdir(wheels_dir):
            for whl in os.listdir(wheels_dir):
                if whl.endswith(".whl"):
                    with zipfile.ZipFile(os.path.join(wheels_dir, whl), "r") as wz:
                        for item in wz.infolist():
                            if not item.filename.endswith(".dist-info/") and "/.dist-info/" not in item.filename:
                                data = wz.read(item.filename)
                                zf.writestr(f"app/{item.filename}", data)

        # Add crptochat under app/crptochat/
        crpto_dir = os.path.join(ROOT, "crptochat")
        for root, _, files in os.walk(crpto_dir):
            for file in files:
                if file.endswith(".py") and file not in SKIP_MODULES:
                    rel = os.path.relpath(os.path.join(root, file), crpto_dir).replace("\\", "/")
                    with open(os.path.join(root, file), "rb") as f:
                        zf.writestr(f"app/crptochat/{rel}", f.read())

        # Add crptoweb under app/crptoweb/
        web_py_dir = os.path.join(HERE, "py", "crptoweb")
        for root, _, files in os.walk(web_py_dir):
            for file in files:
                if file.endswith(".py"):
                    rel = os.path.relpath(os.path.join(root, file), web_py_dir).replace("\\", "/")
                    with open(os.path.join(root, file), "rb") as f:
                        zf.writestr(f"app/crptoweb/{rel}", f.read())

    buf.seek(0)
    return gzip.compress(buf.getvalue())

def create_empty_gz_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("dummy.txt", b"ok")
    return gzip.compress(buf.getvalue())

def main():
    if os.path.exists(DIST):
        shutil.rmtree(DIST)

    os.makedirs(os.path.join(BUILD_DIR, "py"), exist_ok=True)

    # Copy src static files into BUILD_DIR
    src_dir = os.path.join(HERE, "src")
    for item in os.listdir(src_dir):
        s = os.path.join(src_dir, item)
        d = os.path.join(BUILD_DIR, item)
        if os.path.isfile(s) and item not in ("index.html", "sw.js"):
            shutil.copy2(s, d)

    # Copy vendor into BUILD_DIR/vendor
    shutil.copytree(os.path.join(HERE, "vendor"), os.path.join(BUILD_DIR, "vendor"))

    # Copy python_stdlib.zip
    shutil.copy2(
        os.path.join(HERE, "vendor", "pyodide", "python_stdlib.zip"),
        os.path.join(BUILD_DIR, "py", "python_stdlib.zip")
    )

    # Create site.zip.gz
    site_gz = build_site_zip()
    with open(os.path.join(BUILD_DIR, "py", "site.zip.gz"), "wb") as f:
        f.write(site_gz)

    empty_gz = create_empty_gz_zip()
    with open(os.path.join(BUILD_DIR, "py", "stdlib-rest.zip.gz"), "wb") as f:
        f.write(empty_gz)
    with open(os.path.join(BUILD_DIR, "py", "later.zip.gz"), "wb") as f:
        f.write(empty_gz)

    # Write index.html to dist/index.html
    with open(os.path.join(src_dir, "index.html"), "r", encoding="utf-8") as f:
        index_content = f.read()
    index_content = index_content.replace("__BUILD_DIR__", "b/dev")
    with open(os.path.join(DIST, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_content)

    # Write sw.js to dist/sw.js
    with open(os.path.join(src_dir, "sw.js"), "r", encoding="utf-8") as f:
        sw_content = f.read()
    sw_content = sw_content.replace('"__BUILD__"', '"dev"').replace("__FILES__", "[]")
    with open(os.path.join(DIST, "sw.js"), "w", encoding="utf-8") as f:
        f.write(sw_content)

    print("web/dist/ criado com sucesso em b/dev!")

if __name__ == "__main__":
    main()

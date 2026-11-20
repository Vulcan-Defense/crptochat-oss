"""O selo de versão na página de downloads acompanha crptochat.__version__."""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "site" / "downloads" / "index.html"
STAMP = ROOT / "packaging" / "stamp_site.py"


def test_downloads_chip_matches_package_version(tmp_path):
    from crptochat import __version__

    page = tmp_path / "dl.html"
    page.write_text(INDEX.read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run([sys.executable, str(STAMP), "--init", str(ROOT / "crptochat" / "__init__.py"), str(page)],
                   check=True, cwd=ROOT)
    text = page.read_text(encoding="utf-8")
    found = re.search(r'<span class="chip"><i></i>v([0-9.]+) ·', text)
    assert found, "selo de versão não encontrado"
    assert found.group(1) == __version__
    assert "__VERSION__" not in text


def test_stamp_site_replaces_placeholder(tmp_path):
    from crptochat import __version__

    page = tmp_path / "index.html"
    page.write_text("<span>v__VERSION__</span>", encoding="utf-8")
    subprocess.run([sys.executable, str(STAMP), "--init", str(ROOT / "crptochat" / "__init__.py"), str(page)],
                   check=True, cwd=ROOT)
    assert f"v{__version__}" in page.read_text(encoding="utf-8")
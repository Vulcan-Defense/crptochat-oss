"""The zips of modules the page fetches after it opens (the rest of the standard
library, Pygments): put on the import path, including for packages that already
came from the boot zip.

A package imported from the boot zip only looks for its submodules in its own
folder there: `encodings` is used at boot, `encodings.idna` sits in the later
zip, so "x".encode("idna") (any https host name) failed with "unknown encoding".
A last-resort finder looks for such submodules in the later zips. It runs only
when the normal lookup found nothing, so the order between fetching the zips and
importing the packages does not matter, and the boot zip stays as small as it is.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import sys

_ZIPS: list[str] = []


class LaterZips:
    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if "." not in fullname:            # top level: sys.path already has the zips
            return None
        folder = fullname.rpartition(".")[0].replace(".", "/")
        for archive in _ZIPS:
            spec = importlib.machinery.PathFinder.find_spec(fullname, [f"{archive}/{folder}"])
            if spec is not None:
                return spec
        return None


def mount(archive: str) -> None:
    if archive not in sys.path:
        sys.path.append(archive)
    if archive not in _ZIPS:
        _ZIPS.append(archive)
    if LaterZips not in sys.meta_path:
        sys.meta_path.append(LaterZips)
    importlib.invalidate_caches()

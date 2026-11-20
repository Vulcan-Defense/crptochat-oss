"""The CI guard that keeps the version from going back (packaging/check_version.py)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packaging"))
import check_version  # noqa: E402

V = '"""crptochat"""\n\n__version__ = "{}"\n'


@pytest.mark.parametrize("now, before, ok", [
    ("1.21.0", "1.20.1", True),       # #21 after #20
    ("1.21.0", "1.21.0", True),       # a PR that does not bump
    ("1.20.1", "1.21.0", False),      # the wrong side of the conflict
    ("1.10.0", "1.9.9", True),        # numeric, not text
    ("1.9.9", "1.10.0", False),
])
def test_only_forward(now, before, ok):
    assert (check_version.check(V.format(now), V.format(before)) is None) is ok


def test_the_repository_file_parses():
    with open(os.path.join(os.path.dirname(__file__), "..", "crptochat", "__init__.py")) as fh:
        assert check_version.parse(fh.read()) >= (1, 21, 0)


def test_garbage():
    with pytest.raises(ValueError):
        check_version.parse("version = 1")

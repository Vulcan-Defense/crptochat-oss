"""Keys in the browser client: the same sealed blob the terminal keeps in a
password vault goes in and out (web/py/crptoweb/keys.py, plain CPython)."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web", "py"))
from crptoweb import keys  # noqa: E402

from crptochat import crypto  # noqa: E402
from crptochat.store import Identity  # noqa: E402


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "SCRYPT_N", 2 ** 10)
    monkeypatch.setenv("CRPTOCHAT_HOME", str(tmp_path / "terminal"))
    return tmp_path


def terminal_blob(nick, passphrase):
    """What `pass show crptochat/NICK` prints after the terminal moved its key there."""
    ident = Identity.create(nick, passphrase)
    with open(Identity.path(nick)) as fh:
        data = json.load(fh)
    return ident, json.dumps(data, separators=(",", ":"))


def to_browser(monkeypatch, home):
    monkeypatch.setenv("CRPTOCHAT_HOME", str(home / "browser"))


def test_a_terminal_key_moves_into_the_browser_and_back(home, monkeypatch):
    ident, blob = terminal_blob("Neo_99", "senha do terminal")
    to_browser(monkeypatch, home)
    assert not keys.has_key("Neo_99")
    got = keys.import_key(blob, "senha do terminal")
    assert got == {"nick": "Neo_99", "fingerprint": crypto.fingerprint(ident.ik)}
    assert keys.has_key("neo_99")                                     # nicks are case-insensitive
    assert keys.check_passphrase("Neo_99", "senha do terminal")["fingerprint"] == got["fingerprint"]
    loaded = Identity.load("Neo_99", "senha do terminal")             # what the chat then opens
    assert loaded.ik == ident.ik
    out = keys.export_key("Neo_99")
    assert json.loads(out) == json.loads(blob) and " " not in out     # compact, as a vault holds it


def test_the_passphrase_is_checked_before_anything_is_kept(home, monkeypatch):
    _, blob = terminal_blob("trinity", "certa-123")
    to_browser(monkeypatch, home)
    with pytest.raises(keys.KeyProblem, match="senha não abre"):
        keys.import_key(blob, "errada-123")
    assert not keys.has_key("trinity")


@pytest.mark.parametrize("bad, why", [
    ("", "não é JSON"), ("x" * 5000, "grande demais"), ("[1, 2]", "formato"),
    ('{"v": 2, "nick": "neo", "salt": "", "key": ""}', "formato"),
    ('{"v": 1, "nick": "neo", "salt": "AA==", "key": "AA==", "extra": 1}', "formato"),
    ('{"v": 1, "nick": "a b", "salt": "AA==", "key": "AA=="}', "nickname"),
    ('{"v": 1, "nick": "neo", "salt": "%%%", "key": "AA=="}', "base64"),
    ('{"v": 1, "nick": "neo", "salt": "AA==", "key": "AA=="}', "tamanhos"),
])
def test_garbage_is_refused_with_a_clear_reason(bad, why):
    with pytest.raises(keys.KeyProblem, match=why):
        keys.import_key(bad, "qualquer")


def test_another_key_for_the_same_nick_is_not_overwritten_by_accident(home, monkeypatch):
    first, blob1 = terminal_blob("morpheus", "senha-um-1")
    monkeypatch.setenv("CRPTOCHAT_HOME", str(home / "other"))
    second, blob2 = terminal_blob("morpheus", "senha-dois-2")
    to_browser(monkeypatch, home)
    keys.import_key(blob1, "senha-um-1")
    with pytest.raises(keys.KeyProblem, match="substituir"):
        keys.import_key(blob2, "senha-dois-2")
    assert keys.check_passphrase("morpheus", "senha-um-1")["fingerprint"] == crypto.fingerprint(first.ik)
    got = keys.import_key(blob2, "senha-dois-2", replace=True)           # only when asked
    assert got["fingerprint"] == crypto.fingerprint(second.ik)


def test_the_same_key_sealed_again(home, monkeypatch):
    ident, blob = terminal_blob("oracle", "antiga-123")
    ident.change_passphrase("antiga-123", "nova-4567")
    with open(Identity.path("oracle")) as fh:
        newer = fh.read()
    to_browser(monkeypatch, home)
    keys.import_key(blob, "antiga-123")
    with pytest.raises(keys.KeyProblem):                 # the old passphrase does not open the new blob
        keys.import_key(newer, "antiga-123")
    with pytest.raises(keys.KeyProblem, match="substituir"):
        keys.import_key(newer, "nova-4567")              # the kept one does not open with it: cannot compare
    keys.import_key(newer, "nova-4567", replace=True)
    keys.import_key(newer, "nova-4567")                  # same blob again: opens and matches, no replace needed
    assert keys.check_passphrase("oracle", "nova-4567")["fingerprint"] == crypto.fingerprint(ident.ik)


def test_create_and_check(home, monkeypatch):
    to_browser(monkeypatch, home)
    with pytest.raises(keys.KeyProblem, match="nickname"):
        keys.create_key("x", "senha-longa")
    with pytest.raises(keys.KeyProblem, match="8 caracteres"):
        keys.create_key("neo", "curta")
    made = keys.create_key("neo", "senha-longa")
    with pytest.raises(keys.KeyProblem, match="já existe"):
        keys.create_key("NEO", "outra-senha")
    with pytest.raises(keys.KeyProblem, match="senha incorreta"):
        keys.check_passphrase("neo", "errada-errada")
    with pytest.raises(keys.KeyProblem, match="não há chave"):
        keys.check_passphrase("ghost", "senha-longa")
    assert keys.check_passphrase("neo", "senha-longa") == made
    # and it opens on the terminal: same format both ways
    blob = keys.export_key("neo")
    monkeypatch.setenv("CRPTOCHAT_HOME", str(home / "terminal2"))
    keys.import_key(blob, "senha-longa")
    assert crypto.fingerprint(Identity.load("neo", "senha-longa").ik) == made["fingerprint"]

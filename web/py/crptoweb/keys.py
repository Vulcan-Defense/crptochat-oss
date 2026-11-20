"""The browser's nick keys and password vaults.

A browser cannot run `pass`, `secret-tool` or `vault` like the terminal does,
so the key moves in and out as the same blob those vaults hold: the Ed25519
seed already sealed with the identity passphrase (scrypt + ChaCha20-Poly1305,
bound to the nick), as JSON. That blob is safe to keep in a password manager
(a secure note or attachment in 1Password, Bitwarden, KeePassXC...): whoever
gets it still needs the passphrase.

    export  the blob of a key kept in this browser (for a backup or a vault)
    import  a blob from a backup, from another browser or from the terminal
            (`pass show crptochat/NICK`, the key file in ~/.config/crptochat)

No Textual or Pyodide import here: it runs (and is tested) in plain CPython.
"""

from __future__ import annotations

import json
import os

from crptochat import crypto
from crptochat.protocol import b64d, valid_nick
from crptochat.store import BadPassphrase, Identity, _read, _unseal, _write_private

MAX_BLOB = 4096            # a real one is ~180 bytes


class KeyProblem(ValueError):
    """Shown to the person as is (Portuguese)."""


def has_key(nick: str) -> bool:
    return valid_nick(nick) and Identity.exists(nick)


def parse_blob(text: str) -> dict:
    """A sealed identity, checked for shape (not for the passphrase)."""
    if not isinstance(text, str) or len(text) > MAX_BLOB:
        raise KeyProblem("isso não é uma chave do crptochat (grande demais)")
    try:
        data = json.loads(text.strip())
    except ValueError:
        raise KeyProblem("isso não é uma chave do crptochat (não é JSON)") from None
    if not isinstance(data, dict) or data.get("v") != 1 or set(data) != {"v", "nick", "salt", "key"}:
        raise KeyProblem("isso não é uma chave do crptochat (formato desconhecido)")
    if not valid_nick(data["nick"]):
        raise KeyProblem("a chave traz um nickname inválido")
    try:
        salt, key = b64d(data["salt"]), b64d(data["key"])
    except Exception:
        raise KeyProblem("a chave está corrompida (base64 inválido)") from None
    if len(salt) != 16 or not 32 < len(key) < 256:
        raise KeyProblem("a chave está corrompida (tamanhos inesperados)")
    return data


def export_key(nick: str) -> str:
    """The blob of a key kept here, compact as the terminal writes it to a vault."""
    if not has_key(nick):
        raise KeyProblem(f"não há chave de '{nick}' neste navegador")
    data = parse_blob(json.dumps(_read(Identity.path(nick), None)))
    return json.dumps(data, separators=(",", ":"))


def import_key(text: str, passphrase: str, replace: bool = False) -> dict:
    """Keeps a blob in this browser once the passphrase opens it.

    Another blob already kept for the same nick is only replaced with
    ``replace``: if it is a different key, it would be lost for good. Without
    ``replace`` it is only updated when the same passphrase opens it and it holds
    the same key; one sealed with another passphrase cannot be compared (it
    does not open), so that also needs ``replace``."""
    data = parse_blob(text)
    nick = data["nick"]
    try:
        seed = _unseal(data, passphrase)
    except BadPassphrase:
        raise KeyProblem("a senha não abre esta chave") from None
    ik = crypto.ed_pub(crypto.ed_from_seed(seed))
    path = Identity.path(nick)
    if Identity.exists(nick) and not replace:
        old = _read(path, None)
        try:
            same = old is not None and _unseal(old, passphrase) == seed
        except BadPassphrase:
            same = False
        if not same:
            raise KeyProblem(f"já existe uma chave de '{nick}' neste navegador e esta senha não a abre "
                             "(outra chave, ou a mesma com outra senha); marque \"substituir\" só se "
                             "puder perder a atual")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    _write_private(path, data)
    return {"nick": nick, "fingerprint": crypto.fingerprint(ik)}


def create_key(nick: str, passphrase: str) -> dict:
    """A new key for this browser (the server registers it at the first login)."""
    if not valid_nick(nick):
        raise KeyProblem("nickname inválido (3 a 20 letras, números, _ ou -)")
    if len(passphrase) < 8:
        raise KeyProblem("use uma senha de pelo menos 8 caracteres")
    if Identity.exists(nick):
        raise KeyProblem(f"já existe uma chave de '{nick}' neste navegador")
    ident = Identity.create(nick, passphrase)
    return {"nick": ident.nick, "fingerprint": crypto.fingerprint(ident.ik)}


def check_passphrase(nick: str, passphrase: str) -> dict:
    """Opens the key kept here (before the chat starts, so the page can tell
    a wrong passphrase and offer to save the right one to a password manager)."""
    if not has_key(nick):
        raise KeyProblem(f"não há chave de '{nick}' neste navegador")
    try:
        ident = Identity.load(nick, passphrase)
    except BadPassphrase:
        raise KeyProblem("senha incorreta para esta chave") from None
    return {"nick": ident.nick, "fingerprint": crypto.fingerprint(ident.ik)}

#!/usr/bin/env python3
"""Lógica de deploy na VM (verificação de RELEASE, git, env). Usado por crptochat-deploy.sh."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess  # nosec B404
import sys
from pathlib import Path

from crptochat import update

ROOT = Path(__file__).resolve().parents[1]
VERSION_RE = re.compile(r'^__version__ = "(\d+(?:\.\d+){1,3})"$', re.M)
TAG_RE = re.compile(r"^v(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})$")
SAFE_PATH = re.compile(r"^/[a-zA-Z0-9._/-]+$")
SAFE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
BASE64_KEY = re.compile(r"^[A-Za-z0-9+/]{40,60}={0,2}$")

PROD_DEFAULTS = {
    "CRPTOCHAT_REPO_DIR": "/opt/crptochat",
    "CRPTOCHAT_COMPOSE_FILE": "/opt/crptochat-setup/docker-compose.isolated.yml",
    "CRPTOCHAT_COMPOSE_PROJECT": "crptochat-setup",
    "CRPTOCHAT_DATA_VOLUME": "crptochat-setup_crptochat-data",
}

SIGNING_PUBKEY_DEFAULT = "UXnFSaezyEwMqfhfGncgjf03YiSN/FvaIja1Kv6g5VE="


class DeployError(Exception):
    pass


def tag_to_version(tag: str) -> str:
    if not TAG_RE.fullmatch(tag):
        raise DeployError(f"tag inválida: {tag!r}")
    return tag[1:]


def validate_deploy_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Valida variáveis carregadas de deploy.env; devolve defaults de produção onde couber."""
    env = dict(environ or os.environ)
    out = dict(PROD_DEFAULTS)
    for key in PROD_DEFAULTS:
        if key in env and env[key].strip():
            out[key] = env[key].strip()
    path_keys = ("CRPTOCHAT_REPO_DIR", "CRPTOCHAT_COMPOSE_FILE")
    for key in path_keys:
        val = out[key]
        if not SAFE_PATH.fullmatch(val) or ".." in val:
            raise DeployError(f"{key} com caminho inválido: {val!r}")
    proj = out["CRPTOCHAT_COMPOSE_PROJECT"]
    if not SAFE_NAME.fullmatch(proj):
        raise DeployError(f"CRPTOCHAT_COMPOSE_PROJECT inválido: {proj!r}")
    vol = out["CRPTOCHAT_DATA_VOLUME"]
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", vol):
        raise DeployError(f"CRPTOCHAT_DATA_VOLUME inválido: {vol!r}")
    if "SSH_ORIGINAL_COMMAND" in env and env["SSH_ORIGINAL_COMMAND"].strip():
        cmd = env["SSH_ORIGINAL_COMMAND"].strip()
        if not TAG_RE.fullmatch(cmd):
            raise DeployError("SSH_ORIGINAL_COMMAND não é uma tag vX.Y.Z")
    return out


def verify_release_files(manifest_path: Path, sig_path: Path, pubkey: str, requested_tag: str) -> dict:
    manifest = manifest_path.read_bytes()
    update.verify(manifest, sig_path.read_bytes(), pubkey)
    info = update.parse_manifest(manifest)
    want = tag_to_version(requested_tag)
    if info["version"] != want:
        raise DeployError(f"RELEASE version {info['version']!r} ≠ tag {requested_tag} ({want!r})")
    return info


def version_at_commit(repo: Path, commit: str) -> str:
    try:
        text = subprocess.check_output(  # nosec B603 B607
            ["git", "show", f"{commit}:crptochat/__init__.py"],
            cwd=repo,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        raise DeployError(f"commit {commit} sem crptochat/__init__.py") from exc
    m = VERSION_RE.search(text)
    if not m:
        raise DeployError(f"versão não encontrada no commit {commit}")
    return m.group(1)


def _main_tip_ref(repo: Path) -> str:
    remotes = subprocess.check_output(["git", "remote"], cwd=repo, text=True).split()  # nosec B603 B607
    if "origin" in remotes:
        subprocess.run(["git", "fetch", "--quiet", "origin", "main"], cwd=repo, check=True)  # nosec B603 B607
        return "FETCH_HEAD"
    subprocess.run(["git", "rev-parse", "--verify", "main"], cwd=repo, check=True)  # nosec B603 B607
    return "main"


def commit_is_ancestor_of_main(repo: Path, commit: str) -> None:
    tip = _main_tip_ref(repo)
    r = subprocess.run(  # nosec B603 B607
        ["git", "merge-base", "--is-ancestor", commit, tip],
        cwd=repo,
    )
    if r.returncode != 0:
        raise DeployError(f"commit {commit} não é ancestral de origin/main")


def checkout_commit(repo: Path, commit: str) -> None:
    subprocess.run(["git", "checkout", "--quiet", commit], cwd=repo, check=True)  # nosec B603 B607
    subprocess.run(["git", "reset", "--hard", commit], cwd=repo, check=True)  # nosec B603 B607


def assert_version_matches(manifest_version: str, package_version: str) -> None:
    if manifest_version != package_version:
        raise DeployError(
            f"versão do pacote no commit ({package_version}) ≠ RELEASE ({manifest_version})")


def manifest_asset_names(info: dict) -> list[str]:
    return sorted(info["files"])


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_downloaded_assets(manifest_path: Path, directory: Path) -> None:
    info = update.parse_manifest(manifest_path.read_bytes())
    for name, expected in info["files"].items():
        path = directory / name
        if not path.is_file():
            raise DeployError(f"falta {name} em {directory}")
        got = sha256_file(path)
        if got != expected:
            raise DeployError(f"SHA-256 de {name} não confere")


def render_compose(template_path: Path, out_path: Path, volume_name: str) -> None:
    text = template_path.read_text(encoding="utf-8")
    text = re.sub(
        r"\nvolumes:\n.*\Z",
        f"\nvolumes:\n  crptochat-data:\n    external: true\n    name: {volume_name}\n",
        text,
        flags=re.DOTALL,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out_path)


def cmd_verify_release(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--sig", type=Path, required=True)
    ap.add_argument("--pubkey", default=os.environ.get("CLIENT_SIGNING_PUBKEY", SIGNING_PUBKEY_DEFAULT))
    ap.add_argument("--repo", type=Path, default=Path("/opt/crptochat"))
    args = ap.parse_args(argv)
    try:
        info = verify_release_files(args.manifest, args.sig, args.pubkey, args.tag)
        commit_is_ancestor_of_main(args.repo, info["commit"])
        pkg_ver = version_at_commit(args.repo, info["commit"])
        assert_version_matches(info["version"], pkg_ver)
    except (DeployError, update.UpdateError) as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1
    print(f"commit={info['commit']}")
    print(f"version={info['version']}")
    return 0


def cmd_env_check(argv: list[str]) -> int:
    try:
        validate_deploy_env()
    except DeployError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1
    print("ok")
    return 0


def cmd_render_compose(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--volume", required=True)
    args = ap.parse_args(argv)
    render_compose(args.template, args.out, args.volume)
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("uso: deploy_lib.py verify-release | env-check | render-compose", file=sys.stderr)
        return 2
    cmd = sys.argv[1]
    rest = sys.argv[2:]
    if cmd == "verify-release":
        return cmd_verify_release(rest)
    if cmd == "env-check":
        return cmd_env_check(rest)
    if cmd == "render-compose":
        return cmd_render_compose(rest)
    print(f"comando desconhecido: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
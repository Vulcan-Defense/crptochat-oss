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
WEB_DIST_TARBALL = "crptochat-web-dist.tar.gz"
DOWNLOAD_ASSET_NAMES = (
    "crptochat-linux-amd64",
    "crptochat-linux-arm64",
    "crptochat-macos-arm64",
    "crptochat-windows-amd64.exe",
    "install.sh",
    "SHA256SUMS",
    "RELEASE",
    "RELEASE.sig",
    WEB_DIST_TARBALL,
)


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


def root_index_from_downloads(downloads_html: str) -> str:
    """Página completa na raiz: mesmos conteúdos, links de binários em downloads/."""
    text = downloads_html
    for name in DOWNLOAD_ASSET_NAMES:
        if name == WEB_DIST_TARBALL:
            continue
        text = text.replace(f'href="{name}"', f'href="downloads/{name}"')
    return text


def insert_caddy_ws_handle(caddyfile: str, site_host: str = "crptochat.acpt.app.br") -> str:
    if "handle /ws" in caddyfile:
        return caddyfile
    lines = caddyfile.splitlines()
    out: list[str] = []
    in_site = False
    depth = 0
    for line in lines:
        if not in_site and site_host in line and "{" in line:
            in_site = True
            depth = line.count("{") - line.count("}")
            out.append(line)
            continue
        if in_site:
            depth += line.count("{") - line.count("}")
            if depth <= 0 and "}" in line:
                out.append("    handle /ws {")
                out.append("        reverse_proxy crptochat:7778 {")
                out.append("            header_up X-Real-IP {remote_host}")
                out.append("        }")
                out.append("    }")
                in_site = False
        out.append(line)
    if in_site:
        raise DeployError(f"bloco do site {site_host} não fechado no Caddyfile")
    return "\n".join(out) + ("\n" if caddyfile.endswith("\n") else "")


def _toplevel_section_index(lines: list[str], name: str) -> int | None:
    for i, line in enumerate(lines):
        if line == f"{name}:":
            return i
    return None


def _caddy_service_network_ok(lines: list[str]) -> bool:
    in_caddy = False
    caddy_indent = 0
    in_networks = False
    has_crptochat = False
    for line in lines:
        if re.match(r"^\s+caddy:\s*$", line):
            in_caddy = True
            caddy_indent = len(line) - len(line.lstrip())
            in_networks = False
            has_crptochat = False
            continue
        if not in_caddy:
            continue
        if line.strip() and (len(line) - len(line.lstrip())) <= caddy_indent:
            in_caddy = False
            continue
        indent = len(line) - len(line.lstrip())
        if line.strip() == "networks:" and indent == caddy_indent + 2:
            in_networks = True
            continue
        if in_networks and indent >= caddy_indent + 4 and "crptochat" in line:
            has_crptochat = True
        if in_networks and line.strip() and indent <= caddy_indent + 2:
            in_networks = False
    return has_crptochat


def _toplevel_crptochat_network_ok(lines: list[str]) -> bool:
    idx = _toplevel_section_index(lines, "networks")
    if idx is None:
        return False
    end = len(lines)
    for j in range(idx + 1, len(lines)):
        if lines[j] and not lines[j][0].isspace() and lines[j].endswith(":"):
            end = j
            break
    block = "\n".join(lines[idx:end])
    return "crptochat:" in block and "external: true" in block


def ensure_caddy_compose_network(compose_text: str) -> str:
    lines = compose_text.splitlines()
    if _toplevel_crptochat_network_ok(lines) and _caddy_service_network_ok(lines):
        return compose_text
    vol_idx = _toplevel_section_index(lines, "volumes")
    if vol_idx is None:
        raise DeployError("compose do Caddy sem seção volumes:")
    if not _toplevel_crptochat_network_ok(lines):
        lines = (
            lines[:vol_idx]
            + [
                "networks:",
                "  crptochat:",
                "    external: true",
                "    name: crptochat",
                "",
            ]
            + lines[vol_idx:]
        )
    if not _caddy_service_network_ok(lines):
        lines = _add_crptochat_to_caddy_service(lines)
    return "\n".join(lines) + ("\n" if compose_text.endswith("\n") else "")


def _add_crptochat_to_caddy_service(lines: list[str]) -> list[str]:
    for i, line in enumerate(lines):
        if not re.match(r"^\s+caddy:\s*$", line):
            continue
        indent = len(line) - len(line.lstrip())
        j = i + 1
        net_start: int | None = None
        while j < len(lines):
            cur = lines[j]
            if cur.strip() and (len(cur) - len(cur.lstrip())) <= indent:
                break
            cur_indent = len(cur) - len(cur.lstrip())
            if cur.strip() == "networks:" and cur_indent == indent + 2:
                net_start = j
                break
            j += 1
        if net_start is None:
            insert = [
                " " * (indent + 2) + "networks:",
                " " * (indent + 4) + "- default",
                " " * (indent + 4) + "- crptochat",
            ]
            return lines[: i + 1] + insert + lines[i + 1 :]
        k = net_start + 1
        has_default = False
        has_crptochat = False
        last_net_line = net_start
        while k < len(lines):
            cur = lines[k]
            if cur.strip() and (len(cur) - len(cur.lstrip())) <= indent + 2:
                break
            if "crptochat" in cur:
                has_crptochat = True
            if re.search(r"\bdefault\b", cur):
                has_default = True
            last_net_line = k
            k += 1
        if has_crptochat:
            return lines
        additions: list[str] = []
        if not has_default:
            additions.append(" " * (indent + 4) + "- default")
        additions.append(" " * (indent + 4) + "- crptochat")
        return lines[: last_net_line + 1] + additions + lines[last_net_line + 1 :]
    raise DeployError("compose do Caddy sem serviço caddy")


def extract_selftest_fingerprint(probe_output: str) -> str | None:
    for line in probe_output.splitlines():
        if "fingerprint" in line:
            return line.split("fingerprint", 1)[-1].strip().split()[0]
    return None


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


def cmd_caddy_ws(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    try:
        text = insert_caddy_ws_handle(args.inp.read_text(encoding="utf-8"))
    except DeployError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(text, encoding="utf-8")
    return 0


def cmd_root_index(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--downloads", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    args.out.write_text(root_index_from_downloads(args.downloads.read_text(encoding="utf-8")), encoding="utf-8")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("uso: deploy_lib.py verify-release | env-check | render-compose | caddy-ws | root-index",
              file=sys.stderr)
        return 2
    cmd = sys.argv[1]
    rest = sys.argv[2:]
    if cmd == "verify-release":
        return cmd_verify_release(rest)
    if cmd == "env-check":
        return cmd_env_check(rest)
    if cmd == "render-compose":
        return cmd_render_compose(rest)
    if cmd == "caddy-ws":
        return cmd_caddy_ws(rest)
    if cmd == "root-index":
        return cmd_root_index(rest)
    print(f"comando desconhecido: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
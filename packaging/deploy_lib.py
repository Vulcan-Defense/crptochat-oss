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


def verify_downloaded_assets(manifest_path: Path, directory: Path, names: list[str] | None = None) -> None:
    """Each asset (all of the manifest's, or just `names`) must be in `directory`
    with the SHA-256 the signed RELEASE lists. A name the RELEASE does not list
    is refused."""
    info = update.parse_manifest(manifest_path.read_bytes())
    wanted = sorted(info["files"]) if names is None else list(names)
    for name in wanted:
        if name not in info["files"]:
            raise DeployError(f"{name} não está na RELEASE")
        expected = info["files"][name]
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


# ---------------------------------------------------------------- compose YAML
#
# docker-compose.caddy.yml is edited structurally, keeping its comments and
# layout: the file is read as a tree of block mappings by indentation, the two
# nodes that matter are found by path (top-level `networks`, and
# `services.caddy.networks`), and only those get lines added. The result is then
# parsed back (PyYAML when present; the bootstrap also runs `docker compose
# config`) and must mean exactly the original plus the crptochat network.

_KEY_LINE = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z0-9_.-]+|\"[^\"]*\"|'[^']*'):(?:[ \t]+(?P<rest>\S.*?))?[ \t]*$")
_ITEM_LINE = re.compile(r"^(?P<indent> *)- *(?P<value>.*?)[ \t]*$")


def _meaningful(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _strip_comment(value: str) -> str:
    if value.startswith(("'", '"')):
        return value
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def _unquote(key: str) -> str:
    return key[1:-1] if key[:1] in "'\"" and key[-1:] == key[:1] else key


def _block_end(lines: list[str], start: int, end: int, indent: int) -> int:
    """First line after `start` (a key at `indent`) that leaves its block."""
    last = start
    for i in range(start + 1, end):
        if not _meaningful(lines[i]):
            continue
        if _indent(lines[i]) <= indent and not (
            _indent(lines[i]) == indent and _ITEM_LINE.match(lines[i])    # compact list under the key
        ):
            break
        last = i
    return last + 1


def _keys(lines: list[str], start: int, end: int) -> tuple[int | None, dict[str, tuple[int, int, str | None]]]:
    """The keys of the block mapping in lines[start:end]: name -> (line, block end, inline value)."""
    indent = None
    found: dict[str, tuple[int, int, str | None]] = {}
    i = start
    while i < end:
        line = lines[i]
        if not _meaningful(line):
            i += 1
            continue
        if indent is None:
            indent = _indent(line)
        if _indent(line) != indent:
            i += 1
            continue
        m = _KEY_LINE.match(line)
        if not m:
            raise DeployError(f"YAML não suportado na linha {i + 1}: {line.strip()!r}")
        rest = m.group("rest")
        rest = _strip_comment(rest) if rest else None
        block_end = _block_end(lines, i, end, indent)
        found[_unquote(m.group("key"))] = (i, block_end, rest or None)
        i = block_end
    return indent, found


def _last_line(lines: list[str], start: int, end: int) -> int:
    """Index after the last meaningful line of a block (so additions go before trailing blanks/comments)."""
    last = start
    for i in range(start, end):
        if _meaningful(lines[i]):
            last = i
    return last + 1


def _flow_items(value: str) -> list[str] | None:
    value = value.strip()
    if not (value.startswith("[") and value.endswith("]")):
        return None
    inner = value[1:-1].strip()
    return [_unquote(v.strip()) for v in inner.split(",") if v.strip()] if inner else []


def _caddy_networks_edit(lines: list[str], caddy_line: int, caddy_end: int) -> list[str]:
    child_indent, keys = _keys(lines, caddy_line + 1, caddy_end)
    if child_indent is None:
        raise DeployError("serviço caddy vazio no compose")
    if "network_mode" in keys:
        raise DeployError("serviço caddy usa network_mode; não dá para ligar à rede crptochat")
    pad = " " * child_indent
    if "networks" not in keys:
        # no networks: compose had it on `default` only; keep it there and add crptochat
        at = _last_line(lines, caddy_line + 1, caddy_end)
        return lines[:at] + [f"{pad}networks:", f"{pad}  - default", f"{pad}  - crptochat"] + lines[at:]
    n_line, n_end, inline = keys["networks"]
    if inline is not None:
        items = _flow_items(inline)
        if items is None:
            if inline.strip() in ("{}",):
                return lines[:n_line] + [f"{pad}networks: {{default: {{}}, crptochat: {{}}}}"] + lines[n_line + 1:]
            raise DeployError(f"networks do caddy em formato não suportado: {inline!r}")
        if "crptochat" in items:
            return lines
        items = items or ["default"]
        return lines[:n_line] + [f"{pad}networks: [{', '.join(items + ['crptochat'])}]"] + lines[n_line + 1:]
    body = [i for i in range(n_line + 1, n_end) if _meaningful(lines[i])]
    if not body:
        return lines[:n_line + 1] + [f"{pad}  - default", f"{pad}  - crptochat"] + lines[n_line + 1:]
    first = lines[body[0]]
    at = _last_line(lines, n_line + 1, n_end)
    if _ITEM_LINE.match(first):                       # list form
        item_indent = " " * _indent(first)
        names = [_unquote(_strip_comment(_ITEM_LINE.match(lines[i]).group("value")))
                 for i in body if _indent(lines[i]) == _indent(first) and _ITEM_LINE.match(lines[i])]
        if "crptochat" in names:
            return lines
        return lines[:at] + [f"{item_indent}- crptochat"] + lines[at:]
    item_indent, net_keys = _keys(lines, n_line + 1, n_end)   # mapping form: name: {options}
    if "crptochat" in net_keys:
        return lines
    return lines[:at] + [f"{' ' * item_indent}crptochat: {{}}"] + lines[at:]


def _toplevel_networks_edit(lines: list[str]) -> list[str]:
    _, top = _keys(lines, 0, len(lines))
    if "networks" not in top:
        # a new top-level section, at column 0 after everything else (e.g. after volumes:)
        at = _last_line(lines, 0, len(lines))
        sep = [""] if at and lines[at - 1].strip() else []
        return lines[:at] + sep + ["networks:", "  crptochat:", "    external: true"] + lines[at:]
    n_line, n_end, inline = top["networks"]
    if inline is not None:
        raise DeployError("seção networks: em formato inline não suportado")
    child_indent, nets = _keys(lines, n_line + 1, n_end)
    if child_indent is None:
        return lines[:n_line + 1] + ["  crptochat:", "    external: true"] + lines[n_line + 1:]
    if "crptochat" in nets:
        c_line, c_end, c_inline = nets["crptochat"]
        c_indent, opts = _keys(lines, c_line + 1, c_end)
        if c_inline is None and opts.get("external", (0, 0, None))[2] == "true":
            return lines
        raise DeployError("rede crptochat já declarada no compose do Caddy, mas não como external: true")
    at = _last_line(lines, n_line + 1, n_end)
    pad = " " * child_indent
    return lines[:at] + [f"{pad}crptochat:", f"{pad}  external: true"] + lines[at:]


def _expected_compose(data: dict) -> dict:
    import copy
    want = copy.deepcopy(data)
    nets = want.setdefault("networks", {}) or {}
    want["networks"] = nets
    nets.setdefault("crptochat", {"external": True})
    caddy = want["services"]["caddy"]
    current = caddy.get("networks")
    if current is None:
        caddy["networks"] = ["default", "crptochat"]
    elif isinstance(current, list):
        if "crptochat" not in current:
            caddy["networks"] = current + ["crptochat"]
    elif isinstance(current, dict):
        current.setdefault("crptochat", {})
    return want


def _normalize_networks(svc_nets):
    if isinstance(svc_nets, list):
        return {n: None for n in svc_nets}
    return {k: (v or None) for k, v in (svc_nets or {}).items()}


def _same_compose(a: dict, b: dict) -> bool:
    a, b = dict(a), dict(b)
    for doc in (a, b):
        doc["services"] = {k: dict(v) for k, v in doc.get("services", {}).items()}
        for svc in doc["services"].values():
            if "networks" in svc:
                svc["networks"] = _normalize_networks(svc["networks"])
    return a == b


def check_caddy_compose(original: str, edited: str) -> None:
    """The edited file must mean the original plus the crptochat network (needs PyYAML)."""
    import yaml
    before, after = yaml.safe_load(original), yaml.safe_load(edited)
    if not _same_compose(after, _expected_compose(before)):
        raise DeployError("edição do compose do Caddy mudou mais do que a rede crptochat")
    nets = after.get("networks") or {}
    if (nets.get("crptochat") or {}).get("external") is not True:
        raise DeployError("networks.crptochat.external: true ausente")
    if "crptochat" not in _normalize_networks(after["services"]["caddy"].get("networks")):
        raise DeployError("serviço caddy não ligado à rede crptochat")


def ensure_caddy_compose_network(compose_text: str) -> str:
    """networks.crptochat.external: true at the top level, and crptochat in
    services.caddy.networks (keeping `default` when the service had no networks).
    Idempotent; comments and the rest of the file are kept as they are."""
    lines = compose_text.splitlines()
    _, top = _keys(lines, 0, len(lines))
    if "services" not in top:
        raise DeployError("compose do Caddy sem services:")
    s_line, s_end, _ = top["services"]
    _, services = _keys(lines, s_line + 1, s_end)
    if "caddy" not in services:
        raise DeployError("compose do Caddy sem serviço caddy")
    c_line, c_end, _ = services["caddy"]
    lines = _caddy_networks_edit(lines, c_line, c_end)
    lines = _toplevel_networks_edit(lines)
    out = "\n".join(lines) + ("\n" if compose_text.endswith("\n") or not compose_text else "")
    try:
        check_caddy_compose(compose_text, out)
    except ImportError:            # no PyYAML on the VM: the bootstrap checks with `docker compose config`
        pass
    return out


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


def cmd_caddy_compose(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    try:
        text = ensure_caddy_compose_network(args.inp.read_text(encoding="utf-8"))
    except DeployError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1
    args.out.write_text(text, encoding="utf-8")
    return 0


def check_compose_config_json(config_json: str) -> None:
    """What `docker compose config --format json` says about the edited file."""
    import json
    cfg = json.loads(config_json)
    net = (cfg.get("networks") or {}).get("crptochat") or {}
    if net.get("external") is not True:
        raise DeployError("docker compose config: rede crptochat não é external")
    caddy_nets = ((cfg.get("services") or {}).get("caddy") or {}).get("networks") or {}
    if "crptochat" not in caddy_nets:
        raise DeployError("docker compose config: caddy fora da rede crptochat")


def cmd_check_compose_config(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, required=True)
    args = ap.parse_args(argv)
    try:
        check_compose_config_json(args.json.read_text(encoding="utf-8"))
    except (DeployError, ValueError) as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_verify_assets(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--only", action="append", default=None, help="só este arquivo (pode repetir)")
    args = ap.parse_args(argv)
    try:
        verify_downloaded_assets(args.manifest, args.dir, args.only)
    except (DeployError, update.UpdateError) as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_list_assets(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--except", dest="skip", action="append", default=[])
    args = ap.parse_args(argv)
    info = update.parse_manifest(args.manifest.read_bytes())
    for name in sorted(info["files"]):
        if name not in args.skip:
            print(name)
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
        print("uso: deploy_lib.py verify-release | env-check | render-compose | caddy-ws | caddy-compose | "
              "check-compose-config | root-index",
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
    if cmd == "verify-assets":
        return cmd_verify_assets(rest)
    if cmd == "list-assets":
        return cmd_list_assets(rest)
    if cmd == "caddy-compose":
        return cmd_caddy_compose(rest)
    if cmd == "check-compose-config":
        return cmd_check_compose_config(rest)
    print(f"comando desconhecido: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
"""Lógica Python do deploy na VM (RELEASE, git, env, volume padrão)."""

import hashlib
import importlib.util
import subprocess  # nosec B404
from pathlib import Path

import pytest

from crptochat import update

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("deploy_lib", ROOT / "packaging" / "deploy_lib.py")
deploy_lib = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(deploy_lib)
RELEASE_KEY = deploy_lib.SIGNING_PUBKEY_DEFAULT


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)  # nosec B603 B607
    subprocess.run(["git", "config", "user.email", "t@ex"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, capture_output=True)
    (repo / "crptochat").mkdir()
    (repo / "crptochat" / "__init__.py").write_text('__version__ = "1.21.0"\n', encoding="utf-8")
    (repo / "crptochat" / "update.py").write_bytes((ROOT / "crptochat" / "update.py").read_bytes())
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()  # nosec B603 B607
    return repo, sha[:7]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_signed_release(tmp_path, version: str, commit: str, files: dict[str, bytes], private: str) -> tuple[Path, Path]:
    folder = tmp_path / "rel"
    folder.mkdir()
    manifest = update.make_manifest(version, commit, 1, {n: _sha(d) for n, d in files.items()})
    (folder / "RELEASE").write_bytes(manifest)
    sig = update.sign(manifest, private).encode("ascii")
    (folder / "RELEASE.sig").write_text(sig.decode(), encoding="ascii")
    for name, data in files.items():
        (folder / name).write_bytes(data)
    return folder / "RELEASE", folder / "RELEASE.sig"


def test_release_commit_must_be_on_main_not_a_tag_checkout(git_repo, tmp_path):
    """Ponto 1: commit do RELEASE precisa ser ancestral de main; não usa refs/tags."""
    repo, short = git_repo
    private, public = update.generate_key()
    manifest, sig = make_signed_release(
        tmp_path, "1.21.0", short, {"crptochat-linux-amd64": b"x" * 8}, private,
    )
    info = deploy_lib.verify_release_files(manifest, sig, public, "v1.21.0")
    deploy_lib.commit_is_ancestor_of_main(repo, info["commit"])
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=other, check=True, capture_output=True)  # nosec B603 B607
    subprocess.run(["git", "config", "user.email", "t@ex"], cwd=other, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=other, check=True, capture_output=True)
    (other / "f").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "f"], cwd=other, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "o"], cwd=other, check=True, capture_output=True)
    alien = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=other, text=True).strip()[:7]  # nosec B603 B607
    with pytest.raises(deploy_lib.DeployError, match="ancestral"):
        deploy_lib.commit_is_ancestor_of_main(repo, alien)

    # refs/tags no crptochat-oss não são usados — o fluxo resolve só o commit do manifesto
    text = (ROOT / "packaging" / "crptochat-deploy.sh").read_text(encoding="utf-8")
    assert "refs/tags" not in text
    assert "git_sync_commit" in text


def test_release_version_must_match_tag_and_package(git_repo, tmp_path):
    """Ponto 2: RELEASE.version = tag e = __version__ no commit."""
    repo, short = git_repo
    private, public = update.generate_key()
    manifest, sig = make_signed_release(tmp_path, "1.21.0", short, {"a": b"z" * 4}, private)
    deploy_lib.verify_release_files(manifest, sig, public, "v1.21.0")
    with pytest.raises(deploy_lib.DeployError, match="version"):
        deploy_lib.verify_release_files(manifest, sig, public, "v9.9.9")
    pkg = deploy_lib.version_at_commit(repo, short)
    deploy_lib.assert_version_matches("1.21.0", pkg)


def test_prod_env_defaults_and_paths():
    """Ponto 5: defaults de produção e validação de caminhos."""
    env = deploy_lib.validate_deploy_env({})
    assert env["CRPTOCHAT_COMPOSE_PROJECT"] == "crptochat-setup"
    assert env["CRPTOCHAT_DATA_VOLUME"] == "crptochat-setup_crptochat-data"
    assert env["CRPTOCHAT_COMPOSE_FILE"] == "/opt/crptochat-setup/docker-compose.isolated.yml"
    deploy_sh = (ROOT / "packaging" / "crptochat-deploy.sh").read_text(encoding="utf-8")
    assert 'docker compose -p "$COMPOSE_PROJECT"' in deploy_sh
    assert "crptochat-setup" in deploy_sh
    with pytest.raises(deploy_lib.DeployError):
        deploy_lib.validate_deploy_env({"CRPTOCHAT_COMPOSE_PROJECT": "bad name"})


def test_first_run_requires_state_or_container():
    """Ponto 6: snapshot restore; seed_state e fail closed no script."""
    deploy = (ROOT / "packaging" / "crptochat-deploy.sh").read_text(encoding="utf-8")
    assert "seed_state_if_missing" in deploy
    assert "snapshot_restore" in deploy
    assert "primeiro deploy" in deploy


def test_render_compose_external_volume(tmp_path):
    tmpl = ROOT / "docker-compose.isolated.yml"
    out = tmp_path / "compose.yml"
    deploy_lib.render_compose(tmpl, out, "crptochat-setup_crptochat-data")
    text = out.read_text(encoding="utf-8")
    assert "networks:\n  crptochat:\n    external: true\n    name: crptochat\n" in text
    assert "volumes:\n  crptochat-data:\n    external: true\n    name: crptochat-setup_crptochat-data\n" in text
    assert "7777:7777" in text
    assert "127.0.0.1:7778" not in text
    template = tmpl.read_text(encoding="utf-8")
    assert text.split("\nnetworks:")[0] == template.split("\nnetworks:")[0]       # services untouched
    assert deploy_lib.render_compose_text(text, "crptochat-setup_crptochat-data") == text   # idempotent


# #31's template: the network managed by compose; the old regex render left it so
OLD_TEMPLATE = """\
services:
  server:
    image: crptochat-server
    networks:
      - crptochat
    volumes:
      - crptochat-data:/data

networks:
  crptochat:
    name: crptochat
    driver: bridge

volumes:
  crptochat-data:

# a section after volumes (the old regex cut everything from volumes: to the end)
x-notes:
  keep: me
"""


def test_render_compose_by_structure_keeps_what_follows():
    out = deploy_lib.render_compose_text(OLD_TEMPLATE, "vol_x")
    assert "networks:\n  crptochat:\n    external: true\n    name: crptochat\n" in out
    assert "driver: bridge" not in out                          # the managed definition is replaced, not merged
    assert "volumes:\n  crptochat-data:\n    external: true\n    name: vol_x\n" in out
    assert out.endswith("x-notes:\n  keep: me\n")                # nothing after volumes is lost
    assert out.split("\nnetworks:")[0] == OLD_TEMPLATE.split("\nnetworks:")[0]
    without = OLD_TEMPLATE.split("\nnetworks:")[0] + "\n"
    added = deploy_lib.render_compose_text(without, "vol_x")
    assert "\nnetworks:\n  crptochat:\n    external: true\n" in added and "\nvolumes:\n  crptochat-data:\n" in added
    with pytest.raises(deploy_lib.DeployError):
        deploy_lib.render_compose_text("volumes:\n  a:\n", "vol_x")
    with pytest.raises(deploy_lib.DeployError):
        deploy_lib.render_compose_text(OLD_TEMPLATE, "Vol X")


def server_config(**over):
    cfg = {
        "networks": {"crptochat": {"name": "crptochat", "external": True}},
        "volumes": {"crptochat-data": {"name": "v", "external": True}},
        "services": {"server": {
            "networks": {"crptochat": None},
            "volumes": [{"type": "volume", "source": "crptochat-data", "target": "/data"}],
            "ports": [{"mode": "ingress", "target": 7777, "published": "7777", "protocol": "tcp"}],
            "command": ["--data", "/data", "--ws-port", "7778", "--ws-origin", "https://o.example"],
        }},
    }
    for path, value in over.items():
        node = cfg
        keys = path.split("__")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value
    import json
    return json.dumps(cfg)


def test_server_compose_config_check():
    got = deploy_lib.check_server_config_json(server_config(), "v")
    assert got == {"ws_port": "7778", "ws_origin": "https://o.example"}
    for over in ({"networks__crptochat": {"name": "crptochat-setup_crptochat"}},   # managed by compose: the bug
                 {"networks__crptochat": {"name": "outra", "external": True}},
                 {"volumes__crptochat-data": {"name": "crptochat-setup_crptochat-data"}},
                 {"volumes__crptochat-data": {"name": "w", "external": True}},
                 {"services__server__networks": {"default": None}},
                 {"services__server__volumes": []},
                 {"services__server__ports": [{"target": 7777, "published": "17777"}]},
                 {"services__server__command": ["--data", "/data"]},
                 {"services__server__command": ["--ws-port", "7779"]}):
        with pytest.raises(deploy_lib.DeployError):
            deploy_lib.check_server_config_json(server_config(**over), "v")


@pytest.mark.parametrize("line, bad", [
    (" Container crptochat  Recreate", False),
    ("DRY-RUN MODE -  Container crptochat  Recreated", False),
    (" Container crptochat  Starting", False),
    ("WARN[0000] Found orphan containers ([crptochat-caddy]) for this project. If you removed or renamed this "
     "service in your compose file, you can run this command with the --remove-orphans flag to clean it up.", False),
    (" Network crptochat  Recreate", True),
    (" Network crptochat  Removing", True),
    (" Network crptochat-setup_default  Creating", True),
    (' Volume "crptochat-setup_crptochat-data"  Creating', True),
    (" Volume crptochat-data  Removed", True),
    (" Container crptochat-caddy  Stopping", True),
    (" Container crptochat-caddy  Recreate", True),
])
def test_dry_run_check(line, bad):
    assert bool(deploy_lib.check_dry_run(line + "\n", "crptochat")) is bad


def test_asset_names_are_a_closed_list(tmp_path):
    files = {"crptochat-linux-amd64": b"a", "evil.sh": b"b"}
    manifest = update.make_manifest("1.0.0", "abc1234", 1, {n: _sha(d) for n, d in files.items()})
    (tmp_path / "RELEASE").write_bytes(manifest)
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    with pytest.raises(deploy_lib.DeployError, match="conhecido"):
        deploy_lib.verify_downloaded_assets(tmp_path / "RELEASE", tmp_path, ["evil.sh"])
    deploy_lib.verify_downloaded_assets(tmp_path / "RELEASE", tmp_path, ["crptochat-linux-amd64"])
    import sys
    r = subprocess.run([sys.executable, str(ROOT / "packaging" / "deploy_lib.py"), "list-assets",  # nosec B603
                        "--manifest", str(tmp_path / "RELEASE")], capture_output=True, text=True,
                       env={"PYTHONPATH": str(ROOT)})
    assert r.returncode == 1 and "evil.sh" in r.stderr and r.stdout == ""


def test_caddy_ws_inside_site_block():
    src = (
        "crptochat.acpt.app.br {\n"
        "    encode gzip\n"
        "    file_server\n"
        "}\n"
    )
    out = deploy_lib.insert_caddy_ws_handle(src)
    assert "handle /ws" in out
    assert out.index("handle /ws") < out.rindex("}")
    assert "reverse_proxy crptochat:7778" in out


def test_root_index_points_downloads_assets():
    html = '<a href="crptochat-linux-amd64" download>x</a> <a href="RELEASE">r</a>'
    root = deploy_lib.root_index_from_downloads(html)
    assert 'href="downloads/crptochat-linux-amd64"' in root
    assert 'href="downloads/RELEASE"' in root


def test_verify_downloaded_assets(tmp_path):
    private, _ = update.generate_key()
    data = b"binary"
    name = "crptochat-linux-amd64"
    digest = _sha(data)
    manifest = update.make_manifest("1.0.0", "abc1234", 1, {name: digest})
    mpath = tmp_path / "RELEASE"
    mpath.write_bytes(manifest)
    (tmp_path / name).write_bytes(data)
    deploy_lib.verify_downloaded_assets(mpath, tmp_path)
    (tmp_path / name).write_bytes(b"tampered")
    with pytest.raises(deploy_lib.DeployError):
        deploy_lib.verify_downloaded_assets(mpath, tmp_path)
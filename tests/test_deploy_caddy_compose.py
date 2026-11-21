"""Structural edit of the Caddy compose file on the VM (deploy_lib.ensure_caddy_compose_network)."""

import importlib.util
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")   # PyYAML is a dev extra; the VM's python3 does not have it

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("deploy_lib", ROOT / "packaging" / "deploy_lib.py")
deploy_lib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deploy_lib)
edit = deploy_lib.ensure_caddy_compose_network

# Like /opt/crptochat-setup/docker-compose.caddy.yml on the VM: the caddy service
# binds the Caddyfile (a file bind) and `volumes:` is the LAST section.
VM_FILE = """\
# Caddy na frente do crptochat (TLS do site e wss)
services:
  caddy:
    image: caddy:2.8.4
    container_name: crptochat-caddy
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - /opt/crptochat-setup/Caddyfile:/etc/caddy/Caddyfile:ro   # bind de arquivo
      - /opt/crptochat-site:/srv/site:ro
      - caddy_data:/data
      - caddy_config:/config

volumes:
  caddy_data:
  caddy_config:
"""


def nets_of_caddy(doc):
    n = doc["services"]["caddy"].get("networks")
    return list(n) if isinstance(n, (list, dict)) else n


def test_vm_shape_network_at_top_level_and_caddy_joined():
    out = edit(VM_FILE)
    doc, before = yaml.safe_load(out), yaml.safe_load(VM_FILE)
    assert doc["networks"] == {"crptochat": {"external": True}}            # top level, not under volumes
    assert doc["volumes"] == before["volumes"]                             # volumes untouched
    assert nets_of_caddy(doc) == ["default", "crptochat"]                  # keeps default: it had no networks
    caddy = dict(doc["services"]["caddy"])
    caddy.pop("networks")
    assert caddy == before["services"]["caddy"]                            # nothing else in the service changed
    assert out.startswith("# Caddy na frente") and "# bind de arquivo" in out   # comments kept
    assert out.splitlines()[:14] == VM_FILE.splitlines()[:14]             # lines before the edit untouched
    assert edit(out) == out                                                # idempotent


def test_the_bug_from_the_line_based_edit_is_gone():
    """`crptochat` inside volumes (crptochat-data, the site path) is not the network."""
    text = VM_FILE.replace("  caddy_config:\n", "  caddy_config:\n  crptochat-data:\n    external: true\n")
    doc = yaml.safe_load(edit(text))
    assert "crptochat" in doc["networks"] and "crptochat" not in doc["volumes"]
    assert doc["volumes"]["crptochat-data"] == {"external": True}
    assert "crptochat" in nets_of_caddy(doc)


def test_long_bind_syntax_and_compact_list():
    text = """\
services:
  caddy:
    image: caddy:2
    volumes:
    - type: bind
      source: /opt/crptochat-setup/Caddyfile
      target: /etc/caddy/Caddyfile
    networks:
    - web
networks:
  web: {}
volumes:
  caddy_data: {}
"""
    doc = yaml.safe_load(edit(text))
    assert nets_of_caddy(doc) == ["web", "crptochat"]                     # no default added: it had networks
    assert doc["networks"] == {"web": {}, "crptochat": {"external": True}}
    assert doc["services"]["caddy"]["volumes"][0]["type"] == "bind"


def test_mapping_and_flow_forms():
    mapping = "services:\n  caddy:\n    image: c\n    networks:\n      front:\n        aliases: [x]\n"
    doc = yaml.safe_load(edit(mapping))
    assert set(doc["services"]["caddy"]["networks"]) == {"front", "crptochat"}
    assert doc["services"]["caddy"]["networks"]["front"] == {"aliases": ["x"]}
    flow = "services:\n  caddy:\n    image: c\n    networks: [front]\n"
    assert nets_of_caddy(yaml.safe_load(edit(flow))) == ["front", "crptochat"]


def test_already_done_is_left_alone():
    done = edit(VM_FILE)
    assert edit(done) == done
    other = done.replace("    external: true\n", "    driver: bridge\n")
    with pytest.raises(deploy_lib.DeployError, match="external"):
        edit(other)                                   # never guesses over an existing, different network


@pytest.mark.parametrize("text, why", [
    ("services:\n  web:\n    image: x\n", "sem serviço caddy"),
    ("volumes:\n  a:\n", "sem services"),
    ("services:\n  caddy:\n    image: c\n    network_mode: host\n", "network_mode"),
])
def test_refuses_what_it_cannot_do_safely(text, why):
    with pytest.raises(deploy_lib.DeployError, match=why):
        edit(text)


def test_compose_config_check():
    ok = '{"networks": {"crptochat": {"name": "crptochat", "external": true}, "default": {}},' \
         ' "services": {"caddy": {"networks": {"default": null, "crptochat": null}}}}'
    deploy_lib.check_compose_config_json(ok)
    with pytest.raises(deploy_lib.DeployError):
        deploy_lib.check_compose_config_json(ok.replace('"external": true', '"external": false'))
    with pytest.raises(deploy_lib.DeployError):
        deploy_lib.check_compose_config_json(ok.replace('"crptochat": null', '"x": null'))


def test_cli(tmp_path):
    src, out = tmp_path / "in.yml", tmp_path / "out.yml"
    src.write_text(VM_FILE)
    import subprocess  # nosec B404
    import sys
    r = subprocess.run([sys.executable, str(ROOT / "packaging" / "deploy_lib.py"), "caddy-compose",  # nosec B603
                        "--in", str(src), "--out", str(out)], capture_output=True, text=True,
                       env={"PYTHONPATH": str(ROOT)})
    assert r.returncode == 0, r.stderr
    assert out.read_text() == edit(VM_FILE)

"""Requisitos do script de deploy na VM (packaging/crptochat-deploy.sh)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "packaging" / "crptochat-deploy.sh"
RELEASE_PUBKEY = "UXnFSaezyEwMqfhfGncgjf03YiSN/FvaIja1Kv6g5VE="
BOOT = ROOT / "packaging" / "crptochat-bootstrap.sh"


def test_deploy_script_prod_defaults_and_guards():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "/opt/crptochat-setup/docker-compose.isolated.yml" in text
    assert "flock" in text
    assert "ensure_volume" in text
    assert "refs/tags" not in text
    assert "snapshot_create" in text
    assert "snapshot_restore" in text
    assert RELEASE_PUBKEY in text
    assert "CRPTOCHAT_SMOKE_WS_BEHIND_PROXY=1" in text
    assert 'docker compose -p "$COMPOSE_PROJECT"' in text
    assert "crptochat-setup_crptochat-data" in text
    assert "/etc/crptochat/deploy.env" in text
    assert "crptochat-web-dist.tar.gz" in text
    assert "build_web_dist" not in text
    assert "npm ci" not in text
    assert "trap cleanup_tmp EXIT" in text
    assert "trap \"rm -rf" not in text
    assert "selftest --probe" in text
    assert "publish_discard_prevs" in text


def test_compose_template_exposes_public_relay_and_ws_behind_proxy():
    compose = (ROOT / "docker-compose.isolated.yml").read_text(encoding="utf-8")
    assert re.search(r'^\s*-\s*"7777:7777"', compose, re.M)
    assert "127.0.0.1:7778" not in compose
    assert "--ws-behind-proxy" in compose
    assert "name: crptochat" in compose


def test_bootstrap_matches_vm():
    text = BOOT.read_text(encoding="utf-8")
    assert "crptochat-caddy" in text
    assert "/opt/crptochat-setup/Caddyfile" in text
    assert "docker-compose.caddy.yml" in text
    assert "merge #30" in text
    assert "sed -i" not in text or "Caddyfile" not in text.split("sed -i")[0]
    assert "cat " in text and "CADDYFILE" in text
    assert "DESFAZER.sh" in text
    assert "HAD_ENV" in text
    assert "PIN TLS vazio" in text
    assert "NETWORK=crptochat" in text and 'network connect "$NETWORK"' in text
    assert "|| true" not in text.split('network connect "$NETWORK"')[1].split("\n")[0]
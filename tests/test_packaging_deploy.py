"""Requisitos do script de deploy na VM (packaging/crptochat-deploy.sh)."""

import re
from pathlib import Path

from crptochat import update

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "packaging" / "crptochat-deploy.sh"
RELEASE_PUBKEY = "UXnFSaezyEwMqfhfGncgjf03YiSN/FvaIja1Kv6g5VE="


def test_deploy_script_prod_defaults_and_guards():
    text = DEPLOY.read_text(encoding="utf-8")
    assert "/opt/crptochat-setup/docker-compose.isolated.yml" in text
    assert "flock" in text
    assert "ensure_volume" in text
    assert "refs/tags" not in text
    assert "git_sync_commit" in text
    assert "verify_release_precheck" in text
    assert RELEASE_PUBKEY in text
    assert "CRPTOCHAT_SMOKE_WS_BEHIND_PROXY=1" in text
    assert 'docker compose -p "$COMPOSE_PROJECT"' in text
    assert "crptochat-setup_crptochat-data" in text
    assert "/etc/crptochat/deploy.env" in text
    assert "sync_release_downloads" in text
    assert "sync_site_pages" in text


def test_compose_template_exposes_public_relay_and_ws_behind_proxy():
    compose = (ROOT / "docker-compose.isolated.yml").read_text(encoding="utf-8")
    assert re.search(r'^\s*-\s*"7777:7777"', compose, re.M)
    assert "127.0.0.1:7778" not in compose
    assert "--ws-behind-proxy" in compose
    assert "name: crptochat" in compose


def test_bootstrap_script_exists():
    boot = ROOT / "packaging" / "crptochat-bootstrap.sh"
    text = boot.read_text(encoding="utf-8")
    assert "DESFAZER.sh" in text
    assert "caddy validate" in text
    assert "sed -i" not in text
    assert "network connect crptochat" in text
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
    assert "tag_on_main" in text
    assert "tag_gt" in text
    assert RELEASE_PUBKEY in text
    assert f"SHA256 {update.key_fingerprint(RELEASE_PUBKEY)[:4]}" in text or "aae1" in text.lower()
    assert "CRPTOCHAT_SMOKE_WS_BEHIND_PROXY=1" in text
    assert '--project-directory "$REPO_DIR"' in text
    assert '"7777:7777"' not in text  # published without loopback bind in compose template
    assert "127.0.0.1:7777" not in text


def test_compose_template_exposes_public_relay_and_ws_behind_proxy():
    compose = (ROOT / "docker-compose.isolated.yml").read_text(encoding="utf-8")
    assert re.search(r'^\s*-\s*"7777:7777"', compose, re.M)
    assert "127.0.0.1:7778" not in compose
    assert "--ws-behind-proxy" in compose
    assert "name: crptochat" in compose
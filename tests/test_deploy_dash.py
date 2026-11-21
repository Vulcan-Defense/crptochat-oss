"""The VM's deploy and bootstrap scripts run for real under dash (the VM's /bin/sh).

The deploy test runs the real `main` of packaging/crptochat-deploy.sh, with the
real git (a repository with an `origin`), the real deploy_lib.py and a RELEASE
signed for real. Only docker, curl and the TLS probe are stand-ins: a docker stub
on PATH that keeps the state of the server container (on the shared network or
not, 7777 published or not, which image it runs) and reproduces the production
incident of the first deploy with #31 on demand:

- NET_HASH_MISMATCH=1: the `crptochat` network was created by the old compose
  with `driver: bridge`; a compose that manages the network (not external) sees
  another config hash and wants to recreate it. A real `up` then stops the
  server, takes it off the network and fails to remove the network because the
  Caddy (an "orphan" of the project) still has an endpoint on it.
- RESTORE_UP_NO_NET=1: a later plain `compose up -d` only `start`s the container,
  which comes back with no network and 7777 not published.

Nothing here needs PyYAML: `docker compose config` is tests/compose_json_stub.py.
"""

import hashlib
import io
import os
import re
import shutil
import signal
import stat
import subprocess  # nosec B404
import sys
import tarfile
import time
from pathlib import Path

import pytest

from crptochat import update

ROOT = Path(__file__).resolve().parents[1]
DASH = shutil.which("dash")
pytestmark = pytest.mark.skipif(DASH is None, reason="dash não instalado")

PIN = "ab" * 32
SHORT_PIN = "abab…abab"
OTHER_PIN = "cd" * 32
OLD_IMAGE = "sha256:" + "0" * 64
TEMPLATE = (ROOT / "docker-compose.isolated.yml").read_text()
# what #31 left live on the VM: the network managed by the server's compose
OLD_COMPOSE = re.sub(r"\nnetworks:\n.*\Z", "\nnetworks:\n  crptochat:\n    name: crptochat\n\nvolumes:\n"
                     "  crptochat-data:\n    external: true\n    name: crptochat-setup_crptochat-data\n",
                     re.sub(r"\n# A rede é compartilhada.*?(?=\nnetworks:)", "", TEMPLATE, flags=re.S), flags=re.S)

DOCKER_STUB = r'''#!/bin/sh
# docker for the tests: records every call and keeps the server container's state.
S="$DOCKER_STUB_DIR"
echo "$*" >> "$S/calls.log"
file_of() { echo "$*" | sed -n 's/.* -f \([^ ]*\) .*/\1/p'; }
net_external() { awk '/^networks:/{n=1;next} /^[^ #]/{n=0} n&&/external: true/{f=1} END{exit !f}' "$1"; }
case "$1" in
  volume)
    case "$*" in *Mountpoint*) echo "$S/volume" ;; esac
    exit 0 ;;
  network)
    case "$2" in inspect) [ -f "$S/net" ] && exit 0; exit 1 ;; esac
    exit 0 ;;
  port)
    [ -f "$S/attached" ] && echo "0.0.0.0:7777"
    exit 0 ;;
  inspect)
    case "$*" in
      *NetworkSettings.Networks*) [ -f "$S/attached" ] && printf 'crptochat '; echo ;;
      *RestartCount*)
        n=$(cat "$S/restart_reads" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$S/restart_reads"
        if [ "${FAIL_AT:-}" = health ]; then echo 1
        elif [ "${FAIL_AT:-}" = second ] && [ "$n" -gt 1 ]; then echo 1
        else echo 0; fi ;;
      *State.Running*) echo true ;;
      *'{{.Image}}'*) [ -n "${IMAGE_ID_EMPTY:-}" ] || cat "$S/image_id" ;;
      *ExitCode*) echo "ExitCode=0 OOMKilled=false Error=" ;;
    esac
    exit 0 ;;
  logs)
    printf 'crptochat relay v%s\n  fingerprint %s\n' "$(cat "$S/running_version")" "$PIN"
    [ -n "${NO_WEBGATE:-}" ] || echo "  cliente web: ws atrás do proxy (rede privada) em 0.0.0.0:7778 · origens: https://x"
    exit 0 ;;
  exec) echo "${CONTAINER_VERSION:-$(cat "$S/running_version")}"; exit 0 ;;
  tag)
    echo "$2 $3" >> "$S/tags.log"
    [ "$2" = "$(cat "$S/image_id")" ] && echo 9.8.0 > "$S/image_version"
    exit 0 ;;
  run)
    case "$*" in
      *"--network crptochat"*) [ -f "$S/attached" ] && [ "${FAIL_AT:-}" != ws ] && exit 0; exit 1 ;;
    esac
    exit 0 ;;
  compose)
    f=$(file_of "$*")
    case "$*" in
      *" config --format json"*)
        exec python3 "$S/compose_json_stub.py" "$f" crptochat-setup ;;
      *" build"*)
        if [ "${FAIL_AT:-}" = build ]; then echo "build falhou de propósito" >&2; exit 1; fi
        if [ -n "${HANG_AT_BUILD:-}" ]; then echo $$ > "$S/hanging"; sleep 60; exit 0; fi
        sed -n 's/^__version__ = "\(.*\)"/\1/p' "$CRPTOCHAT_REPO_DIR/crptochat/__init__.py" > "$S/image_version"
        exit 0 ;;
      *"--dry-run"*)
        if [ -n "${NET_HASH_MISMATCH:-}" ] && ! net_external "$f"; then
          echo " Network crptochat  Recreate"
        fi
        [ -z "${DRY_RUN_EXTRA:-}" ] || printf '%s\n' "$DRY_RUN_EXTRA"
        echo "DRY-RUN MODE -  Container crptochat  Recreate"
        exit 0 ;;
      *" up "*)
        echo "$*" >> "$S/ups.log"
        n=$(wc -l < "$S/ups.log")
        if [ -n "${NET_HASH_MISMATCH:-}" ] && ! net_external "$f"; then
          # the incident: server stopped and disconnected, the network cannot go
          rm -f "$S/attached"
          echo "Error response from daemon: error while removing network: network crptochat has active endpoints (name:\"crptochat-caddy\")" >&2
          exit 1
        fi
        if [ "${FAIL_AT:-}" = up ] && [ "$n" = 1 ]; then rm -f "$S/attached"; echo "up falhou de propósito" >&2; exit 1; fi
        case "$*" in
          *--force-recreate*)
            [ -n "${FORCE_RECREATE_FAILS:-}" ] && { rm -f "$S/attached"; exit 1; } ;;
          *)
            if [ -n "${RESTORE_UP_NO_NET:-}" ] && [ "$n" -gt 1 ]; then
              rm -f "$S/attached"; exit 0          # only `start`: no endpoint, 7777 not published
            fi ;;
        esac
        cp "$S/image_version" "$S/running_version"
        touch "$S/attached"
        exit 0 ;;
    esac
    exit 0 ;;
esac
exit 0
'''

CURL_STUB = r'''#!/bin/sh
# curl for the tests: serves the release assets from $RELEASE_DIR by file name.
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out=$2; shift 2 ;; --max-time) shift 2 ;; -*) shift ;; *) url=$1; shift ;; esac
done
src="$RELEASE_DIR/$(basename "$url")"
[ -f "$src" ] || exit 22
cp "$src" "$out"
'''

# the TLS probe answers only while the server is on the network with 7777 published
SELFTEST_STUB = '''import os, sys
S = os.environ.get("DOCKER_STUB_DIR", "")
if not os.path.exists(os.path.join(S, "attached")):
    print("falhou 127.0.0.1:7777 sem resposta")
    sys.exit(1)
pin = os.environ["PIN"]
with open(os.path.join(S, "running_version")) as fh:
    if fh.read().strip() == "9.9.0":
        pin = os.environ.get("PROBE_PIN", pin)        # the new image answers with another PIN
print("ok    127.0.0.1:7777 responde em TLS 1.3 · fingerprint " + pin)
'''


def run(cmd, cwd=None, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, check=True, capture_output=True, text=True)  # nosec B603


def git(repo, *args):
    return run(["git", *args], cwd=repo).stdout.strip()


def sign_release(rel, version, commit, built, private):
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in rel.iterdir()
             if p.name not in ("RELEASE", "RELEASE.sig")}
    manifest = update.make_manifest(version, commit, built, files)
    (rel / "RELEASE").write_bytes(manifest)
    (rel / "RELEASE.sig").write_text(update.sign(manifest, private))


@pytest.fixture
def vm(tmp_path):
    """A VM in miniature: the repo with origin, the site, the compose file, the state, the stubs."""
    origin = tmp_path / "origin.git"
    run(["git", "init", "--bare", "-b", "main", str(origin)])
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-b", "main")
    git(work, "config", "user.email", "t@ex")
    git(work, "config", "user.name", "t")
    shutil.copytree(ROOT / "crptochat", work / "crptochat", ignore=shutil.ignore_patterns("__pycache__"))
    (work / "crptochat" / "selftest.py").write_text(SELFTEST_STUB)
    (work / "packaging").mkdir()
    for name in ("deploy_lib.py", "stamp_site.py"):
        shutil.copy(ROOT / "packaging" / name, work / "packaging" / name)
    smoke = work / "packaging" / "server_smoke_restricted.sh"
    smoke.write_text("#!/bin/sh\nexit 0\n")              # the real smoke is tested on its own
    smoke.chmod(0o755)
    shutil.copytree(ROOT / "site", work / "site")
    shutil.copy(ROOT / "docker-compose.isolated.yml", work / "docker-compose.isolated.yml")

    def commit(version):
        (work / "crptochat" / "__init__.py").write_text(f'"""crptochat"""\n\n__version__ = "{version}"\n')
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", version)
        git(work, "push", "-q", str(origin), "main")
        return git(work, "rev-parse", "HEAD")

    old_commit = commit("9.8.0")
    repo = tmp_path / "opt-crptochat"
    run(["git", "clone", "-q", str(origin), str(repo)])
    new_commit = commit("9.9.0")                         # released; the VM has not fetched it yet

    # the release v9.9.0, signed
    rel = tmp_path / "release"
    rel.mkdir()
    (rel / "crptochat-linux-amd64").write_bytes(b"\x7fELF binario novo")
    (rel / "install.sh").write_text("#!/bin/sh\necho instalar\n")
    web = io.BytesIO()
    with tarfile.open(fileobj=web, mode="w:gz") as tar:
        for name, data in (("dist/index.html", b"web-NOVO"), ("dist/b/1/main.js", b"js")):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600      # as packed under umask 077
            tar.addfile(info, io.BytesIO(data))
    (rel / "crptochat-web-dist.tar.gz").write_bytes(web.getvalue())
    private, public = update.generate_key()
    built = int(time.time())
    sign_release(rel, "9.9.0", new_commit, built, private)

    # what is live on the VM before the deploy
    site = tmp_path / "site"
    (site / "downloads").mkdir(parents=True)
    (site / "web").mkdir()
    (site / "index.html").write_text("site-ORIGINAL")
    (site / "downloads" / "index.html").write_text("downloads-ORIGINAL")
    (site / "downloads" / "crptochat-linux-amd64").write_bytes(b"binario-ORIGINAL")
    (site / "web" / "index.html").write_text("web-ORIGINAL")
    compose = tmp_path / "setup" / "docker-compose.isolated.yml"
    compose.parent.mkdir()
    compose.write_text(OLD_COMPOSE)
    state = tmp_path / "state" / "deploy.state"
    state.parent.mkdir()
    state.write_text(f"prev_tag=v9.8.0\ntls_pin={PIN}\n")

    stub = tmp_path / "stub"
    (stub / "bin").mkdir(parents=True)
    (stub / "volume").mkdir()
    (stub / "volume" / "registry.db").write_text("dados")
    (stub / "image_id").write_text(OLD_IMAGE + "\n")
    (stub / "running_version").write_text("9.8.0\n")
    (stub / "image_version").write_text("9.8.0\n")
    (stub / "net").touch()                                # created by the bootstrap
    (stub / "attached").touch()                           # the server is up, on the network, 7777 published
    shutil.copy(ROOT / "tests" / "compose_json_stub.py", stub / "compose_json_stub.py")
    for name, body in (("docker", DOCKER_STUB), ("curl", CURL_STUB)):
        (stub / "bin" / name).write_text(body)
        (stub / "bin" / name).chmod(0o755)

    env = {
        "PATH": f"{stub / 'bin'}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "SSH_ORIGINAL_COMMAND": "v9.9.0",
        "PIN": PIN,
        "DOCKER_STUB_DIR": str(stub),
        "RELEASE_DIR": str(rel),
        "CLIENT_SIGNING_PUBKEY": public,
        "CRPTOCHAT_DEPLOY_ENV": str(tmp_path / "nao-existe.env"),
        "CRPTOCHAT_REPO_DIR": str(repo),
        "CRPTOCHAT_COMPOSE_FILE": str(compose),
        "CRPTOCHAT_SITE_ROOT": str(site),
        "CRPTOCHAT_STATE_FILE": str(state),
        "CRPTOCHAT_BACKUP_DIR": str(tmp_path / "backups"),
        "CRPTOCHAT_DEPLOY_LOG_DIR": str(tmp_path / "logs"),
        "CRPTOCHAT_DEPLOY_LOCK": str(tmp_path / "deploy.lock"),
        "CRPTOCHAT_HEALTH_WAIT": "0",
        "CRPTOCHAT_HEALTH_SECOND_WAIT": "0",
        "CRPTOCHAT_PROBE_TRIES": "1",
    }
    return dict(env=env, site=site, compose=compose, state=state, stub=stub, repo=repo, rel=rel,
                commit=new_commit, old_commit=old_commit, private=private, built=built, logs=tmp_path / "logs")


def deploy(vm, **knobs):
    env = dict(vm["env"])
    env.update({k.upper(): v for k, v in knobs.items()})
    # cwd: somewhere neutral, like the forced command's home on the VM (not a checkout)
    return subprocess.run([DASH, str(ROOT / "packaging" / "crptochat-deploy.sh")], env=env,  # nosec B603
                          capture_output=True, text=True, timeout=120, cwd=env["HOME"])


def tree(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


def inodes(path):
    return {str(p.relative_to(path)): p.stat().st_ino for p in sorted(path.rglob("*"))}


def calls(vm):
    f = vm["stub"] / "calls.log"
    return f.read_text() if f.exists() else ""


def ups(vm):
    f = vm["stub"] / "ups.log"
    return f.read_text().splitlines() if f.exists() else []


def server_up(vm):
    return (vm["stub"] / "attached").exists()


def log_text(vm):
    return "".join(p.read_text() for p in vm["logs"].glob("deploy-*.log"))


def assert_restored(vm, site_before, compose_inode=None):
    assert vm["compose"].read_text() == OLD_COMPOSE                            # compose
    if compose_inode is not None:
        assert vm["compose"].stat().st_ino == compose_inode                     # by rename, not a copy
    assert tree(vm["site"]) == site_before                                      # site: index, downloads, web
    assert not list(vm["site"].rglob(".*.prev")) and not list(vm["site"].rglob(".*.new"))
    tags = (vm["stub"] / "tags.log").read_text().split("\n")
    assert f"{OLD_IMAGE} crptochat-server" in tags                              # image tag back to the old image
    assert vm["state"].read_text() == f"prev_tag=v9.8.0\ntls_pin={PIN}\n"      # state not advanced
    assert git(vm["repo"], "rev-parse", "HEAD") == vm["old_commit"]             # I2: HEAD back
    assert git(vm["repo"], "symbolic-ref", "HEAD") == "refs/heads/main"         # on main, not detached
    assert server_up(vm)                                                        # 7777 back, proven
    assert (vm["stub"] / "running_version").read_text().strip() == "9.8.0"


# ------------------------------------------------------------ production bug

def test_incident_state_no_longer_touches_the_network(vm):
    """Item 1: the VM as the incident left it (network from the old compose, other
    hash, Caddy endpoint on it). The rendered compose has the network external:
    compose does not try to recreate it, and the deploy goes through."""
    r = deploy(vm, net_hash_mismatch="1")
    assert r.returncode == 0, r.stdout + r.stderr
    text = vm["compose"].read_text()
    assert "networks:\n  crptochat:\n    external: true\n    name: crptochat\n" in text
    assert "volumes:\n  crptochat-data:\n    external: true\n    name: crptochat-setup_crptochat-data\n" in text
    assert text.split("\nnetworks:")[0] == TEMPLATE.split("\nnetworks:")[0]   # services and comments kept as is
    assert len(ups(vm)) == 1 and "--remove-orphans" not in calls(vm)
    assert "network rm" not in calls(vm) and server_up(vm)


@pytest.mark.parametrize("plan", [
    " Network crptochat  Recreate",
    " Network crptochat  Removing",
    ' Volume "crptochat-setup_crptochat-data"  Creating',
    " Container crptochat-caddy  Stopping",
])
def test_a_dry_run_that_touches_more_than_the_server_aborts_without_stopping_it(vm, plan):
    """Item 2 / item 4: the dry-run runs before anything is stopped; a plan that
    touches the network, the volume or another container (the Caddy) aborts the
    deploy with the server never stopped, and the restore does not run `up`."""
    site_before, compose_inode = tree(vm["site"]), vm["compose"].stat().st_ino
    r = deploy(vm, dry_run_extra=plan)
    out = r.stdout + r.stderr
    assert r.returncode == 1, out
    assert "dry-run: abortado sem tocar no servidor" in out
    assert plan.strip() in out
    assert ups(vm) == []                                                        # never a real `up`
    assert "--dry-run" in calls(vm)
    assert "servidor não foi tocado pelo deploy e segue no ar" in out
    assert "estado anterior restaurado" in out
    assert_restored(vm, site_before, compose_inode)


def test_the_incident_compose_is_caught_by_the_dry_run():
    """The guard reads compose's plan: the incident's lines are refused, a plain
    recreate of the server container is not."""
    r = subprocess.run([sys.executable, str(ROOT / "packaging" / "deploy_lib.py"), "check-dry-run",  # nosec B603
                        "--file", "/dev/stdin", "--container", "crptochat"],
                       input=" Container crptochat  Recreate\n Container crptochat  Recreated\n", text=True,
                       capture_output=True, env={"PYTHONPATH": str(ROOT)})
    assert r.returncode == 0, r.stdout + r.stderr
    r = subprocess.run([sys.executable, str(ROOT / "packaging" / "deploy_lib.py"), "check-dry-run",  # nosec B603
                        "--file", "/dev/stdin", "--container", "crptochat"],
                       input=" Container crptochat  Stopping\n Network crptochat  Removing\n", text=True,
                       capture_output=True, env={"PYTHONPATH": str(ROOT)})
    assert r.returncode == 1 and "Network crptochat  Removing" in r.stdout


def test_restore_detects_the_container_that_came_back_without_network(vm):
    """Item 3 / item 4: the plain `up` of the restore only starts the container,
    with no network and 7777 not published. The restore notices, recreates the
    server from the snapshot's compose and proves 7777 + PIN."""
    site_before = tree(vm["site"])
    r = deploy(vm, fail_at="health", restore_up_no_net="1")
    out = r.stdout + r.stderr
    assert r.returncode == 1, out
    assert "fora da rede crptochat" in out
    assert ups(vm)[-1].endswith("up -d --no-build --force-recreate server")
    assert "snap/compose.yml" in ups(vm)[-1]                                    # the snapshot's compose
    assert "estado anterior restaurado" in out
    assert_restored(vm, site_before)


def test_restore_that_cannot_prove_it_says_so_with_rc_3(vm):
    r = deploy(vm, fail_at="health", restore_up_no_net="1", force_recreate_fails="1")
    out = r.stdout + r.stderr
    assert r.returncode == 3, out
    assert "RESTORE FALHOU: 7777 possivelmente fora" in r.stderr
    assert "estado anterior restaurado" not in out


def test_incident_restore_never_runs_the_compose_that_breaks_the_network(vm):
    """The snapshot's compose (the #31 one, network not external) would recreate the
    network again: its dry-run blocks it, and the server is recreated on the old
    image with the checked compose (network/volume external)."""
    r = deploy(vm, fail_at="health", net_hash_mismatch="1")
    out = r.stdout + r.stderr
    assert r.returncode == 1, out
    assert all("snap/compose.yml" not in u for u in ups(vm))                    # never `up` with the old one
    assert ups(vm)[-1].endswith("--force-recreate server") and "stage/compose.yml" in ups(vm)[-1]
    assert "estado anterior restaurado" in out and server_up(vm)
    assert (vm["stub"] / "running_version").read_text().strip() == "9.8.0"


@pytest.mark.parametrize("fail_at", ["up", "health"])
def test_a_step_failing_after_the_server_was_touched_restores_everything(vm, fail_at):
    """`compose up` and the health check fail as plain commands (no `fail`): only
    set -e stops the deploy there. The compose comes back by rename (same inode)."""
    site_before, compose_inode = tree(vm["site"]), vm["compose"].stat().st_ino
    r = deploy(vm, fail_at=fail_at)
    out = r.stdout + r.stderr
    assert r.returncode == 1, out
    assert "restaurando snapshot" in out and "estado anterior restaurado" in out
    assert "deploy v9.9.0 concluído" not in out
    assert_restored(vm, site_before, compose_inode)
    if fail_at == "up":
        assert "RestartCount" not in calls(vm).split("up falhou")[0]           # set -e held: no health after it


def test_a_failing_build_stops_before_the_server_is_touched(vm):
    site_before = tree(vm["site"])
    r = deploy(vm, fail_at="build")
    assert r.returncode == 1
    assert ups(vm) == [] and "--dry-run" not in calls(vm)
    assert_restored(vm, site_before)


@pytest.mark.parametrize("drop", ["ports", "networks", "command"])
def test_a_rendered_compose_compose_does_not_confirm_aborts_before_anything(vm, drop):
    """Item 1: what `docker compose config --format json` reads back is checked
    (network and volume external, 7777, --ws-port 7778)."""
    r = deploy(vm, compose_config_drop=drop)
    assert r.returncode == 1
    assert "compose renderizado não confere" in r.stderr
    assert ups(vm) == [] and " build" not in calls(vm)


def test_no_network_no_deploy(vm):
    (vm["stub"] / "net").unlink()
    r = deploy(vm)
    assert r.returncode == 1 and "rode o crptochat-bootstrap.sh" in r.stderr
    assert "compose" not in calls(vm)


# ------------------------------------------------------------ good path, order

def test_a_good_release_is_deployed_in_order_and_published_readable(vm):
    r = deploy(vm)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    state = vm["state"].read_text()
    assert state == f"prev_tag=v9.9.0\ntls_pin={PIN}\nbuilt={vm['built']}\ncommit={vm['commit']}\n"
    assert git(vm["repo"], "rev-parse", "HEAD") == vm["commit"]
    site = vm["site"]
    assert (site / "web" / "index.html").read_bytes() == b"web-NOVO"
    assert (site / "downloads" / "crptochat-linux-amd64").read_bytes() == b"\x7fELF binario novo"
    assert (site / "downloads" / "RELEASE.sig").is_file()
    for p in [site / "web", site / "downloads", *site.joinpath("web").rglob("*"), *site.joinpath("downloads").rglob("*")]:
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == (0o755 if p.is_dir() else 0o644), (p, oct(mode))
    assert stat.S_IMODE((site / "index.html").stat().st_mode) == 0o644
    assert stat.S_IMODE(vm["compose"].stat().st_mode) == 0o644
    assert not list(site.rglob(".*.prev")) and not list(site.parent.glob(".*.prev"))
    assert not list(vm["compose"].parent.glob(".*"))
    assert not (vm["stub"] / "tags.log").exists()                               # no rollback
    # I4: staging -> dry-run -> up -> health (+ WebGate) -> site
    log = log_text(vm)
    c = calls(vm)
    assert c.index("--dry-run") < c.index(" up -d --no-build\n") < c.index("run --rm --network crptochat")
    assert log.index("health: WebGate responde") < log.index("site publicado")
    assert "2ª amostra" in log
    assert "--remove-orphans" not in c


def test_the_site_is_published_only_after_the_health_check(vm):
    """I4: health failing leaves the site exactly as it was (same inodes: never moved)."""
    before = inodes(vm["site"])
    r = deploy(vm, fail_at="health")
    assert r.returncode == 1
    assert inodes(vm["site"]) == before


@pytest.mark.parametrize("knob", ["no_webgate", "fail_at=ws", "fail_at=second"])
def test_health_checks_the_webgate_and_takes_a_second_sample(vm, knob):
    """I7: the behind-proxy log line, a WebSocket handshake from the shared network,
    and RestartCount 0 again at the second sample."""
    key, _, value = knob.partition("=")
    r = deploy(vm, **{key: value or "1"})
    out = r.stdout + r.stderr
    assert r.returncode == 1, out
    expect = {"no_webgate": "WebGate não subiu", "fail_at=ws": "handshake WebSocket",
              "fail_at=second": "2ª amostra"}[knob]
    assert expect in out
    assert "estado anterior restaurado" in out and server_up(vm)


def test_a_pin_change_is_rc_2_and_restored(vm):
    site_before = tree(vm["site"])
    r = deploy(vm, probe_pin=OTHER_PIN)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "PIN TLS cdcd…cdcd != esperado " + SHORT_PIN in r.stdout
    assert "PIN TLS inesperado (estado anterior restaurado)" in r.stderr
    assert_restored(vm, site_before)                                            # old image: old PIN, proven


# ------------------------------------------------------------ review items

@pytest.mark.parametrize("line", ["prev_tag=v9.8.0;touch PWNED", "prev_tag=$(touch PWNED)", "tls_pin=`touch PWNED`",
                                  "prev_tag=9.8.0", "tls_pin=abc"])
def test_state_is_parsed_not_sourced(vm, line):
    """I1: deploy.state is never run as shell code; bad values are refused."""
    key = line.split("=")[0]
    other = f"tls_pin={PIN}" if key == "prev_tag" else "prev_tag=v9.8.0"
    vm["state"].write_text(f"{other}\n{line}\n")
    r = deploy(vm)
    assert r.returncode == 1 and "deploy.state" in r.stderr
    assert not (Path(vm["env"]["HOME"]) / "PWNED").exists()
    assert "compose" not in calls(vm)


@pytest.mark.parametrize("version", ["9.8.0; rm -rf /", "9.8", "x"])
def test_seed_validates_the_container_version(vm, version):
    vm["state"].unlink()
    r = deploy(vm, container_version=version)
    if version == "9.8":                    # two parts is a valid version: seeded, deploy goes on
        assert "primeiro deploy" not in r.stderr
        return
    assert r.returncode == 1 and "versão do contêiner inválida" in r.stderr
    assert not vm["state"].exists()


def test_snapshot_failure_stops_before_anything(vm):
    """I3: the snapshot is not best effort."""
    site_before = tree(vm["site"])
    r = deploy(vm, image_id_empty="1")
    assert r.returncode == 1 and "snapshot" in r.stderr
    assert " build" not in calls(vm) and ups(vm) == []
    assert tree(vm["site"]) == site_before
    assert git(vm["repo"], "rev-parse", "HEAD") == vm["old_commit"]


def test_stdout_has_no_container_logs_and_no_whole_pin(vm):
    """I6: stdout is the public Actions log; the log file (600) has the details."""
    r = deploy(vm, fail_at="health")
    assert r.returncode == 1
    for stream in (r.stdout, r.stderr):
        assert PIN not in stream
        assert "relay v" not in stream and "cliente web:" not in stream
    assert SHORT_PIN in r.stdout
    logs = list(vm["logs"].glob("deploy-*.log"))
    assert logs and stat.S_IMODE(logs[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(vm["logs"].stat().st_mode) == 0o700
    assert "relay v" in log_text(vm)                                            # the diagnostics are in the file


def test_fail_writes_to_stderr(vm):
    r = deploy(vm, ssh_original_command="v1.0.0; id")
    assert r.returncode == 1
    assert "ERRO: deploy.env ou variáveis inválidas" in r.stderr and "ERRO" not in r.stdout
    vm["state"].write_text("prev_tag=lixo\n")
    r = deploy(vm)
    assert r.returncode == 1 and "ERRO" in r.stderr and "ERRO" not in r.stdout


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_a_signal_during_the_deploy_restores(vm, sig):
    """A signal mid-deploy (here, during the build) stops the body and its children
    and runs the verified restore, not just the cleanup."""
    site_before = tree(vm["site"])
    env = dict(vm["env"], HANG_AT_BUILD="1")
    p = subprocess.Popen([DASH, str(ROOT / "packaging" / "crptochat-deploy.sh")], env=env,  # nosec B603
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=env["HOME"])
    hanging = vm["stub"] / "hanging"
    for _ in range(300):
        if hanging.exists() and hanging.read_text().strip():
            break
        time.sleep(0.1)
    else:
        p.kill()
        pytest.fail("o build não começou: " + "".join(p.communicate()))
    p.send_signal(sig)
    out, err = p.communicate(timeout=60)
    assert p.returncode == 1, out + err
    assert "deploy interrompido por sinal" in out
    assert "estado anterior restaurado" in err
    assert not alive(int(hanging.read_text()))                                 # the build was stopped too
    assert_restored(vm, site_before)


def alive(pid):
    """Running (a zombie waiting for a reaper that is not there counts as gone)."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def test_unknown_release_assets_are_refused(vm):
    """Closed list of asset names: a RELEASE listing anything else is not published."""
    (vm["rel"] / "evil.sh").write_text("#!/bin/sh\n")
    sign_release(vm["rel"], "9.9.0", vm["commit"], vm["built"], vm["private"])
    site_before = tree(vm["site"])
    r = deploy(vm)
    assert r.returncode == 1 and "arquivos desconhecidos" in r.stderr
    assert ups(vm) == [] and tree(vm["site"]) == site_before


def test_an_older_release_of_the_same_tag_is_refused(vm):
    assert deploy(vm).returncode == 0
    sign_release(vm["rel"], "9.9.0", vm["commit"], vm["built"] - 60, vm["private"])
    r = deploy(vm)
    assert r.returncode == 1 and "mais antiga" in r.stderr
    sign_release(vm["rel"], "9.9.0", vm["commit"], vm["built"], vm["private"])
    assert deploy(vm).returncode == 0                                           # the same one again is fine


def test_why_the_body_is_not_called_under_or():
    """In dash, a function called under || runs with set -e ignored even inside its own
    `( set -e ... )`: the old `deploy_body ... || rc=$?` would carry on after a failure."""
    script = 'set -eu; f(){ ( set -e; false; echo CONTINUOU ); }; rc=0; f || rc=$?; echo "rc=$rc"'
    r = subprocess.run([DASH, "-c", script], capture_output=True, text=True)  # nosec B603
    assert r.stdout.split() == ["CONTINUOU", "rc=0"]
    main = (ROOT / "packaging" / "crptochat-deploy.sh").read_text().split("main() {", 1)[1]
    assert 'deploy_body "$new_tag" "$commit" "$pin_before" "$manifest_dir" &\n' in main
    assert "deploy_body" not in main.split("set +e", 1)[0]


def test_new_file_is_readable_before_it_is_moved_into_place():
    text = (ROOT / "packaging" / "crptochat-deploy.sh").read_text()
    body = text.split("publish_file_begin() {", 1)[1].split("\n}\n", 1)[0]
    assert body.index('chmod 644 "$new"') < body.index('mv "$new" "$dest"')


# ------------------------------------------------------------------- bootstrap

CADDY_COMPOSE = (ROOT / "tests" / "test_deploy_caddy_compose.py").read_text().split('VM_FILE = """\\\n', 1)[1].split('"""', 1)[0]
CADDYFILE = "crptochat.acpt.app.br {\n    root * /srv/site\n    file_server\n}\n"

BOOT_DOCKER_STUB = r'''#!/bin/sh
S="$DOCKER_STUB_DIR"
echo "$*" >> "$S/calls.log"
case "$*" in
  "network inspect"*) [ -f "$S/net" ] && exit 0 || exit 1 ;;
  "network create"*) touch "$S/net"; exit 0 ;;
  "network rm"*) rm -f "$S/net"; exit 0 ;;
  "network connect"*) touch "$S/connected"; exit 0 ;;
  "network disconnect"*) rm -f "$S/connected"; exit 0 ;;
  *NetworkSettings.Networks*)
    # keys only: a JSON grep would also match "crptochat" in another network's fields
    [ -f "$S/connected" ] && echo 'crptochat default ' || echo 'crptochat_old default '; exit 0 ;;
  *State.Running*) echo true; exit 0 ;;
  *Config.Image*) echo caddy:2.8.4; exit 0 ;;
  inspect*) exit 0 ;;
  logs*) echo "fingerprint ${LOG_PIN:-$PIN}"; exit 0 ;;
  "exec crptochat python"*) echo "${CONTAINER_VERSION:-9.8.0}"; exit 0 ;;
  "run --rm"*) exit 0 ;;
  "compose"*config*)
    exec python3 "$S/compose_json_stub.py" "$(echo "$*" | sed 's/.* -f \([^ ]*\) .*/\1/')" crptochat-setup ;;
  *"caddy validate"*) [ "${FAIL_AT:-}" = validate ] && exit 1; exit 0 ;;
  *"caddy reload"*)
    n=$(cat "$S/reloads" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$S/reloads"
    [ "${FAIL_AT:-}" = reload ] && [ "$n" = 1 ] && exit 1; exit 0 ;;
esac
exit 0
'''


@pytest.fixture
def boot(tmp_path):
    stub = tmp_path / "stub"
    (stub / "bin").mkdir(parents=True)
    (stub / "bin" / "docker").write_text(BOOT_DOCKER_STUB)
    (stub / "bin" / "docker").chmod(0o755)
    (stub / "bin" / "id").write_text("#!/bin/sh\necho 0\n")   # the bootstrap insists on root; CI is not
    (stub / "bin" / "id").chmod(0o755)
    shutil.copy(ROOT / "tests" / "compose_json_stub.py", stub / "compose_json_stub.py")
    setup = tmp_path / "opt-crptochat-setup"
    setup.mkdir()
    (setup / "Caddyfile").write_text(CADDYFILE)
    (setup / "docker-compose.caddy.yml").write_text(CADDY_COMPOSE)
    bak = tmp_path / "bak"
    env = {
        "PATH": f"{stub / 'bin'}:{os.environ['PATH']}", "HOME": str(tmp_path), "PIN": PIN,
        "DOCKER_STUB_DIR": str(stub),
        "CRPTOCHAT_REPO_DIR": str(ROOT),
        "CRPTOCHAT_DEPLOY_ENV": str(tmp_path / "etc" / "deploy.env"),
        "CRPTOCHAT_STATE_FILE": str(tmp_path / "lib" / "deploy.state"),
        "CRPTOCHAT_DEPLOY_BIN": str(tmp_path / "sbin" / "crptochat-deploy"),
        "CRPTOCHAT_CADDYFILE": str(setup / "Caddyfile"),
        "CRPTOCHAT_CADDY_COMPOSE": str(setup / "docker-compose.caddy.yml"),
        "CRPTOCHAT_BACKUP_ROOT": str(bak),
    }
    (tmp_path / "sbin").mkdir()
    return dict(env=env, setup=setup, bak=bak, stub=stub, tmp=tmp_path)


def bootstrap(b, **knobs):
    env = dict(b["env"])
    env.update({k.upper(): v for k, v in knobs.items()})
    return subprocess.run([DASH, str(ROOT / "packaging" / "crptochat-bootstrap.sh")], env=env,  # nosec B603
                          capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("fail_at", ["validate", "reload"])
def test_bootstrap_failing_at_the_caddy_can_be_undone(boot, fail_at):
    caddyfile = boot["setup"] / "Caddyfile"
    inode = caddyfile.stat().st_ino
    r = bootstrap(boot, fail_at=fail_at)
    out = r.stdout + r.stderr
    assert r.returncode != 0 and "para desfazer: sh" in out, out
    assert not (boot["tmp"] / "sbin" / "crptochat-deploy").exists()          # I5: installed only at the end
    desfazer = boot["bak"] / "DESFAZER.sh"
    text = desfazer.read_text()
    assert "Caddyfile" in text and "caddy reload" in text                    # recorded BEFORE the Caddyfile changed
    assert "handle /ws" in caddyfile.read_text()                              # it did change
    assert (boot["stub"] / "connected").exists() and (boot["stub"] / "net").exists()

    u = subprocess.run([DASH, str(desfazer)], env=boot["env"], capture_output=True, text=True)  # nosec B603
    assert u.returncode == 0, u.stdout + u.stderr
    assert caddyfile.read_text() == CADDYFILE and caddyfile.stat().st_ino == inode   # same inode (bind mount)
    assert (boot["setup"] / "docker-compose.caddy.yml").read_text() == CADDY_COMPOSE
    assert not (boot["stub"] / "connected").exists() and not (boot["stub"] / "net").exists()
    for name in ("etc/deploy.env", "lib/deploy.state", "sbin/crptochat-deploy"):
        assert not (boot["tmp"] / name).exists(), name                         # did not exist before


def test_bootstrap_completes_and_is_idempotent(boot):
    r = bootstrap(boot)
    assert r.returncode == 0, r.stdout + r.stderr
    compose = (boot["setup"] / "docker-compose.caddy.yml").read_text()
    assert "    networks:\n      - default\n      - crptochat\n" in compose and compose.endswith("    external: true\n")
    assert "handle /ws" in (boot["setup"] / "Caddyfile").read_text()
    assert (boot["tmp"] / "lib" / "deploy.state").read_text() == f"prev_tag=v9.8.0\ntls_pin={PIN}\n"
    assert PIN not in r.stdout and SHORT_PIN in r.stdout
    deploy_bin = boot["tmp"] / "sbin" / "crptochat-deploy"
    assert deploy_bin.read_bytes() == (ROOT / "packaging" / "crptochat-deploy.sh").read_bytes()
    calls_log = (boot["stub"] / "calls.log").read_text()
    assert "network connect crptochat crptochat-caddy" in calls_log       # crptochat_old is not crptochat
    first = boot["setup"].joinpath("Caddyfile").read_text()
    boot["env"]["CRPTOCHAT_BACKUP_ROOT"] = str(boot["tmp"] / "bak2")
    r2 = bootstrap(boot)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert boot["setup"].joinpath("Caddyfile").read_text() == first
    assert (boot["stub"] / "reloads").read_text().strip() == "1"            # second run: nothing to reload
    assert (boot["stub"] / "calls.log").read_text().count("network connect") == 1


@pytest.mark.parametrize("knob", [{"container_version": "9.8.0; id"}, {"log_pin": "ab" * 31}])
def test_bootstrap_validates_what_it_seeds(boot, knob):
    """I1: the version and the PIN read from the container are validated before
    deploy.state is written."""
    r = bootstrap(boot, **knob)
    assert r.returncode == 1
    assert not (boot["tmp"] / "lib" / "deploy.state").exists()
    assert not (boot["tmp"] / "sbin" / "crptochat-deploy").exists()


def test_bootstrap_refuses_an_old_checkout(boot, tmp_path):
    """I5: a checkout from #30 (deploy_lib.py without the new subcommands) is refused
    before anything changes."""
    old = tmp_path / "old-checkout"
    (old / "packaging").mkdir(parents=True)
    (old / "crptochat").symlink_to(ROOT / "crptochat")
    (old / "packaging" / "deploy_lib.py").write_text(
        "import sys\nsys.exit(0 if sys.argv[1:2] == ['check-compose-config'] else 2)\n")
    shutil.copy(ROOT / "packaging" / "crptochat-deploy.sh", old / "packaging" / "crptochat-deploy.sh")
    boot["env"]["CRPTOCHAT_REPO_DIR"] = str(old)
    r = bootstrap(boot)
    assert r.returncode == 1 and "check-server-config" in r.stderr
    assert not (boot["stub"] / "calls.log").exists()                         # docker never called
    assert not (boot["tmp"] / "sbin" / "crptochat-deploy").exists()

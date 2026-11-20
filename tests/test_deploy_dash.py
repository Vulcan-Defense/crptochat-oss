"""The VM's deploy and bootstrap scripts run for real under dash (the VM's /bin/sh).

The deploy test runs the real `main` of packaging/crptochat-deploy.sh, with the
real git (a repository with an `origin`), the real deploy_lib.py and a RELEASE
signed for real. Only docker, curl and the TLS probe are stand-ins: a stub on
PATH that records what it was asked and can fail on purpose at a given step.
"""

import hashlib
import io
import os
import shutil
import stat
import subprocess  # nosec B404
import tarfile
import time
from pathlib import Path

import pytest

from crptochat import update

ROOT = Path(__file__).resolve().parents[1]
DASH = shutil.which("dash")
pytestmark = pytest.mark.skipif(DASH is None, reason="dash não instalado")

PIN = "ab" * 32
OLD_IMAGE = "sha256:" + "0" * 64

DOCKER_STUB = r'''#!/bin/sh
# docker for the tests: records every call; FAIL_AT makes one step fail.
S="$DOCKER_STUB_DIR"
echo "$*" >> "$S/calls.log"
case "$1" in
  volume)
    case "$*" in *Mountpoint*) echo "$S/volume" ;; esac
    exit 0 ;;
  inspect)
    case "$*" in
      *RestartCount*) [ "${FAIL_AT:-}" = health ] && echo 1 || echo 0 ;;
      *State.Running*) echo true ;;
      *'{{.Image}}'*) cat "$S/image_id" ;;
      *ExitCode*) echo "ExitCode=0 OOMKilled=false Error=" ;;
    esac
    exit 0 ;;
  logs)
    printf 'relay v%s\nfingerprint %s\n' "$(cat "$S/running_version")" "$PIN"
    exit 0 ;;
  exec) cat "$S/running_version"; exit 0 ;;
  tag) echo "$2 $3" >> "$S/tags.log"; exit 0 ;;
  compose)
    case "$*" in
      *" build"*)
        [ "${FAIL_AT:-}" = build ] && { echo "build falhou de propósito" >&2; exit 1; }
        exit 0 ;;
      *" up -d"*)
        n=$(cat "$S/ups" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$S/ups"
        if [ "${FAIL_AT:-}" = up ] && [ "$n" = 1 ]; then echo "up falhou de propósito" >&2; exit 1; fi
        # the container now runs whatever the compose file and the image tag say
        if grep -q "crptochat-data" "$COMPOSE_FILE_UNDER_TEST" 2>/dev/null && [ ! -f "$S/restored" ]; then
          echo "9.9.0" > "$S/running_version"
        fi
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

SELFTEST_STUB = 'import os\nprint("ok: tls fingerprint " + os.environ.get("PIN", ""))\n'


def run(cmd, cwd=None, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, check=True, capture_output=True, text=True)  # nosec B603


def git(repo, *args):
    return run(["git", *args], cwd=repo).stdout.strip()


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

    commit("9.8.0")
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
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in rel.iterdir()}
    manifest = update.make_manifest("9.9.0", new_commit, int(time.time()), files)
    private, public = update.generate_key()
    (rel / "RELEASE").write_bytes(manifest)
    (rel / "RELEASE.sig").write_text(update.sign(manifest, private))

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
    compose.write_text("compose-ORIGINAL\n")
    state = tmp_path / "state" / "deploy.state"
    state.parent.mkdir()
    state.write_text(f"prev_tag=v9.8.0\ntls_pin={PIN}\n")

    stub = tmp_path / "stub"
    (stub / "bin").mkdir(parents=True)
    (stub / "volume").mkdir()
    (stub / "volume" / "registry.db").write_text("dados")
    (stub / "image_id").write_text(OLD_IMAGE + "\n")
    (stub / "running_version").write_text("9.8.0\n")
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
        "COMPOSE_FILE_UNDER_TEST": str(compose),
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
    }
    return dict(env=env, site=site, compose=compose, state=state, stub=stub, repo=repo, commit=new_commit)


def deploy(vm, fail_at=None):
    env = dict(vm["env"])
    if fail_at:
        env["FAIL_AT"] = fail_at
    # cwd: somewhere neutral, like the forced command's home on the VM (not a checkout)
    return subprocess.run([DASH, str(ROOT / "packaging" / "crptochat-deploy.sh")], env=env,  # nosec B603
                          capture_output=True, text=True, timeout=120, cwd=env["HOME"])


def tree(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


def assert_restored(vm, before):
    assert vm["compose"].read_text() == "compose-ORIGINAL\n"                   # compose
    assert tree(vm["site"]) == before                                         # site: index, downloads, web
    tags = (vm["stub"] / "tags.log").read_text().split("\n")
    assert f"{OLD_IMAGE} crptochat-server" in tags                             # image tag back to the old image
    assert vm["state"].read_text() == f"prev_tag=v9.8.0\ntls_pin={PIN}\n"     # state not advanced


@pytest.mark.parametrize("fail_at", ["up", "health"])
def test_a_step_failing_after_publishing_restores_compose_site_and_image(vm, fail_at):
    """`compose up` and the health check fail as plain commands (no `fail`): only
    set -e stops the deploy there. Everything published before must come back."""
    before = tree(vm["site"])
    r = deploy(vm, fail_at)
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "restaurando snapshot" in out and "estado anterior restaurado" in out
    assert "deploy v9.9.0 concluído" not in out
    assert_restored(vm, before)
    assert not list(vm["site"].rglob(".*.prev")) and not list(vm["site"].rglob(".*.new"))
    calls = (vm["stub"] / "calls.log").read_text()
    if fail_at == "up":
        # set -e held: nothing after the failing `compose up` ran (no health check)
        assert "RestartCount" not in calls


def test_a_failing_build_stops_before_anything_is_published(vm):
    before = tree(vm["site"])
    r = deploy(vm, "build")
    assert r.returncode != 0
    calls = (vm["stub"] / "calls.log").read_text()
    assert " up -d" in calls.split("build")[-1]          # only the restore's `compose up`, after the build
    assert calls.count(" up -d") == 1
    assert_restored(vm, before)


def test_a_good_release_is_deployed_and_published_readable(vm):
    r = deploy(vm)
    assert r.returncode == 0, r.stdout + r.stderr
    assert vm["state"].read_text().startswith("prev_tag=v9.9.0\n")
    assert git(vm["repo"], "rev-parse", "HEAD") == vm["commit"]
    assert "crptochat-data" in vm["compose"].read_text()                      # rendered from the release's template
    site = vm["site"]
    assert (site / "web" / "index.html").read_bytes() == b"web-NOVO"
    assert (site / "downloads" / "crptochat-linux-amd64").read_bytes() == b"\x7fELF binario novo"
    assert (site / "downloads" / "RELEASE.sig").is_file()
    for p in [site / "web", site / "downloads", *site.joinpath("web").rglob("*"), *site.joinpath("downloads").rglob("*")]:
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == (0o755 if p.is_dir() else 0o644), (p, oct(mode))
    assert not list(site.rglob(".*.prev")) and not list(site.parent.glob(".*.prev"))
    assert not (vm["stub"] / "tags.log").exists()                               # no rollback


def test_why_the_body_is_not_called_under_or():
    """In dash, a function called under || runs with set -e ignored even inside its own
    `( set -e ... )`: the old `deploy_body ... || rc=$?` would carry on after a failure."""
    script = 'set -eu; f(){ ( set -e; false; echo CONTINUOU ); }; rc=0; f || rc=$?; echo "rc=$rc"'
    r = subprocess.run([DASH, "-c", script], capture_output=True, text=True)  # nosec B603
    assert r.stdout.split() == ["CONTINUOU", "rc=0"]
    main = (ROOT / "packaging" / "crptochat-deploy.sh").read_text().split("main() {", 1)[1]
    assert 'set +e\n  deploy_body "$new_tag"' in main and "deploy_body" not in main.split("set +e", 1)[0]


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
  *NetworkSettings*) [ -f "$S/connected" ] && echo '{"crptochat":{}}' || echo '{"default":{}}'; exit 0 ;;
  *State.Running*) echo true; exit 0 ;;
  *Config.Image*) echo caddy:2.8.4; exit 0 ;;
  inspect*) exit 0 ;;
  logs*) echo "fingerprint $PIN"; exit 0 ;;
  "exec crptochat python"*) echo 9.8.0; exit 0 ;;
  "run --rm"*) exit 0 ;;
  "compose"*config*) python3 -c "
import json, sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
for s in d['services'].values():
    n = s.get('networks')
    s['networks'] = {k: None for k in n} if isinstance(n, list) else (n or {})
print(json.dumps(d))" "$(echo "$*" | sed 's/.* -f \([^ ]*\) .*/\1/')"; exit $? ;;
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


def bootstrap(b, fail_at=None):
    env = dict(b["env"])
    if fail_at:
        env["FAIL_AT"] = fail_at
    return subprocess.run([DASH, str(ROOT / "packaging" / "crptochat-bootstrap.sh")], env=env,  # nosec B603
                          capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("fail_at", ["validate", "reload"])
def test_bootstrap_failing_at_the_caddy_can_be_undone(boot, fail_at):
    caddyfile = boot["setup"] / "Caddyfile"
    inode = caddyfile.stat().st_ino
    r = bootstrap(boot, fail_at)
    out = r.stdout + r.stderr
    assert r.returncode != 0 and "para desfazer: sh" in out, out
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
    first = boot["setup"].joinpath("Caddyfile").read_text()
    boot["env"]["CRPTOCHAT_BACKUP_ROOT"] = str(boot["tmp"] / "bak2")
    r2 = bootstrap(boot)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert boot["setup"].joinpath("Caddyfile").read_text() == first
    assert (boot["stub"] / "reloads").read_text().strip() == "1"            # second run: nothing to reload

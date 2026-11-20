#!/bin/sh
# Entrada do comando forçado SSH na VM (tag vX.Y.Z em SSH_ORIGINAL_COMMAND). packaging/DEPLOY.md
#
# Ordem: tudo é baixado, verificado e montado em staging; o compose novo passa
# por `docker compose config` e por um `up --dry-run` que não pode tocar na rede
# nem no volume; só então o servidor é recriado, o health prova 7777 + PIN +
# WebGate, e o site (páginas, web, downloads) é publicado por rename atômico.
# Qualquer falha (ou INT/TERM/HUP) restaura por rename e PROVA que a 7777 voltou:
# rc 1 = deploy falhou e o estado anterior foi restaurado e verificado,
# rc 2 = idem, PIN TLS inesperado, rc 3 = RESTORE FALHOU (7777 possivelmente fora).
set -eu

DEPLOY_ENV=${CRPTOCHAT_DEPLOY_ENV:-/etc/crptochat/deploy.env}
if [ -f "$DEPLOY_ENV" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$DEPLOY_ENV"
  set +a
fi

REPO_DIR=${CRPTOCHAT_REPO_DIR:-/opt/crptochat}
COMPOSE_FILE=${CRPTOCHAT_COMPOSE_FILE:-/opt/crptochat-setup/docker-compose.isolated.yml}
COMPOSE_PROJECT=${CRPTOCHAT_COMPOSE_PROJECT:-crptochat-setup}
CONTAINER=${CRPTOCHAT_CONTAINER:-crptochat}
SERVICE=server
NETWORK=crptochat
IMAGE=${CRPTOCHAT_IMAGE:-crptochat-server}
DATA_VOLUME=${CRPTOCHAT_DATA_VOLUME:-crptochat-setup_crptochat-data}
CLIENT_REPO=${CRPTOCHAT_CLIENT_REPO:-hacking-table/crptochat-client}
SITE_ROOT=${CRPTOCHAT_SITE_ROOT:-/opt/crptochat-site}
WEB_TARBALL=crptochat-web-dist.tar.gz
LOG_DIR=${CRPTOCHAT_DEPLOY_LOG_DIR:-/var/log/crptochat}
STATE_FILE=${CRPTOCHAT_STATE_FILE:-/var/lib/crptochat/deploy.state}
BACKUP_DIR=${CRPTOCHAT_BACKUP_DIR:-/var/backups/crptochat}
LOCK_FILE=${CRPTOCHAT_DEPLOY_LOCK:-/var/lock/crptochat-deploy.lock}
SIGNING_PUBKEY=${CLIENT_SIGNING_PUBKEY:-UXnFSaezyEwMqfhfGncgjf03YiSN/FvaIja1Kv6g5VE=}
SMOKE_TIMEOUT=${CRPTOCHAT_SMOKE_TIMEOUT:-20}
HEALTH_WAIT=${CRPTOCHAT_HEALTH_WAIT:-20}
HEALTH_SECOND_WAIT=${CRPTOCHAT_HEALTH_SECOND_WAIT:-60}
PROBE_TRIES=${CRPTOCHAT_PROBE_TRIES:-10}

DEPLOY_TMP=
SNAP_DIR=
PREV_REGISTRY=
BODY_PID=
INTERRUPTED=0
PHASE=pre

umask 077

timestamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

# The log file (root, 600) has everything; stdout becomes the public Actions log,
# so it only gets one-line summaries (never container logs, never the whole PIN).
log_file() {
  mkdir -p "$LOG_DIR"
  chmod 700 "$LOG_DIR" 2>/dev/null || true
  logf="$LOG_DIR/deploy-$(date -u +%Y%m%d).log"
  printf '%s %s\n' "$(timestamp)" "$*" >> "$logf"
  chmod 600 "$logf" 2>/dev/null || true
}

log() {
  log_file "$*"
  printf '%s %s\n' "$(timestamp)" "$*" 2>/dev/null || true
}

# a command's whole output goes to the log file only
to_log() {
  mkdir -p "$LOG_DIR"
  logf="$LOG_DIR/deploy-$(date -u +%Y%m%d).log"
  "$@" >> "$logf" 2>&1
}

fail() {
  log_file "ERRO: $*"
  printf '%s ERRO: %s\n' "$(timestamp)" "$*" >&2 || true
  exit 1
}

short_pin() {
  printf '%s' "${1:-?}" | sed 's/^\(....\).\{1,\}\(....\)$/\1…\2/'
}

cleanup_tmp() {
  [ -n "${DEPLOY_TMP:-}" ] && rm -rf "$DEPLOY_TMP"
}
trap cleanup_tmp EXIT

kill_tree() {
  for child in $(pgrep -P "$1" 2>/dev/null); do kill_tree "$child"; done
  kill -TERM "$1" 2>/dev/null || true
}

# Before the snapshot: nothing changed, just leave. During the body: stop it (the
# whole process tree) and let main run the restore. During the restore and the
# final commit of the state: ignored (trap '' there).
on_signal() {
  if [ "$PHASE" = body ]; then
    INTERRUPTED=1
    [ -n "$BODY_PID" ] && kill_tree "$BODY_PID"
    return 0
  fi
  log "interrompido antes de mudar qualquer coisa"
  exit 130
}
trap on_signal INT TERM HUP

py_deploy() {
  PYTHONPATH="$REPO_DIR" CLIENT_SIGNING_PUBKEY="$SIGNING_PUBKEY" python3 "$REPO_DIR/packaging/deploy_lib.py" "$@"
}

# compose with a given file; never --remove-orphans: the Caddy (another compose
# file, same project) is an "orphan" of this project.
compose_with() {
  file=$1
  shift
  docker compose -p "$COMPOSE_PROJECT" -f "$file" --project-directory "$REPO_DIR" "$@"
}

compose() {
  compose_with "$COMPOSE_FILE" "$@"
}

validate_tag() {
  tag="${SSH_ORIGINAL_COMMAND:-}"
  case "$tag" in ''|*[!v0-9.]*) fail "tag recusada" ;; esac
  printf '%s\n' "$tag" | grep -Eqx 'v(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})' \
    || fail "tag recusada"
  printf '%s' "$tag"
}

tag_gt() {
  a=$(printf '%s' "$1" | sed 's/^v//' | tr '.' ' ')
  b=$(printf '%s' "$2" | sed 's/^v//' | tr '.' ' ')
  set -- $a
  a1=$1; a2=$2; a3=${3:-0}; a4=${4:-0}
  set -- $b
  b1=$1; b2=$2; b3=${3:-0}; b4=${4:-0}
  for pair in "$a1 $b1" "$a2 $b2" "$a3 $b3" "$a4 $b4"; do
    set -- $pair
    if [ "$1" -gt "$2" ]; then return 0; fi
    if [ "$1" -lt "$2" ]; then return 1; fi
  done
  return 1
}

valid_version() { printf '%s\n' "$1" | grep -Eqx '[0-9]+(\.[0-9]+){1,3}'; }
valid_pin() { printf '%s\n' "$1" | grep -Eqx '[0-9a-f]{64}'; }

# deploy.state is data, never code: each key is read with sed and validated.
state_get() {
  sed -n "s/^$1=//p" "$STATE_FILE" | head -n 1
}

read_state() {
  prev_tag=
  tls_pin=
  state_built=
  state_commit=
  [ -f "$STATE_FILE" ] || return 0
  prev_tag=$(state_get prev_tag)
  tls_pin=$(state_get tls_pin)
  state_built=$(state_get built)
  state_commit=$(state_get commit)
  case "$prev_tag" in
    '') ;;
    v*) valid_version "${prev_tag#v}" || fail "deploy.state: prev_tag inválido" ;;
    *) fail "deploy.state: prev_tag inválido" ;;
  esac
  [ -z "$tls_pin" ] || valid_pin "$tls_pin" || fail "deploy.state: tls_pin inválido"
  [ -z "$state_built" ] || printf '%s\n' "$state_built" | grep -Eqx '[0-9]{1,12}' || fail "deploy.state: built inválido"
  [ -z "$state_commit" ] || printf '%s\n' "$state_commit" | grep -Eqx '[0-9a-f]{4,40}' || fail "deploy.state: commit inválido"
}

write_state() {
  # tag pin built commit
  valid_version "${1#v}" || fail "write_state: tag inválida"
  valid_pin "$2" || fail "write_state: PIN inválido"
  umask 077
  mkdir -p "$(dirname "$STATE_FILE")"
  tmp="${STATE_FILE}.$$"
  {
    printf 'prev_tag=%s\n' "$1"
    printf 'tls_pin=%s\n' "$2"
    [ -z "${3:-}" ] || printf 'built=%s\n' "$3"
    [ -z "${4:-}" ] || printf 'commit=%s\n' "$4"
  } > "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$STATE_FILE"
}

ensure_volume() {
  if ! docker volume inspect "$DATA_VOLUME" >/dev/null 2>&1; then
    fail "volume ${DATA_VOLUME} não existe — abortando para não criar um volume novo e mudar o PIN"
  fi
}

# The shared network belongs to the bootstrap (the Caddy is on it); the server's
# compose only references it as external.
ensure_network() {
  if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
    fail "rede ${NETWORK} não existe — rode o crptochat-bootstrap.sh antes do deploy"
  fi
}

tls_pin_from_logs() {
  docker logs "$CONTAINER" 2>&1 | tr -d '\033' | grep -oE '[0-9a-f]{64}' | head -n 1
}

seed_state_if_missing() {
  if [ -f "$STATE_FILE" ] && [ -n "${prev_tag:-}" ]; then
    return 0
  fi
  running=$(docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)
  [ "$running" = true ] || fail "primeiro deploy: falta ${STATE_FILE} e o contêiner ${CONTAINER} não está rodando"
  pin=$(tls_pin_from_logs)
  valid_pin "$pin" || fail "primeiro deploy: PIN TLS não encontrado nos logs"
  version=$(docker exec "$CONTAINER" python -c "from crptochat import __version__; print(__version__)" 2>/dev/null) \
    || fail "primeiro deploy: não foi possível ler __version__ do contêiner"
  valid_version "$version" || fail "primeiro deploy: versão do contêiner inválida"
  log "inicializando deploy.state a partir do contêiner: tag=v${version} PIN $(short_pin "$pin")"
  write_state "v${version}" "$pin"
}

download_release_manifest() {
  tag=$1
  dest=$2
  mkdir -p "$dest"
  for name in RELEASE RELEASE.sig; do
    if ! curl -fsSL --max-time 120 "https://github.com/${CLIENT_REPO}/releases/download/${tag}/${name}" \
      -o "${dest}/${name}"; then
      fail "não foi possível baixar ${name} da release ${tag}"
    fi
  done
}

verify_release_precheck() {
  tag=$1
  manifest_dir=$2
  py_deploy verify-release --tag "$tag" \
    --manifest "${manifest_dir}/RELEASE" \
    --sig "${manifest_dir}/RELEASE.sig" \
    --repo "$REPO_DIR" \
    --pubkey "$SIGNING_PUBKEY" >"${manifest_dir}/verified.txt" 2>>"${manifest_dir}/verify.err" \
    || { to_log cat "${manifest_dir}/verify.err"; fail "verificação RELEASE/git falhou"; }
}

git_sync_commit() {
  commit=$1
  (
    umask 022
    cd "$REPO_DIR"
    git fetch --quiet origin main
    git -c advice.detachedHead=false checkout --quiet "$commit"
    git reset --quiet --hard "$commit"
  )
}

repo_version() {
  sed -n 's/^__version__ = "\([0-9][0-9.]*\)".*/\1/p' "$REPO_DIR/crptochat/__init__.py" | head -n 1
}

# What the site publishes must be readable by the web server: the downloads run
# with umask 077 (curl, tar and mkdir would leave 600/700). Set in staging, before
# the move into place, so nothing is ever published unreadable.
site_perms() {
  find "$1" -type d -exec chmod 755 {} +
  find "$1" -type f -exec chmod 644 {} +
}

# --------------------------------------------------------------- publishing
# dest is replaced by rename: src is copied next to it as .<name>.new (same
# filesystem), dest becomes .<name>.prev and .new becomes dest. The restore moves
# .prev back (rename again: no window where the path is missing or half copied).

publish_begin() {
  src=$1
  dest=$2
  parent=$(dirname "$dest")
  base=$(basename "$dest")
  new="${parent}/.${base}.new"
  prev="${parent}/.${base}.prev"
  rm -rf "$new"
  cp -a "$src" "$new"
  printf '%s\n' "$dest" >> "$PREV_REGISTRY"
  if [ -e "$dest" ]; then
    rm -rf "$prev"
    mv "$dest" "$prev"
  fi
  mv "$new" "$dest"
}

publish_file_begin() {
  src=$1
  dest=$2
  parent=$(dirname "$dest")
  base=$(basename "$dest")
  new="${parent}/.${base}.new"
  prev="${parent}/.${base}.prev"
  cp "$src" "$new"
  chmod 644 "$new"
  printf '%s\n' "$dest" >> "$PREV_REGISTRY"
  if [ -f "$dest" ]; then
    rm -f "$prev"
    mv "$dest" "$prev"
  fi
  mv "$new" "$dest"
}

publish_discard_prevs() {
  while IFS= read -r dest; do
    base=$(basename "$dest")
    parent=$(dirname "$dest")
    rm -rf "${parent}/.${base}.prev" 2>/dev/null || true
  done < "$PREV_REGISTRY"
}

# in reverse order of publishing; dest absent with no .prev (it did not exist
# before) is removed
publish_restore_prevs() {
  [ -s "$PREV_REGISTRY" ] || return 0
  sed -n '1!G;h;$p' "$PREV_REGISTRY" | while IFS= read -r dest; do
    base=$(basename "$dest")
    parent=$(dirname "$dest")
    prev="${parent}/.${base}.prev"
    if [ -e "$prev" ]; then
      rm -rf "$dest"
      mv "$prev" "$dest"
    fi
    rm -rf "${parent}/.${base}.new" 2>/dev/null || true
  done
}

# ----------------------------------------------------------------- snapshot
# Everything the restore needs, or the deploy does not start: image ID, compose,
# git HEAD of REPO_DIR, deploy.state. The site is not copied: it is restored by
# rename from the .prev copies.
snapshot_create() {
  SNAP_DIR="${DEPLOY_TMP}/snap"
  mkdir -p "$SNAP_DIR" || fail "snapshot: não foi possível criar ${SNAP_DIR}"
  PREV_REGISTRY="${SNAP_DIR}/prev_registry.txt"
  : > "$PREV_REGISTRY" || fail "snapshot: registro"
  if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    docker inspect --format '{{.Image}}' "$CONTAINER" > "$SNAP_DIR/image_id" \
      || fail "snapshot: imagem do contêiner"
    [ -s "$SNAP_DIR/image_id" ] || fail "snapshot: imagem do contêiner vazia"
  else
    : > "$SNAP_DIR/image_id"
  fi
  if [ -f "$COMPOSE_FILE" ]; then
    cp "$COMPOSE_FILE" "$SNAP_DIR/compose.yml" || fail "snapshot: compose"
  fi
  (cd "$REPO_DIR" && git rev-parse --verify HEAD) > "$SNAP_DIR/git_head" 2>/dev/null \
    || fail "snapshot: git HEAD de ${REPO_DIR}"
  grep -Eqx '[0-9a-f]{40}' "$SNAP_DIR/git_head" || fail "snapshot: git HEAD inválido"
  if [ -f "$STATE_FILE" ]; then
    cp "$STATE_FILE" "$SNAP_DIR/deploy.state" || fail "snapshot: deploy.state"
  fi
  log "snapshot do estado atual gravado"
}

# ----------------------------------------------------------------- staging
stage_web() {
  tag=$1
  manifest_dir=$2
  dl="${DEPLOY_TMP}/dl"
  mkdir -p "$dl" "${DEPLOY_TMP}/stage"
  curl -fsSL --max-time 600 "https://github.com/${CLIENT_REPO}/releases/download/${tag}/${WEB_TARBALL}" \
    -o "${dl}/${WEB_TARBALL}" || fail "falha ao baixar ${WEB_TARBALL}"
  py_deploy verify-assets --manifest "${manifest_dir}/RELEASE" --dir "$dl" --only "$WEB_TARBALL" \
    || fail "SHA-256 de ${WEB_TARBALL} inválido"
  mkdir -p "${DEPLOY_TMP}/web.x"
  tar -xzf "${dl}/${WEB_TARBALL}" -C "${DEPLOY_TMP}/web.x"
  [ -d "${DEPLOY_TMP}/web.x/dist" ] || fail "${WEB_TARBALL} sem dist/"
  mv "${DEPLOY_TMP}/web.x/dist" "${DEPLOY_TMP}/stage/web"
  site_perms "${DEPLOY_TMP}/stage/web"
}

stage_downloads() {
  tag=$1
  manifest_dir=$2
  dl="${DEPLOY_TMP}/dl"
  mkdir -p "$dl"
  # only names from the closed list (deploy_lib.DOWNLOAD_ASSET_NAMES)
  assets=$(py_deploy list-assets --manifest "${manifest_dir}/RELEASE" --except "$WEB_TARBALL" \
    --except RELEASE --except RELEASE.sig) || fail "RELEASE com arquivos desconhecidos"
  [ -n "$assets" ] || fail "RELEASE sem arquivos para downloads"
  for name in $assets; do
    curl -fsSL --max-time 600 "https://github.com/${CLIENT_REPO}/releases/download/${tag}/${name}" \
      -o "${dl}/${name}" || fail "falha ao baixar ${name}"
  done
  only=""
  for name in $assets; do only="$only --only $name"; done
  # shellcheck disable=SC2086
  py_deploy verify-assets --manifest "${manifest_dir}/RELEASE" --dir "$dl" $only \
    || fail "verificação de binários falhou"
  out="${DEPLOY_TMP}/stage/downloads"
  mkdir -p "$out"
  if [ -d "${SITE_ROOT}/downloads" ]; then
    cp -a "${SITE_ROOT}/downloads/." "$out/"
  fi
  for name in $assets; do cp "${dl}/${name}" "${out}/${name}"; done
  cp "${manifest_dir}/RELEASE" "${manifest_dir}/RELEASE.sig" "$out/"
}

stage_pages() {
  pin=$1
  manifest_dir=$2
  version=$(sed -n 's/^version=//p' "${manifest_dir}/verified.txt")
  root="${DEPLOY_TMP}/stage/index.html"
  dl="${DEPLOY_TMP}/stage/downloads/index.html"
  py_deploy root-index --downloads "$REPO_DIR/site/downloads/index.html" --out "$root"
  cp "$REPO_DIR/site/downloads/index.html" "$dl"
  to_log python3 "$REPO_DIR/packaging/stamp_site.py" --version "$version" --pin "$pin" "$root" "$dl"
  site_perms "${DEPLOY_TMP}/stage/downloads"
  chmod 644 "$root"
}

# rendered by structure (network and volume external), then read back by compose
# itself and checked: network/volume external with their real names, 7777, 7778
stage_compose() {
  staged="${DEPLOY_TMP}/stage/compose.yml"
  py_deploy render-compose --template "$REPO_DIR/docker-compose.isolated.yml" \
    --out "$staged" --volume "$DATA_VOLUME" --network "$NETWORK" || fail "render do compose falhou"
  compose_with "$staged" config --format json > "${DEPLOY_TMP}/compose.config.json" 2>>"${DEPLOY_TMP}/compose.err" \
    || { to_log cat "${DEPLOY_TMP}/compose.err"; fail "docker compose config rejeitou o compose renderizado"; }
  py_deploy check-server-config --json "${DEPLOY_TMP}/compose.config.json" --volume "$DATA_VOLUME" \
    --network "$NETWORK" > "${DEPLOY_TMP}/compose.check" || fail "compose renderizado não confere"
}

# compose's own plan, before anything is stopped: only the server container may
# change; any Create/Recreate/Remove of a network or a volume aborts here.
dry_run_guard() {
  file=$1
  out="${DEPLOY_TMP}/dryrun.$$.$(date +%s)"
  if ! compose_with "$file" up -d --no-build --dry-run > "$out" 2>&1; then
    to_log cat "$out"
    log "dry-run do compose falhou"
    return 1
  fi
  to_log cat "$out"
  if ! py_deploy check-dry-run --file "$out" --container "$CONTAINER" > "${out}.bad"; then
    log "dry-run: o compose mexeria em mais do que o contêiner ${CONTAINER}:"
    while IFS= read -r line; do log "  dry-run: ${line}"; done < "${out}.bad"
    return 1
  fi
  return 0
}

# ----------------------------------------------------------------- checks
capture_container_diagnostics() {
  [ -n "${1:-}" ] || return 0
  log "diagnóstico do contêiner ${1} gravado no log do deploy (${LOG_DIR})"
  log_file "docker logs --tail 200 ${1}:"
  to_log docker logs --tail 200 "$1" || true
  to_log docker inspect --format 'ExitCode={{.State.ExitCode}} OOMKilled={{.State.OOMKilled}} Error={{.State.Error}}' \
    "$1" || true
}

container_networks() {
  docker inspect --format '{{range $k,$_ := .NetworkSettings.Networks}}{{$k}} {{end}}' "$CONTAINER" 2>/dev/null
}

probe_fingerprint() {
  # from inside the repo: `python3 -m` puts the current directory first on sys.path
  i=0
  while [ "$i" -lt "$PROBE_TRIES" ]; do
    i=$((i + 1))
    probe_out=$(cd "$REPO_DIR" && PYTHONPATH="$REPO_DIR" python3 -m crptochat.selftest --probe 127.0.0.1:7777 2>&1) || true
    fp=$(printf '%s' "$probe_out" | sed -n 's/.*fingerprint \([0-9a-f]*\).*/\1/p' | head -n 1)
    if [ -n "$fp" ]; then
      printf '%s' "$fp"
      return 0
    fi
    [ "$i" -lt "$PROBE_TRIES" ] && sleep 2
  done
  log_file "probe TLS: ${probe_out:-sem saída}"
  return 1
}

# The server is really serving: running, on the shared network, 7777 published,
# and the TLS probe answers with the expected PIN. rc 2 = answers with another PIN.
verify_server() {
  pin=$1
  running=$(docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)
  if [ "$running" != true ]; then
    log "verificação: contêiner ${CONTAINER} não está rodando"
    return 1
  fi
  nets=" $(container_networks) "
  case "$nets" in
    *" ${NETWORK} "*) ;;
    *) log "verificação: ${CONTAINER} fora da rede ${NETWORK} (redes:${nets})"; return 1 ;;
  esac
  if [ -z "$(docker port "$CONTAINER" 7777/tcp 2>/dev/null)" ]; then
    log "verificação: porta 7777 não publicada"
    return 1
  fi
  if ! fp=$(probe_fingerprint); then
    log "verificação: probe TLS na 7777 sem resposta"
    return 1
  fi
  if [ "$fp" != "$pin" ]; then
    log "verificação: PIN TLS $(short_pin "$fp") != esperado $(short_pin "$pin")"
    return 2
  fi
  log "verificação: 7777 no ar, rede ${NETWORK}, PIN $(short_pin "$pin")"
  return 0
}

# WebGate: the log line of the behind-proxy mode, and a real WebSocket handshake
# from the shared network (where the Caddy reaches it).
check_webgate() {
  ws_port=$1
  ws_origin=$2
  if ! docker logs "$CONTAINER" 2>&1 | grep -qF "cliente web: ws atrás do proxy"; then
    log "health: WebGate não subiu (falta 'cliente web: ws atrás do proxy' no log)"
    return 1
  fi
  if ! to_log timeout 60 docker run --rm --network "$NETWORK" --cap-drop ALL \
      --security-opt no-new-privileges:true --entrypoint python "$IMAGE" -c '
import asyncio, json, sys
from websockets.asyncio.client import connect
async def main():
    async with connect(sys.argv[1], origin=sys.argv[2], open_timeout=10) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
        sys.exit(0 if hello.get("t") == "welcome" else 1)
asyncio.run(main())
' "ws://${CONTAINER}:${ws_port}" "$ws_origin"; then
    log "health: handshake WebSocket em ${CONTAINER}:${ws_port} pela rede ${NETWORK} falhou"
    return 1
  fi
  log "health: WebGate responde em ${CONTAINER}:${ws_port} pela rede ${NETWORK}"
}

restart_count() {
  docker inspect --format '{{ .RestartCount }}' "$CONTAINER" 2>/dev/null || echo 999
}

health_check() {
  version=$1
  pin=$2
  sleep "$HEALTH_WAIT"
  restarts=$(restart_count)
  log "health: RestartCount=${restarts}"
  [ "$restarts" = 0 ] || return 1
  set +e
  verify_server "$pin"
  vrc=$?
  set -e
  [ "$vrc" = 0 ] || return "$vrc"
  if ! docker logs "$CONTAINER" 2>&1 | grep -qF "relay v${version}"; then
    log "health: banner relay v${version} ausente nos logs"
    return 1
  fi
  ws_port=$(sed -n 's/^ws_port=//p' "${DEPLOY_TMP}/compose.check")
  ws_origin=$(sed -n 's/^ws_origin=//p' "${DEPLOY_TMP}/compose.check")
  if [ -n "$ws_port" ]; then
    check_webgate "$ws_port" "$ws_origin" || return 1
  fi
  # second sample: a server that dies after the first answer shows up here
  sleep "$HEALTH_SECOND_WAIT"
  restarts=$(restart_count)
  running=$(docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)
  log "health (2ª amostra, ${HEALTH_SECOND_WAIT}s): running=${running} RestartCount=${restarts}"
  [ "$running" = true ] && [ "$restarts" = 0 ]
}

smoke_test_image() {
  version=$1
  script="$REPO_DIR/packaging/server_smoke_restricted.sh"
  [ -x "$script" ] || fail "falta $script"
  log "smoke descartável da imagem ${IMAGE} (versão ${version})"
  to_log sh "$script" "$IMAGE" "$version" "$SMOKE_TIMEOUT"
  to_log env CRPTOCHAT_SMOKE_WS_BEHIND_PROXY=1 sh "$script" "$IMAGE" "$version" "$SMOKE_TIMEOUT"
}

backup_data() {
  tag=$1
  umask 077
  mkdir -p "$BACKUP_DIR"
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  arc="${BACKUP_DIR}/data-before-${tag}-${stamp}.tar.gz"
  vol=$(docker volume inspect "$DATA_VOLUME" -f '{{ .Mountpoint }}' 2>/dev/null || true)
  if [ -n "$vol" ] && [ -d "$vol" ]; then
    tar -C "$vol" -czf "$arc" .
    log "backup em $arc"
  else
    fail "volume ${DATA_VOLUME} sem mountpoint"
  fi
}

# ----------------------------------------------------------------- restore
# Proves it: the server is verified (running, network, 7777, PIN of deploy.state).
# If the plain `up` leaves it without network/port, the server is recreated from
# the snapshot's compose. A compose whose dry-run would touch the network or the
# volume is never run; then the staged compose (already checked: both external)
# recreates the server on the old image.
restore_server() {
  pin=$1
  if [ ! -f "${DEPLOY_TMP}/touched" ] && verify_server "$pin"; then
    log "servidor não foi tocado pelo deploy e segue no ar"
    return 0
  fi
  snap_compose="$SNAP_DIR/compose.yml"
  staged="${DEPLOY_TMP}/stage/compose.yml"
  if [ -f "$snap_compose" ] && dry_run_guard "$snap_compose"; then
    compose_with "$snap_compose" up -d --no-build || log "restore: compose up falhou"
    verify_server "$pin" && return 0
    log "restore: recriando ${SERVICE} com o compose do snapshot"
    compose_with "$snap_compose" up -d --no-build --force-recreate "$SERVICE" || log "restore: force-recreate falhou"
    verify_server "$pin" && return 0
  fi
  if [ -f "$staged" ] && [ -f "${DEPLOY_TMP}/compose.check" ] && dry_run_guard "$staged"; then
    log "restore: recriando ${SERVICE} na imagem anterior com o compose verificado (rede/volume external)"
    compose_with "$staged" up -d --no-build --force-recreate "$SERVICE" || log "restore: force-recreate falhou"
    verify_server "$pin" && return 0
  fi
  return 1
}

snapshot_restore() {
  pin=$1
  log "restaurando snapshot"
  publish_restore_prevs
  old_head=$(cat "$SNAP_DIR/git_head")
  (umask 022; cd "$REPO_DIR" && git checkout -q main && git reset -q --hard "$old_head") \
    || log "restore: git de ${REPO_DIR} não voltou para ${old_head}"
  if [ -s "$SNAP_DIR/image_id" ]; then
    img=$(cat "$SNAP_DIR/image_id")
    docker tag "$img" "$IMAGE" || log "restore: não foi possível retag da imagem $img"
  fi
  if [ -f "$SNAP_DIR/deploy.state" ]; then
    cp "$SNAP_DIR/deploy.state" "$STATE_FILE"
  fi
  restore_server "$pin"
}

# ----------------------------------------------------------------- body
deploy_body() {
  tag=$1
  commit=$2
  pin_before=$3
  manifest_dir=$4
  (
    set -e
    trap - INT TERM HUP
    backup_data "$tag"
    git_sync_commit "$commit"
    version=$(repo_version)
    [ "$version" = "$(sed -n 's/^version=//p' "${manifest_dir}/verified.txt")" ] \
      || fail "versão em __init__.py não confere com a RELEASE"
    # 1. everything downloaded, verified and assembled in staging
    stage_web "$tag" "$manifest_dir"
    stage_downloads "$tag" "$manifest_dir"
    stage_pages "$pin_before" "$manifest_dir"
    stage_compose
    staged="${DEPLOY_TMP}/stage/compose.yml"
    to_log compose_with "$staged" build --quiet
    smoke_test_image "$version"
    # 2. compose's plan, before anything is stopped
    dry_run_guard "$staged" || fail "dry-run: abortado sem tocar no servidor"
    # 3. the server
    publish_file_begin "$staged" "$COMPOSE_FILE"
    : > "${DEPLOY_TMP}/touched"
    compose up -d --no-build
    # 4. health (7777, PIN, WebGate, second sample)
    health_check "$version" "$pin_before"
    # 5. the site, by atomic rename
    publish_begin "${DEPLOY_TMP}/stage/web" "${SITE_ROOT}/web"
    publish_begin "${DEPLOY_TMP}/stage/downloads" "${SITE_ROOT}/downloads"
    publish_file_begin "${DEPLOY_TMP}/stage/index.html" "${SITE_ROOT}/index.html"
    log "site publicado em ${SITE_ROOT}"
  )
}

main() {
  py_deploy env-check >/dev/null || fail "deploy.env ou variáveis inválidas"

  new_tag=$(validate_tag)
  mkdir -p "$(dirname "$LOCK_FILE")"
  # shellcheck disable=SC3023
  exec 9>"$LOCK_FILE"
  flock -n 9 || fail "outro deploy em andamento"

  export REPO_DIR
  read_state
  seed_state_if_missing
  read_state
  old_tag=${prev_tag:-}
  pin_before=${tls_pin:-}
  valid_pin "$pin_before" || fail "PIN TLS desconhecido"

  log "deploy solicitado: ${new_tag} (anterior: ${old_tag:-nenhum})"
  ensure_volume
  ensure_network

  if [ -n "$old_tag" ] && tag_gt "$old_tag" "$new_tag"; then
    fail "recusa downgrade: ${new_tag} é mais antiga que ${old_tag}"
  fi

  DEPLOY_TMP=$(mktemp -d)
  manifest_dir="${DEPLOY_TMP}/manifest"
  download_release_manifest "$new_tag" "$manifest_dir"
  verify_release_precheck "$new_tag" "$manifest_dir"
  commit=$(sed -n 's/^commit=//p' "${manifest_dir}/verified.txt")
  built=$(sed -n 's/^built=//p' "${manifest_dir}/verified.txt")
  printf '%s\n' "$built" | grep -Eqx '[0-9]{1,12}' || fail "RELEASE sem built"
  if [ "$new_tag" = "$old_tag" ] && [ -n "${state_built:-}" ] && [ "$built" -lt "$state_built" ]; then
    fail "recusa RELEASE de ${new_tag} mais antiga (built ${built}) que a publicada (built ${state_built})"
  fi

  snapshot_create
  # The body runs in the background and main waits for it: a signal interrupts
  # the wait right away (the trap stops the body and the restore runs). It is not
  # called under ||, &&, if or !: in dash (the VM's /bin/sh) a function called in
  # such a context runs with set -e ignored all the way down.
  set +e
  PHASE=body
  deploy_body "$new_tag" "$commit" "$pin_before" "$manifest_dir" &
  BODY_PID=$!
  wait "$BODY_PID"
  rc=$?
  while kill -0 "$BODY_PID" 2>/dev/null; do wait "$BODY_PID"; rc=$?; done
  trap '' INT TERM HUP
  PHASE=restore
  [ "$INTERRUPTED" = 0 ] || { rc=130; log "deploy interrompido por sinal"; }
  set -e
  if [ "$rc" -eq 0 ]; then
    write_state "$new_tag" "$pin_before" "$built" "$commit"
    publish_discard_prevs
    log "deploy ${new_tag} concluído (PIN $(short_pin "$pin_before"))"
    exit 0
  fi
  # every step of the restore runs even if one of them fails
  set +e
  capture_container_diagnostics "$CONTAINER"
  snapshot_restore "$pin_before"
  restored=$?
  set -e
  if [ "$restored" -ne 0 ]; then
    log_file "ERRO: RESTORE FALHOU: 7777 possivelmente fora (deploy rc=${rc})"
    printf '%s ERRO: RESTORE FALHOU: 7777 possivelmente fora (deploy rc=%s)\n' "$(timestamp)" "$rc" >&2
    exit 3
  fi
  case "$rc" in
    2) log_file "ERRO: PIN TLS inesperado (estado anterior restaurado)"
       printf '%s ERRO: PIN TLS inesperado (estado anterior restaurado)\n' "$(timestamp)" >&2
       exit 2 ;;
    *) fail "deploy falhou (rc=${rc}); estado anterior restaurado" ;;
  esac
}

main

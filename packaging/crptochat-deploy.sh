#!/bin/sh
# Entrada do comando forçado SSH na VM de produção (uma tag vX.Y.Z em
# SSH_ORIGINAL_COMMAND). Instalação: packaging/DEPLOY.md
set -eu

DEPLOY_ENV=/etc/crptochat/deploy.env
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
IMAGE=${CRPTOCHAT_IMAGE:-crptochat-server}
DATA_VOLUME=${CRPTOCHAT_DATA_VOLUME:-crptochat-setup_crptochat-data}
CLIENT_REPO=${CRPTOCHAT_CLIENT_REPO:-hacking-table/crptochat-client}
SITE_ROOT=${CRPTOCHAT_SITE_ROOT:-/opt/crptochat-site}
LOG_DIR=${CRPTOCHAT_DEPLOY_LOG_DIR:-/var/log/crptochat}
STATE_FILE=${CRPTOCHAT_STATE_FILE:-/var/lib/crptochat/deploy.state}
BACKUP_DIR=${CRPTOCHAT_BACKUP_DIR:-/var/backups/crptochat}
LOCK_FILE=${CRPTOCHAT_DEPLOY_LOCK:-/var/lock/crptochat-deploy.lock}
SIGNING_PUBKEY=${CLIENT_SIGNING_PUBKEY:-UXnFSaezyEwMqfhfGncgjf03YiSN/FvaIja1Kv6g5VE=}
SMOKE_TIMEOUT=${CRPTOCHAT_SMOKE_TIMEOUT:-20}
HEALTH_WAIT=${CRPTOCHAT_HEALTH_WAIT:-20}

umask 077

timestamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

log() {
  mkdir -p "$LOG_DIR"
  logf="$LOG_DIR/deploy-$(date -u +%Y%m%d).log"
  umask 077
  printf '%s %s\n' "$(timestamp)" "$*" | tee -a "$logf"
}

fail() {
  log "ERRO: $*"
  exit 1
}

py_deploy() {
  PYTHONPATH="$REPO_DIR" CLIENT_SIGNING_PUBKEY="$SIGNING_PUBKEY" python3 "$REPO_DIR/packaging/deploy_lib.py" "$@"
}

compose() {
  docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" --project-directory "$REPO_DIR" "$@"
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
  a1=$1; a2=$2; a3=${3:-0}
  set -- $b
  b1=$1; b2=$2; b3=${3:-0}
  if [ "$a1" -gt "$b1" ]; then return 0; fi
  if [ "$a1" -lt "$b1" ]; then return 1; fi
  if [ "$a2" -gt "$b2" ]; then return 0; fi
  if [ "$a2" -lt "$b2" ]; then return 1; fi
  if [ "$a3" -gt "$b3" ]; then return 0; fi
  return 1
}

read_state() {
  prev_tag=
  tls_pin=
  if [ -f "$STATE_FILE" ]; then
    # shellcheck disable=SC1090
    . "$STATE_FILE" 2>/dev/null || true
  fi
}

write_state() {
  umask 077
  mkdir -p "$(dirname "$STATE_FILE")"
  tmp="${STATE_FILE}.$$"
  {
    printf 'prev_tag=%s\n' "$1"
    printf 'tls_pin=%s\n' "$2"
  } > "$tmp"
  mv -f "$tmp" "$STATE_FILE"
}

ensure_volume() {
  if ! docker volume inspect "$DATA_VOLUME" >/dev/null 2>&1; then
    fail "volume ${DATA_VOLUME} não existe — abortando para não criar um volume novo e mudar o PIN"
  fi
}

seed_state_if_missing() {
  if [ -f "$STATE_FILE" ] && [ -n "${prev_tag:-}" ]; then
    return 0
  fi
  running=$(docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)
  [ "$running" = true ] || fail "primeiro deploy: falta ${STATE_FILE} e o contêiner ${CONTAINER} não está rodando"
  pin=$(tls_pin_from_logs)
  [ -n "$pin" ] || fail "primeiro deploy: PIN TLS não encontrado nos logs"
  version=$(docker exec "$CONTAINER" python -c "from crptochat import __version__; print(__version__)" 2>/dev/null) \
    || fail "primeiro deploy: não foi possível ler __version__ do contêiner"
  prev_tag="v${version}"
  tls_pin=$pin
  log "inicializando deploy.state a partir do contêiner: tag=${prev_tag}"
  write_state "$prev_tag" "$pin"
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
    --pubkey "$SIGNING_PUBKEY" >"${manifest_dir}/verified.txt" \
    || fail "verificação RELEASE/git falhou"
  sed -n 's/^commit=//p' "${manifest_dir}/verified.txt"
}

git_sync_commit() {
  commit=$1
  (
    umask 022
    cd "$REPO_DIR"
    git fetch --quiet origin main
    git -c advice.detachedHead=false checkout --quiet "$commit"
    git reset --hard "$commit"
  )
}

repo_version() {
  sed -n 's/^__version__ = "\([0-9][0-9.]*\)".*/\1/p' "$REPO_DIR/crptochat/__init__.py" | head -n 1
}

sync_compose_file() {
  py_deploy render-compose \
    --template "$REPO_DIR/docker-compose.isolated.yml" \
    --out "$COMPOSE_FILE" \
    --volume "$DATA_VOLUME"
  log "compose em ${COMPOSE_FILE}"
}

build_web_dist() {
  (
    umask 022
    cd "$REPO_DIR"
    if [ ! -d web/node_modules/ws ]; then
      (cd web && npm ci)
    fi
    if [ ! -f web/vendor/manifest.json ]; then
      python3 web/fetch_vendor.py
    fi
    python3 web/build.py
  )
}

publish_dir() {
  src=$1
  dest=$2
  parent=$(dirname "$dest")
  base=$(basename "$dest")
  new="${parent}/.${base}.new.$$"
  prev="${parent}/.${base}.prev.$$"
  rm -rf "$new"
  cp -a "$src" "$new"
  if [ -e "$dest" ]; then
    rm -rf "$prev"
    mv "$dest" "$prev"
  fi
  mv "$new" "$dest"
  rm -rf "$prev" 2>/dev/null || true
}

sync_site_pages() {
  pin=$1
  manifest_dir=$2
  mkdir -p "${SITE_ROOT}/downloads"
  tmpi=$(mktemp)
  tmpd=$(mktemp)
  version=$(sed -n 's/^version=//p' "${manifest_dir}/verified.txt")
  cp "$REPO_DIR/site/index.html" "$tmpi"
  cp "$REPO_DIR/site/downloads/index.html" "$tmpd"
  python3 "$REPO_DIR/packaging/stamp_site.py" --version "$version" --pin "$pin" "$tmpi" "$tmpd"
  mv -f "$tmpi" "${SITE_ROOT}/index.html"
  mv -f "$tmpd" "${SITE_ROOT}/downloads/index.html"
  log "páginas HTML em ${SITE_ROOT}"
}

sync_web_client() {
  publish_dir "$REPO_DIR/web/dist" "${SITE_ROOT}/web"
  log "cliente web em ${SITE_ROOT}/web"
}

sync_release_downloads() {
  tag=$1
  manifest_dir=$2
  stage=$(mktemp -d)
  # shellcheck disable=SC2064
  trap "rm -rf '$stage'" RETURN
  cp "${manifest_dir}/RELEASE" "${manifest_dir}/RELEASE.sig" "$stage/"
  assets=$(PYTHONPATH="$REPO_DIR" python3 -c "
from pathlib import Path
from crptochat import update
info = update.parse_manifest(Path('${manifest_dir}/RELEASE').read_bytes())
print(' '.join(sorted(info['files'])))
")
  for name in $assets; do
    if ! curl -fsSL --max-time 600 "https://github.com/${CLIENT_REPO}/releases/download/${tag}/${name}" \
      -o "${stage}/${name}"; then
      fail "falha ao baixar ${name}"
    fi
  done
  PYTHONPATH="$REPO_DIR" python3 -c "
from pathlib import Path
import sys
sys.path.insert(0, '${REPO_DIR}/packaging')
from deploy_lib import verify_downloaded_assets
verify_downloaded_assets(Path('${manifest_dir}/RELEASE'), Path('${stage}'))
"
  mkdir -p "${SITE_ROOT}/downloads"
  for name in $assets RELEASE RELEASE.sig; do
    tmp="${SITE_ROOT}/downloads/.${name}.new.$$"
    cp "${stage}/${name}" "$tmp"
    mv -f "$tmp" "${SITE_ROOT}/downloads/${name}"
  done
  log "artefatos da release em ${SITE_ROOT}/downloads"
}

backup_data() {
  tag=$1
  umask 077
  mkdir -p "$BACKUP_DIR"
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  arc="${BACKUP_DIR}/data-before-${tag}-${stamp}.tar.gz"
  vol=$(docker volume inspect "$DATA_VOLUME" -f '{{ .Mountpoint }}' 2>/dev/null || true)
  if [ -n "$vol" ] && [ -d "$vol" ]; then
    tar -C "$vol" -czf "$arc" . 2>/dev/null || tar -czf "$arc" -C "$vol" .
    log "backup em $arc"
  else
    fail "volume ${DATA_VOLUME} sem mountpoint"
  fi
}

capture_container_diagnostics() {
  [ -n "${1:-}" ] || return 0
  log "diagnóstico do contêiner ${1}:"
  docker logs --tail 200 "$1" 2>&1 | while IFS= read -r line; do log "  logs: $line"; done || true
  docker inspect --format 'ExitCode={{.State.ExitCode}} OOMKilled={{.State.OOMKilled}} Error={{.State.Error}}' \
    "$1" 2>/dev/null | while IFS= read -r line; do log "  $line"; done || true
}

tls_pin_from_logs() {
  docker logs "$CONTAINER" 2>&1 | tr -d '\033' | grep -oE '[0-9a-f]{64}' | head -n 1
}

smoke_test_image() {
  version=$1
  script="$REPO_DIR/packaging/server_smoke_restricted.sh"
  [ -x "$script" ] || fail "falta $script"
  log "smoke descartável da imagem ${IMAGE} (versão ${version})"
  sh "$script" "$IMAGE" "$version" "$SMOKE_TIMEOUT" \
    || fail "smoke falhou; produção intacta"
  CRPTOCHAT_SMOKE_WS_BEHIND_PROXY=1 sh "$script" "$IMAGE" "$version" "$SMOKE_TIMEOUT" \
    || fail "smoke (atrás do proxy) falhou; produção intacta"
}

health_check() {
  sleep "$HEALTH_WAIT"
  restarts=$(docker inspect --format '{{ .RestartCount }}' "$CONTAINER" 2>/dev/null || echo 999)
  running=$(docker inspect --format '{{ .State.Running }}' "$CONTAINER" 2>/dev/null || echo false)
  log "health: running=${running} RestartCount=${restarts}"
  [ "$running" = true ] && [ "$restarts" = 0 ] || return 1
}

deploy_release() {
  tag=$1
  commit=$2
  pin_before=$3
  manifest_dir=$4

  backup_data "$tag"
  git_sync_commit "$commit"
  version=$(repo_version)
  [ -n "$version" ] || fail "versão em __init__.py não encontrada"

  sync_compose_file
  build_web_dist
  sync_web_client
  sync_site_pages "$pin_before" "$manifest_dir"
  sync_release_downloads "$tag" "$manifest_dir"

  compose build --quiet
  smoke_test_image "$version"

  if ! compose up -d; then
    capture_container_diagnostics "$CONTAINER"
    return 1
  fi
  if ! health_check; then
    capture_container_diagnostics "$CONTAINER"
    return 1
  fi
  new_pin=$(tls_pin_from_logs)
  if [ -n "$pin_before" ] && [ -n "$new_pin" ] && [ "$new_pin" != "$pin_before" ]; then
    log "PIN TLS mudou (${pin_before} -> ${new_pin})"
    capture_container_diagnostics "$CONTAINER"
    return 2
  fi
  write_state "$tag" "${new_pin:-$pin_before}"
  log "deploy ${tag} concluído"
  return 0
}

rollback_to() {
  tag=$1
  [ -n "$tag" ] || fail "rollback sem tag anterior"
  log "rollback para ${tag}"
  rdir=$(mktemp -d)
  # shellcheck disable=SC2064
  trap "rm -rf '$rdir'" RETURN
  download_release_manifest "$tag" "$rdir"
  commit=$(verify_release_precheck "$tag" "$rdir")
  pin_before=${tls_pin:-}
  [ -n "$pin_before" ] || pin_before=$(tls_pin_from_logs)
  case $(deploy_release "$tag" "$commit" "$pin_before" "$rdir"; echo $?) in
    0) ;;
    *) fail "rollback não ficou saudável" ;;
  esac
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
  expected_pin=${tls_pin:-}

  log "deploy solicitado: ${new_tag} (anterior: ${old_tag:-nenhum})"
  ensure_volume

  if [ -n "$old_tag" ] && tag_gt "$old_tag" "$new_tag"; then
    fail "recusa downgrade: ${new_tag} é mais antiga que ${old_tag}"
  fi

  manifest_dir=$(mktemp -d)
  download_release_manifest "$new_tag" "$manifest_dir"
  commit=$(verify_release_precheck "$new_tag" "$manifest_dir")

  pin_before=${expected_pin:-}
  if [ -z "$pin_before" ]; then
    pin_before=$(tls_pin_from_logs)
  fi

  rc=0
  deploy_release "$new_tag" "$commit" "$pin_before" "$manifest_dir" || rc=$?
  if [ "$rc" = 0 ]; then
    exit 0
  fi
  if [ -n "$old_tag" ] && [ "$old_tag" != "$new_tag" ]; then
    rollback_to "$old_tag"
  fi
  case "$rc" in
    1) fail "docker compose up ou health falhou" ;;
    2) fail "PIN TLS inesperado" ;;
    *) fail "deploy falhou (rc=${rc})" ;;
  esac
}

main
#!/bin/sh
# Entrada do comando forçado SSH na VM (tag vX.Y.Z em SSH_ORIGINAL_COMMAND). packaging/DEPLOY.md
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

DEPLOY_TMP=
SNAP_DIR=
PREV_REGISTRY=

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

cleanup_tmp() {
  [ -n "${DEPLOY_TMP:-}" ] && rm -rf "$DEPLOY_TMP"
}
trap cleanup_tmp EXIT INT TERM

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

# What the site publishes must be readable by the web server: the downloads run
# with umask 077 (curl, tar and mkdir would leave 600/700). Set before the move
# into place, so nothing is ever published unreadable.
site_perms() {
  find "$1" -type d -exec chmod 755 {} +
  find "$1" -type f -exec chmod 644 {} +
}

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
  printf '%s\n' "$dest" >> "$PREV_REGISTRY"
  if [ -f "$dest" ]; then
    rm -f "$prev"
    mv "$dest" "$prev"
  fi
  mv "$new" "$dest"
  chmod 644 "$dest"
}

publish_discard_prevs() {
  while IFS= read -r dest; do
    base=$(basename "$dest")
    parent=$(dirname "$dest")
    rm -rf "${parent}/.${base}.prev" 2>/dev/null || true
  done < "$PREV_REGISTRY"
}

publish_restore_prevs() {
  while IFS= read -r dest; do
    base=$(basename "$dest")
    parent=$(dirname "$dest")
    prev="${parent}/.${base}.prev"
    if [ -e "$prev" ] || [ -d "$prev" ]; then
      rm -rf "$dest"
      mv "$prev" "$dest"
    fi
    rm -rf "${parent}/.${base}.new" 2>/dev/null || true
  done < "$PREV_REGISTRY"
}

snapshot_create() {
  SNAP_DIR="${DEPLOY_TMP}/snap"
  mkdir -p "$SNAP_DIR/site"
  PREV_REGISTRY="${SNAP_DIR}/prev_registry.txt"
  : > "$PREV_REGISTRY"
  if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    docker inspect --format '{{.Image}}' "$CONTAINER" > "$SNAP_DIR/image_id"
  else
    : > "$SNAP_DIR/image_id"
  fi
  if [ -f "$COMPOSE_FILE" ]; then
    cp "$COMPOSE_FILE" "$SNAP_DIR/compose.yml"
  fi
  [ -d "${SITE_ROOT}/web" ] && cp -a "${SITE_ROOT}/web" "$SNAP_DIR/site/web" || true
  [ -d "${SITE_ROOT}/downloads" ] && cp -a "${SITE_ROOT}/downloads" "$SNAP_DIR/site/downloads" || true
  [ -f "${SITE_ROOT}/index.html" ] && cp "${SITE_ROOT}/index.html" "$SNAP_DIR/site/index.html" || true
  log "snapshot do estado atual gravado"
}

snapshot_restore() {
  log "restaurando snapshot (sem re-renderizar compose de commit antigo)"
  if [ -f "$SNAP_DIR/compose.yml" ]; then
    cp "$SNAP_DIR/compose.yml" "$COMPOSE_FILE"
  fi
  publish_restore_prevs
  if [ -f "$SNAP_DIR/image_id" ] && [ -s "$SNAP_DIR/image_id" ]; then
    img=$(cat "$SNAP_DIR/image_id")
    docker tag "$img" "$IMAGE" 2>/dev/null || log "aviso: não foi possível retag da imagem $img"
  fi
  if [ -d "$SNAP_DIR/site/web" ]; then
    rm -rf "${SITE_ROOT}/web"
    cp -a "$SNAP_DIR/site/web" "${SITE_ROOT}/web"
  fi
  if [ -d "$SNAP_DIR/site/downloads" ]; then
    rm -rf "${SITE_ROOT}/downloads"
    cp -a "$SNAP_DIR/site/downloads" "${SITE_ROOT}/downloads"
  fi
  if [ -f "$SNAP_DIR/site/index.html" ]; then
    cp "$SNAP_DIR/site/index.html" "${SITE_ROOT}/index.html"
    chmod 644 "${SITE_ROOT}/index.html"
  fi
  compose up -d || log "aviso: compose up no restore falhou"
}

sync_compose_file() {
  staged="${DEPLOY_TMP}/compose.rendered.yml"
  py_deploy render-compose \
    --template "$REPO_DIR/docker-compose.isolated.yml" \
    --out "$staged" \
    --volume "$DATA_VOLUME"
  publish_file_begin "$staged" "$COMPOSE_FILE"
  log "compose em ${COMPOSE_FILE}"
}

sync_site_pages() {
  pin=$1
  manifest_dir=$2
  version=$(sed -n 's/^version=//p' "${manifest_dir}/verified.txt")
  root="${DEPLOY_TMP}/index.root.html"
  dl="${DEPLOY_TMP}/index.dl.html"
  py_deploy root-index --downloads "$REPO_DIR/site/downloads/index.html" --out "$root"
  cp "$REPO_DIR/site/downloads/index.html" "$dl"
  python3 "$REPO_DIR/packaging/stamp_site.py" --version "$version" --pin "$pin" "$root" "$dl"
  chmod 644 "$root" "$dl"
  publish_file_begin "$root" "${SITE_ROOT}/index.html"
  publish_file_begin "$dl" "${SITE_ROOT}/downloads/index.html"
  log "páginas HTML em ${SITE_ROOT}"
}

sync_web_from_release() {
  tag=$1
  manifest_dir=$2
  stage=$(mktemp -d)
  if ! curl -fsSL --max-time 600 "https://github.com/${CLIENT_REPO}/releases/download/${tag}/${WEB_TARBALL}" \
    -o "${stage}/${WEB_TARBALL}"; then
    rm -rf "$stage"
    fail "falha ao baixar ${WEB_TARBALL}"
  fi
  py_deploy verify-assets --manifest "${manifest_dir}/RELEASE" --dir "$stage" --only "$WEB_TARBALL" \
    || { rm -rf "$stage"; fail "SHA-256 de ${WEB_TARBALL} inválido"; }
  mkdir -p "${stage}/web"
  tar -xzf "${stage}/${WEB_TARBALL}" -C "${stage}/web"
  [ -d "${stage}/web/dist" ] || { rm -rf "$stage"; fail "${WEB_TARBALL} sem dist/"; }
  site_perms "${stage}/web/dist"
  publish_begin "${stage}/web/dist" "${SITE_ROOT}/web"
  rm -rf "$stage"
  log "cliente web em ${SITE_ROOT}/web"
}

sync_release_downloads() {
  tag=$1
  manifest_dir=$2
  stage=$(mktemp -d)
  cp "${manifest_dir}/RELEASE" "${manifest_dir}/RELEASE.sig" "$stage/"
  assets=$(py_deploy list-assets --manifest "${manifest_dir}/RELEASE" --except "$WEB_TARBALL")
  [ -n "$assets" ] || { rm -rf "$stage"; fail "RELEASE sem arquivos para downloads"; }
  for name in $assets; do
    if ! curl -fsSL --max-time 600 "https://github.com/${CLIENT_REPO}/releases/download/${tag}/${name}" \
      -o "${stage}/${name}"; then
      rm -rf "$stage"
      fail "falha ao baixar ${name}"
    fi
  done
  only=""
  for name in $assets; do only="$only --only $name"; done
  # shellcheck disable=SC2086
  py_deploy verify-assets --manifest "${manifest_dir}/RELEASE" --dir "$stage" $only \
    || { rm -rf "$stage"; fail "verificação de binários falhou"; }
  mkdir -p "${DEPLOY_TMP}/downloads.merge"
  if [ -d "${SITE_ROOT}/downloads" ]; then
    cp -a "${SITE_ROOT}/downloads/." "${DEPLOY_TMP}/downloads.merge/"
  fi
  for name in $assets RELEASE RELEASE.sig; do
    cp "${stage}/${name}" "${DEPLOY_TMP}/downloads.merge/${name}"
  done
  site_perms "${DEPLOY_TMP}/downloads.merge"
  publish_begin "${DEPLOY_TMP}/downloads.merge" "${SITE_ROOT}/downloads"
  rm -rf "$stage"
  log "downloads em ${SITE_ROOT}/downloads"
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
  sh "$script" "$IMAGE" "$version" "$SMOKE_TIMEOUT"
  CRPTOCHAT_SMOKE_WS_BEHIND_PROXY=1 sh "$script" "$IMAGE" "$version" "$SMOKE_TIMEOUT"
}

health_check() {
  version=$1
  pin=$2
  sleep "$HEALTH_WAIT"
  restarts=$(docker inspect --format '{{ .RestartCount }}' "$CONTAINER" 2>/dev/null || echo 999)
  running=$(docker inspect --format '{{ .State.Running }}' "$CONTAINER" 2>/dev/null || echo false)
  log "health: running=${running} RestartCount=${restarts}"
  if [ "$running" != true ] || [ "$restarts" != 0 ]; then
    return 1
  fi
  if ! docker logs "$CONTAINER" 2>&1 | grep -qF "relay v${version}"; then
    log "health: banner relay v${version} ausente nos logs"
    return 1
  fi
  # from inside the repo: `python3 -m` puts the current directory first on sys.path
  probe_out=$(cd "$REPO_DIR" && PYTHONPATH="$REPO_DIR" python3 -m crptochat.selftest --probe 127.0.0.1:7777 2>&1) || true
  fp=$(printf '%s' "$probe_out" | sed -n 's/.*fingerprint //p' | head -n 1)
  if [ -z "$fp" ]; then
    log "health: fingerprint TLS não obtida"
    return 1
  fi
  if [ -n "$pin" ] && [ "$fp" != "$pin" ]; then
    log "health: fingerprint ${fp} != PIN ${pin}"
    return 1
  fi
  return 0
}

deploy_body() {
  tag=$1
  commit=$2
  pin_before=$3
  manifest_dir=$4
  (
    set -e
    backup_data "$tag"
    git_sync_commit "$commit"
    version=$(repo_version)
    [ -n "$version" ] || fail "versão em __init__.py não encontrada"
    compose build --quiet
    smoke_test_image "$version"
    sync_compose_file
    sync_web_from_release "$tag" "$manifest_dir"
    sync_site_pages "$pin_before" "$manifest_dir"
    sync_release_downloads "$tag" "$manifest_dir"
    compose up -d
    health_check "$version" "$pin_before"
    new_pin=$(tls_pin_from_logs)
    if [ -n "$pin_before" ] && [ -n "$new_pin" ] && [ "$new_pin" != "$pin_before" ]; then
      log "PIN TLS mudou (${pin_before} -> ${new_pin})"
      exit 2
    fi
    write_state "$tag" "${new_pin:-$pin_before}"
    log "deploy ${tag} concluído"
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
  expected_pin=${tls_pin:-}

  log "deploy solicitado: ${new_tag} (anterior: ${old_tag:-nenhum})"
  ensure_volume

  if [ -n "$old_tag" ] && tag_gt "$old_tag" "$new_tag"; then
    fail "recusa downgrade: ${new_tag} é mais antiga que ${old_tag}"
  fi

  DEPLOY_TMP=$(mktemp -d)
  manifest_dir="${DEPLOY_TMP}/manifest"
  download_release_manifest "$new_tag" "$manifest_dir"
  commit=$(verify_release_precheck "$new_tag" "$manifest_dir")

  pin_before=${expected_pin:-}
  if [ -z "$pin_before" ]; then
    pin_before=$(tls_pin_from_logs)
  fi
  [ -n "$pin_before" ] || fail "PIN TLS desconhecido"

  snapshot_create
  # The body runs as a plain command, never under ||, &&, if or !: in dash (the
  # VM's /bin/sh) a function called in such a context runs with set -e ignored
  # all the way down, even after its own `set -e`, so a failing step would go
  # unnoticed. set +e only around the call, to catch the status here.
  set +e
  deploy_body "$new_tag" "$commit" "$pin_before" "$manifest_dir"
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then
    publish_discard_prevs
    exit 0
  fi
  # best effort: every step of the restore runs even if one of them fails
  set +e
  capture_container_diagnostics "$CONTAINER"
  snapshot_restore
  set -e
  case "$rc" in
    2) fail "PIN TLS inesperado (estado anterior restaurado)" ;;
    *) fail "deploy falhou (rc=${rc}); estado anterior restaurado" ;;
  esac
}

main

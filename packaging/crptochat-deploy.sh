#!/bin/sh
# Entrada do comando forçado SSH na VM de produção (uma tag vX.Y.Z em
# SSH_ORIGINAL_COMMAND). Instalação: packaging/DEPLOY.md
set -eu

REPO_DIR=${CRPTOCHAT_REPO_DIR:-/opt/crptochat}
COMPOSE_FILE=${CRPTOCHAT_COMPOSE_FILE:-docker-compose.isolated.yml}
CONTAINER=${CRPTOCHAT_CONTAINER:-crptochat}
IMAGE=${CRPTOCHAT_IMAGE:-crptochat-server}
CLIENT_REPO=${CRPTOCHAT_CLIENT_REPO:-hacking-table/crptochat-client}
LOG_DIR=${CRPTOCHAT_DEPLOY_LOG_DIR:-/var/log/crptochat}
STATE_FILE=${CRPTOCHAT_STATE_FILE:-/var/lib/crptochat/deploy.state}
BACKUP_DIR=${CRPTOCHAT_BACKUP_DIR:-/var/backups/crptochat}
SIGNING_PUBKEY=${CLIENT_SIGNING_PUBKEY:-}
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

validate_tag() {
  tag="${SSH_ORIGINAL_COMMAND:-}"
  case "$tag" in ''|*[!v0-9.]*) fail "tag recusada" ;; esac
  printf '%s\n' "$tag" | grep -Eqx 'v(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})' \
    || fail "tag recusada"
  printf '%s' "$tag"
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

git_sync_tag() {
  tag=$1
  (
    umask 022
    cd "$REPO_DIR"
    git fetch --quiet --tags origin
    git -c advice.detachedHead=false checkout --quiet "refs/tags/$tag"
    git reset --hard "refs/tags/$tag"
  )
}

repo_version() {
  sed -n 's/^__version__ = "\([0-9][0-9.]*\)".*/\1/p' "$REPO_DIR/crptochat/__init__.py" | head -n 1
}

verify_release() {
  tag=$1
  if [ -z "$SIGNING_PUBKEY" ]; then
    log "aviso: CLIENT_SIGNING_PUBKEY vazio; pulando verificação RELEASE"
    return 0
  fi
  tmp=$(mktemp -d)
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" RETURN
  for name in RELEASE RELEASE.sig; do
    if ! curl -fsSL --max-time 60 "https://github.com/${CLIENT_REPO}/releases/download/${tag}/${name}" \
      -o "${tmp}/${name}"; then
      fail "não foi possível baixar ${name} da release ${tag}"
    fi
  done
  CLIENT_SIGNING_PUBKEY="$SIGNING_PUBKEY" PYTHONPATH="$REPO_DIR" \
    python3 -m crptochat.update verify "${tmp}/RELEASE" "${tmp}/RELEASE.sig" \
    || fail "assinatura RELEASE inválida para ${tag}"
  log "release ${tag} assinada conferida"
}

backup_data() {
  tag=$1
  umask 077
  mkdir -p "$BACKUP_DIR"
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  arc="${BACKUP_DIR}/data-before-${tag}-${stamp}.tar.gz"
  vol=$(docker volume inspect crptochat-data -f '{{ .Mountpoint }}' 2>/dev/null || true)
  if [ -n "$vol" ] && [ -d "$vol" ]; then
    tar -C "$vol" -czf "$arc" . 2>/dev/null || tar -czf "$arc" -C "$vol" .
    log "backup em $arc"
  else
    log "aviso: volume crptochat-data não encontrado; backup omitido"
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
}

health_check() {
  sleep "$HEALTH_WAIT"
  restarts=$(docker inspect --format '{{ .RestartCount }}' "$CONTAINER" 2>/dev/null || echo 999)
  running=$(docker inspect --format '{{ .State.Running }}' "$CONTAINER" 2>/dev/null || echo false)
  log "health: running=${running} RestartCount=${restarts}"
  [ "$running" = true ] && [ "$restarts" = 0 ] || return 1
}

rollback_to() {
  tag=$1
  log "rollback para ${tag}"
  git_sync_tag "$tag"
  docker compose -f "$REPO_DIR/$COMPOSE_FILE" build --quiet
  docker compose -f "$REPO_DIR/$COMPOSE_FILE" up -d
  health_check || fail "rollback não ficou saudável"
  pin=$(tls_pin_from_logs)
  [ -n "$pin" ] || fail "PIN TLS não encontrado após rollback"
  write_state "$tag" "$pin"
}

main() {
  new_tag=$(validate_tag)
  export REPO_DIR
  read_state
  old_tag=${prev_tag:-}
  expected_pin=${tls_pin:-}

  log "deploy solicitado: ${new_tag} (anterior: ${old_tag:-nenhum})"
  verify_release "$new_tag"
  backup_data "$new_tag"

  git_sync_tag "$new_tag"
  version=$(repo_version)
  [ -n "$version" ] || fail "versão em __init__.py não encontrada"

  docker compose -f "$REPO_DIR/$COMPOSE_FILE" build --quiet
  smoke_test_image "$version"

  if ! docker compose -f "$REPO_DIR/$COMPOSE_FILE" up -d; then
    capture_container_diagnostics "$CONTAINER"
    [ -n "$old_tag" ] && rollback_to "$old_tag"
    fail "docker compose up falhou"
  fi

  if ! health_check; then
    capture_container_diagnostics "$CONTAINER"
    [ -n "$old_tag" ] && rollback_to "$old_tag"
    fail "health check falhou após compose up"
  fi

  new_pin=$(tls_pin_from_logs)
  if [ -n "$expected_pin" ] && [ -n "$new_pin" ] && [ "$new_pin" != "$expected_pin" ]; then
    log "PIN TLS mudou (${expected_pin} -> ${new_pin}); revertendo"
    capture_container_diagnostics "$CONTAINER"
    rollback_to "$old_tag"
    fail "PIN TLS inesperado"
  fi

  write_state "$new_tag" "${new_pin:-$expected_pin}"
  log "deploy ${new_tag} concluído"
}

main
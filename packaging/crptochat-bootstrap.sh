#!/bin/sh
# Configuração única idempotente da VM de produção (deploy, rede Caddy, deploy.state).
# Uso: sudo sh packaging/crptochat-bootstrap.sh
set -eu

DEPLOY_ENV=/etc/crptochat/deploy.env
REPO_DIR=${CRPTOCHAT_REPO_DIR:-/opt/crptochat}
COMPOSE_PROJECT=${CRPTOCHAT_COMPOSE_PROJECT:-crptochat-setup}
CONTAINER=${CRPTOCHAT_CONTAINER:-crptochat}
CADDY_CONTAINER=${CRPTOCHAT_CADDY_CONTAINER:-caddy}
CADDYFILE=${CRPTOCHAT_CADDYFILE:-/etc/caddy/Caddyfile}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_ROOT="/root/bak-vision-${STAMP}"

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
fail() { log "ERRO: $*"; exit 1; }

[ "$(id -u)" = 0 ] || fail "rode como root"

mkdir -p "$BACKUP_ROOT"
DESFAZER="${BACKUP_ROOT}/DESFAZER.sh"
{
  echo "#!/bin/sh"
  echo "set -eu"
  echo "# Restaura arquivos salvos em ${BACKUP_ROOT}"
  [ -f /usr/local/sbin/crptochat-deploy ] && echo "cp '${BACKUP_ROOT}/crptochat-deploy' /usr/local/sbin/crptochat-deploy 2>/dev/null || true"
  [ -f "$DEPLOY_ENV" ] && echo "cp '${BACKUP_ROOT}/deploy.env' '$DEPLOY_ENV' 2>/dev/null || true"
  [ -f /var/lib/crptochat/deploy.state ] && echo "cp '${BACKUP_ROOT}/deploy.state' /var/lib/crptochat/deploy.state 2>/dev/null || true"
  echo "docker network disconnect crptochat ${CADDY_CONTAINER} 2>/dev/null || true"
  echo "cp '${BACKUP_ROOT}/Caddyfile' '${CADDYFILE}' 2>/dev/null || true"
  echo "docker exec ${CADDY_CONTAINER} caddy validate --config /etc/caddy/Caddyfile 2>/dev/null || true"
  echo "docker exec ${CADDY_CONTAINER} caddy reload --config /etc/caddy/Caddyfile 2>/dev/null || true"
} > "$DESFAZER"
chmod 755 "$DESFAZER"

[ -f /usr/local/sbin/crptochat-deploy ] && cp -a /usr/local/sbin/crptochat-deploy "${BACKUP_ROOT}/crptochat-deploy" || true
[ -f "$DEPLOY_ENV" ] && cp -a "$DEPLOY_ENV" "${BACKUP_ROOT}/deploy.env" || true
[ -f /var/lib/crptochat/deploy.state ] && cp -a /var/lib/crptochat/deploy.state "${BACKUP_ROOT}/deploy.state" || true
[ -f "$CADDYFILE" ] && cp -a "$CADDYFILE" "${BACKUP_ROOT}/Caddyfile" || true

install -m 755 "${REPO_DIR}/packaging/crptochat-deploy.sh" /usr/local/sbin/crptochat-deploy

umask 077
mkdir -p /etc/crptochat
if [ ! -f "$DEPLOY_ENV" ]; then
  cat > "$DEPLOY_ENV" <<'EOF'
CRPTOCHAT_REPO_DIR=/opt/crptochat
CRPTOCHAT_COMPOSE_FILE=/opt/crptochat-setup/docker-compose.isolated.yml
CRPTOCHAT_COMPOSE_PROJECT=crptochat-setup
CRPTOCHAT_DATA_VOLUME=crptochat-setup_crptochat-data
CRPTOCHAT_SITE_ROOT=/opt/crptochat-site
CLIENT_SIGNING_PUBKEY=UXnFSaezyEwMqfhfGncgjf03YiSN/FvaIja1Kv6g5VE=
EOF
  chmod 600 "$DEPLOY_ENV"
fi

if [ ! -f /var/lib/crptochat/deploy.state ]; then
  pin=$(docker logs "$CONTAINER" 2>&1 | tr -d '\033' | grep -oE '[0-9a-f]{64}' | head -n 1) \
    || fail "não foi possível ler o PIN TLS do contêiner ${CONTAINER}"
  version=$(docker exec "$CONTAINER" python -c "from crptochat import __version__; print(__version__)" 2>/dev/null) \
    || fail "não foi possível ler a versão do contêiner"
  umask 077
  mkdir -p /var/lib/crptochat
  {
    printf 'prev_tag=v%s\n' "$version"
    printf 'tls_pin=%s\n' "$pin"
  } > /var/lib/crptochat/deploy.state
  chmod 600 /var/lib/crptochat/deploy.state
  log "deploy.state inicial: v${version}"
fi

docker network inspect crptochat >/dev/null 2>&1 || docker network create crptochat
docker network connect crptochat "$CADDY_CONTAINER" 2>/dev/null || true

if [ -f "$CADDYFILE" ]; then
  tmp="${BACKUP_ROOT}/Caddyfile.new"
  if grep -q 'handle /ws' "$CADDYFILE" 2>/dev/null; then
    log "Caddyfile já tem handle /ws"
  else
    {
      cat "$CADDYFILE"
      printf '\n    handle /ws {\n        reverse_proxy crptochat:7778 {\n            header_up X-Real-IP {remote_host}\n        }\n    }\n'
    } > "$tmp"
    install -m 644 "$tmp" "$CADDYFILE"
  fi
  docker exec "$CADDY_CONTAINER" caddy validate --config /etc/caddy/Caddyfile \
    || fail "Caddyfile inválido após edição"
  docker exec "$CADDY_CONTAINER" caddy reload --config /etc/caddy/Caddyfile \
    || fail "caddy reload falhou"
else
  log "aviso: ${CADDYFILE} não encontrado — configure /ws manualmente"
fi

log "bootstrap concluído; backup em ${BACKUP_ROOT}"
log "para desfazer: sh ${DESFAZER}"
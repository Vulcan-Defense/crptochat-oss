#!/bin/sh
# Configuração única idempotente da VM. Exige /opt/crptochat no merge #30+.
# Uso: sudo sh /opt/crptochat/packaging/crptochat-bootstrap.sh
set -eu

DEPLOY_ENV=/etc/crptochat/deploy.env
STATE_FILE=/var/lib/crptochat/deploy.state
REPO_DIR=${CRPTOCHAT_REPO_DIR:-/opt/crptochat}
CADDY_CONTAINER=${CRPTOCHAT_CADDY_CONTAINER:-crptochat-caddy}
CADDYFILE=${CRPTOCHAT_CADDYFILE:-/opt/crptochat-setup/Caddyfile}
CADDY_COMPOSE=${CRPTOCHAT_CADDY_COMPOSE:-/opt/crptochat-setup/docker-compose.caddy.yml}
CONTAINER=${CRPTOCHAT_CONTAINER:-crptochat}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_ROOT="/root/bak-vision-${STAMP}"

HAD_ENV=0
HAD_STATE=0
CONNECTED_CADDY=0
MODIFIED_CADDY=0
MODIFIED_CADDY_COMPOSE=0

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
fail() { log "ERRO: $*"; exit 1; }

[ "$(id -u)" = 0 ] || fail "rode como root"
[ -f "${REPO_DIR}/packaging/deploy_lib.py" ] || fail "${REPO_DIR} não está no merge #30 (falta deploy_lib.py)"

mkdir -p "$BACKUP_ROOT"

[ -f "$DEPLOY_ENV" ] && HAD_ENV=1 && cp -a "$DEPLOY_ENV" "${BACKUP_ROOT}/deploy.env"
[ -f /usr/local/sbin/crptochat-deploy ] && cp -a /usr/local/sbin/crptochat-deploy "${BACKUP_ROOT}/crptochat-deploy"
[ -f "$STATE_FILE" ] && HAD_STATE=1 && cp -a "$STATE_FILE" "${BACKUP_ROOT}/deploy.state"
[ -f "$CADDYFILE" ] && cp -a "$CADDYFILE" "${BACKUP_ROOT}/Caddyfile"
[ -f "$CADDY_COMPOSE" ] && cp -a "$CADDY_COMPOSE" "${BACKUP_ROOT}/docker-compose.caddy.yml"

write_desfazer() {
  DESFAZER="${BACKUP_ROOT}/DESFAZER.sh"
  {
    echo "#!/bin/sh"
    echo "set -eu"
    echo "HAD_ENV=${HAD_ENV}"
    echo "HAD_STATE=${HAD_STATE}"
    echo "CONNECTED=${CONNECTED_CADDY}"
    echo "MOD_CADDY=${MODIFIED_CADDY}"
    echo "MOD_CADDY_COMPOSE=${MODIFIED_CADDY_COMPOSE}"
    echo '[ -f "'"${BACKUP_ROOT}/crptochat-deploy"'" ] && cp "'"${BACKUP_ROOT}/crptochat-deploy"'" /usr/local/sbin/crptochat-deploy'
    echo 'if [ "$HAD_ENV" = 0 ]; then rm -f "'"$DEPLOY_ENV"'"; else cp "'"${BACKUP_ROOT}/deploy.env"'" "'"$DEPLOY_ENV"'"; fi'
    echo 'if [ "$HAD_STATE" = 0 ]; then rm -f "'"$STATE_FILE"'"; else cp "'"${BACKUP_ROOT}/deploy.state"'" "'"$STATE_FILE"'"; fi'
    echo 'if [ "$MOD_CADDY" = 1 ] && [ -f "'"${BACKUP_ROOT}/Caddyfile"'" ]; then cp "'"${BACKUP_ROOT}/Caddyfile"'" "'"$CADDYFILE"'"; fi'
    echo 'if [ "$MOD_CADDY_COMPOSE" = 1 ] && [ -f "'"${BACKUP_ROOT}/docker-compose.caddy.yml"'" ]; then cp "'"${BACKUP_ROOT}/docker-compose.caddy.yml"'" "'"$CADDY_COMPOSE"'"; fi'
    echo 'if [ "$CONNECTED" = 1 ]; then docker network disconnect crptochat '"$CADDY_CONTAINER"'; fi'
    echo "docker exec $CADDY_CONTAINER caddy reload --config /etc/caddy/Caddyfile"
  } > "$DESFAZER"
  chmod 755 "$DESFAZER"
}

install -m 755 "${REPO_DIR}/packaging/crptochat-deploy.sh" /usr/local/sbin/crptochat-deploy

if [ ! -f "$DEPLOY_ENV" ]; then
  umask 077
  mkdir -p /etc/crptochat
  cat > "$DEPLOY_ENV" <<'EOF'
CRPTOCHAT_REPO_DIR=/opt/crptochat
CRPTOCHAT_COMPOSE_FILE=/opt/crptochat-setup/docker-compose.isolated.yml
CRPTOCHAT_COMPOSE_PROJECT=crptochat-setup
CRPTOCHAT_DATA_VOLUME=crptochat-setup_crptochat-data
CRPTOCHAT_SITE_ROOT=/opt/crptochat-site
CRPTOCHAT_CADDY_CONTAINER=crptochat-caddy
CRPTOCHAT_CADDYFILE=/opt/crptochat-setup/Caddyfile
CRPTOCHAT_CADDY_COMPOSE=/opt/crptochat-setup/docker-compose.caddy.yml
CLIENT_SIGNING_PUBKEY=UXnFSaezyEwMqfhfGncgjf03YiSN/FvaIja1Kv6g5VE=
EOF
  chmod 600 "$DEPLOY_ENV"
fi

if [ ! -f "$STATE_FILE" ]; then
  running=$(docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)
  [ "$running" = true ] || fail "contêiner ${CONTAINER} não está rodando"
  pin=$(docker logs "$CONTAINER" 2>&1 | tr -d '\033' | grep -oE '[0-9a-f]{64}' | head -n 1)
  [ -n "$pin" ] || fail "PIN TLS vazio nos logs de ${CONTAINER}"
  version=$(docker exec "$CONTAINER" python -c "from crptochat import __version__; print(__version__)" 2>/dev/null) \
    || fail "não foi possível ler a versão do contêiner"
  umask 077
  mkdir -p /var/lib/crptochat
  {
    printf 'prev_tag=v%s\n' "$version"
    printf 'tls_pin=%s\n' "$pin"
  } > "$STATE_FILE"
  chmod 600 "$STATE_FILE"
  log "deploy.state inicial: v${version}"
fi

docker network inspect crptochat >/dev/null 2>&1 || docker network create crptochat

if docker inspect "$CADDY_CONTAINER" >/dev/null 2>&1; then
  if docker inspect "$CADDY_CONTAINER" --format '{{json .NetworkSettings.Networks}}' | grep -q '"crptochat"'; then
    log "Caddy já na rede crptochat"
  else
    docker network connect crptochat "$CADDY_CONTAINER"
    CONNECTED_CADDY=1
  fi
else
  fail "contêiner ${CADDY_CONTAINER} não encontrado"
fi

if [ -f "$CADDY_COMPOSE" ]; then
  new_cc="${BACKUP_ROOT}/caddy-compose.new"
  PYTHONPATH="$REPO_DIR" python3 -c "
import sys
sys.path.insert(0, '${REPO_DIR}/packaging')
from pathlib import Path
from deploy_lib import ensure_caddy_compose_network
p = Path('${CADDY_COMPOSE}')
text = ensure_caddy_compose_network(p.read_text(encoding='utf-8'))
Path('${new_cc}').write_text(text, encoding='utf-8')
"
  if ! cmp -s "$CADDY_COMPOSE" "$new_cc"; then
    cp "$new_cc" "$CADDY_COMPOSE"
    MODIFIED_CADDY_COMPOSE=1
  fi
fi

if [ -f "$CADDYFILE" ]; then
  new_cf="${BACKUP_ROOT}/Caddyfile.new"
  PYTHONPATH="$REPO_DIR" python3 -c "
import sys
sys.path.insert(0, '${REPO_DIR}/packaging')
from pathlib import Path
from deploy_lib import insert_caddy_ws_handle
text = insert_caddy_ws_handle(Path('${CADDYFILE}').read_text(encoding='utf-8'))
Path('${new_cf}').write_text(text, encoding='utf-8')
"
  if ! cmp -s "$CADDYFILE" "$new_cf"; then
    MODIFIED_CADDY=1
    cimg=$(docker inspect --format '{{.Config.Image}}' "$CADDY_CONTAINER")
    docker run --rm -v "${new_cf}:/etc/caddy/Caddyfile:ro" "$cimg" caddy validate --config /etc/caddy/Caddyfile \
      || fail "Caddyfile proposto inválido"
    cat "$new_cf" >"$CADDYFILE"
    docker exec "$CADDY_CONTAINER" caddy validate --config /etc/caddy/Caddyfile \
      || fail "Caddyfile inválido no contêiner"
    docker exec "$CADDY_CONTAINER" caddy reload --config /etc/caddy/Caddyfile \
      || fail "caddy reload falhou"
  fi
else
  fail "Caddyfile ausente: ${CADDYFILE}"
fi

write_desfazer
log "bootstrap concluído; backup em ${BACKUP_ROOT}"
log "desfazer: sh ${DESFAZER}"
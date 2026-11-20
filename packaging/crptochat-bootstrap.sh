#!/bin/sh
# Configuração única idempotente da VM. Exige /opt/crptochat no merge #30+.
# Uso: sudo sh /opt/crptochat/packaging/crptochat-bootstrap.sh
#
# Tudo o que ela muda é salvo antes em BACKUP_ROOT, e o DESFAZER.sh de lá é
# gravado logo depois do backup e regravado ANTES de cada passo que muda algo
# (registro antes da ação): se ela parar no meio, inclusive no validate ou no
# reload do Caddy, `sh BACKUP_ROOT/DESFAZER.sh` desfaz exatamente o que já foi
# feito. Não troca o PIN nem tira a 7777 do ar: o contêiner do servidor não é
# recriado aqui, e o Caddy só recarrega a configuração (a antiga continua
# valendo se o reload falhar).
set -eu

DEPLOY_ENV=${CRPTOCHAT_DEPLOY_ENV:-/etc/crptochat/deploy.env}
STATE_FILE=${CRPTOCHAT_STATE_FILE:-/var/lib/crptochat/deploy.state}
DEPLOY_BIN=${CRPTOCHAT_DEPLOY_BIN:-/usr/local/sbin/crptochat-deploy}
REPO_DIR=${CRPTOCHAT_REPO_DIR:-/opt/crptochat}
CADDY_CONTAINER=${CRPTOCHAT_CADDY_CONTAINER:-crptochat-caddy}
CADDYFILE=${CRPTOCHAT_CADDYFILE:-/opt/crptochat-setup/Caddyfile}
CADDY_COMPOSE=${CRPTOCHAT_CADDY_COMPOSE:-/opt/crptochat-setup/docker-compose.caddy.yml}
CADDY_PROJECT=${CRPTOCHAT_CADDY_PROJECT:-crptochat-setup}
CONTAINER=${CRPTOCHAT_CONTAINER:-crptochat}
NETWORK=crptochat
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_ROOT=${CRPTOCHAT_BACKUP_ROOT:-/root/bak-vision-${STAMP}}
DESFAZER="${BACKUP_ROOT}/DESFAZER.sh"

HAD_ENV=0
HAD_STATE=0
HAD_BIN=0
CREATED_NET=0
CONNECTED_CADDY=0
MODIFIED_CADDY=0
MODIFIED_CADDY_COMPOSE=0

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
fail() {
  log "ERRO: $*"
  if [ -f "$DESFAZER" ]; then
    log "nada além do que está em ${BACKUP_ROOT} foi alterado; para desfazer: sh ${DESFAZER}"
  fi
  exit 1
}

py_lib() {
  PYTHONPATH="$REPO_DIR" python3 "${REPO_DIR}/packaging/deploy_lib.py" "$@"
}

[ "$(id -u)" = 0 ] || fail "rode como root"
[ -f "${REPO_DIR}/packaging/deploy_lib.py" ] || fail "${REPO_DIR} não está no merge #30 (falta deploy_lib.py)"

umask 077
mkdir -p "$BACKUP_ROOT"

# ---- backup of everything this script may touch
[ -f "$DEPLOY_ENV" ] && HAD_ENV=1 && cp -a "$DEPLOY_ENV" "${BACKUP_ROOT}/deploy.env"
[ -f "$DEPLOY_BIN" ] && HAD_BIN=1 && cp -a "$DEPLOY_BIN" "${BACKUP_ROOT}/crptochat-deploy"
[ -f "$STATE_FILE" ] && HAD_STATE=1 && cp -a "$STATE_FILE" "${BACKUP_ROOT}/deploy.state"
[ -f "$CADDYFILE" ] && cp -a "$CADDYFILE" "${BACKUP_ROOT}/Caddyfile"
[ -f "$CADDY_COMPOSE" ] && cp -a "$CADDY_COMPOSE" "${BACKUP_ROOT}/docker-compose.caddy.yml"

# DESFAZER.sh reverts what the flags say was (or was about to be) done. Every step
# is attempted even if another fails; the Caddyfile is written back with `cat >`
# (same inode: the Caddy container binds the file, not the directory).
write_desfazer() {
  tmp="${DESFAZER}.tmp"
  {
    echo "#!/bin/sh"
    echo "# Desfaz o bootstrap de ${STAMP}. Gerado por crptochat-bootstrap.sh."
    echo "set -u"
    echo "ok=0"
    echo "step() { \"\$@\" || { echo \"aviso: falhou: \$*\" >&2; ok=1; }; }"
    if [ "$HAD_BIN" = 1 ]; then
      echo "step cp '${BACKUP_ROOT}/crptochat-deploy' '${DEPLOY_BIN}'"
    else
      echo "step rm -f '${DEPLOY_BIN}'"
    fi
    if [ "$HAD_ENV" = 1 ]; then
      echo "step cp '${BACKUP_ROOT}/deploy.env' '${DEPLOY_ENV}'"
    else
      echo "step rm -f '${DEPLOY_ENV}'"
    fi
    if [ "$HAD_STATE" = 1 ]; then
      echo "step cp '${BACKUP_ROOT}/deploy.state' '${STATE_FILE}'"
    else
      echo "step rm -f '${STATE_FILE}'"
    fi
    if [ "$MODIFIED_CADDY_COMPOSE" = 1 ]; then
      echo "step cp '${BACKUP_ROOT}/docker-compose.caddy.yml' '${CADDY_COMPOSE}'"
    fi
    if [ "$MODIFIED_CADDY" = 1 ]; then
      echo "step sh -c \"cat '${BACKUP_ROOT}/Caddyfile' > '${CADDYFILE}'\""
      echo "step docker exec '${CADDY_CONTAINER}' caddy reload --config /etc/caddy/Caddyfile"
    fi
    if [ "$CONNECTED_CADDY" = 1 ]; then
      echo "step docker network disconnect '${NETWORK}' '${CADDY_CONTAINER}'"
    fi
    if [ "$CREATED_NET" = 1 ]; then
      echo "step docker network rm '${NETWORK}'"
    fi
    echo "[ \"\$ok\" = 0 ] && echo 'bootstrap desfeito' || echo 'bootstrap desfeito com avisos (acima)'"
    echo "exit \"\$ok\""
  } > "$tmp"
  chmod 700 "$tmp"
  mv -f "$tmp" "$DESFAZER"
}

write_desfazer      # right after the backup: nothing changed yet, nothing to undo but what is listed

# ---- the deploy entry point (forced command of the SSH key)
install -m 755 "${REPO_DIR}/packaging/crptochat-deploy.sh" "$DEPLOY_BIN"

# ---- deploy.env and the initial state
if [ ! -f "$DEPLOY_ENV" ]; then
  mkdir -p "$(dirname "$DEPLOY_ENV")"
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
  mkdir -p "$(dirname "$STATE_FILE")"
  {
    printf 'prev_tag=v%s\n' "$version"
    printf 'tls_pin=%s\n' "$pin"
  } > "$STATE_FILE"
  chmod 600 "$STATE_FILE"
  log "deploy.state inicial: v${version}"
fi

# ---- the network the Caddy uses to reach crptochat:7778 (wss)
docker inspect "$CADDY_CONTAINER" >/dev/null 2>&1 || fail "contêiner ${CADDY_CONTAINER} não encontrado"
if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
  CREATED_NET=1
  write_desfazer
  docker network create "$NETWORK" >/dev/null || fail "não foi possível criar a rede ${NETWORK}"
fi
if docker inspect "$CADDY_CONTAINER" --format '{{json .NetworkSettings.Networks}}' | grep -q "\"${NETWORK}\""; then
  log "Caddy já na rede ${NETWORK}"
else
  CONNECTED_CADDY=1
  write_desfazer
  docker network connect "$NETWORK" "$CADDY_CONTAINER" || fail "não foi possível ligar ${CADDY_CONTAINER} à rede ${NETWORK}"
fi

# ---- the Caddy compose file, so a future `docker compose up` keeps the Caddy on the network
if [ -f "$CADDY_COMPOSE" ]; then
  new_cc="${BACKUP_ROOT}/docker-compose.caddy.new.yml"
  py_lib caddy-compose --in "$CADDY_COMPOSE" --out "$new_cc" || fail "não foi possível editar ${CADDY_COMPOSE}"
  if ! cmp -s "$CADDY_COMPOSE" "$new_cc"; then
    # compose's own parser must read it as intended before it replaces the file
    docker compose -p "$CADDY_PROJECT" -f "$new_cc" --project-directory "$(dirname "$CADDY_COMPOSE")" \
      config --format json > "${BACKUP_ROOT}/caddy-compose.config.json" \
      || fail "docker compose config rejeitou o compose do Caddy proposto"
    py_lib check-compose-config --json "${BACKUP_ROOT}/caddy-compose.config.json" \
      || fail "compose do Caddy proposto não liga o caddy à rede ${NETWORK}"
    MODIFIED_CADDY_COMPOSE=1
    write_desfazer
    cat "$new_cc" > "$CADDY_COMPOSE"
    log "compose do Caddy: rede ${NETWORK} adicionada"
  fi
fi

# ---- the Caddyfile: /ws -> crptochat:7778
[ -f "$CADDYFILE" ] || fail "Caddyfile ausente: ${CADDYFILE}"
new_cf="${BACKUP_ROOT}/Caddyfile.new"
py_lib caddy-ws --in "$CADDYFILE" --out "$new_cf" || fail "não foi possível editar o Caddyfile"
if ! cmp -s "$CADDYFILE" "$new_cf"; then
  cimg=$(docker inspect --format '{{.Config.Image}}' "$CADDY_CONTAINER")
  # checked in a throwaway container before anything is touched
  docker run --rm -v "${new_cf}:/etc/caddy/Caddyfile:ro" "$cimg" caddy validate --config /etc/caddy/Caddyfile \
    || fail "Caddyfile proposto inválido (nada foi alterado no Caddy)"
  MODIFIED_CADDY=1
  write_desfazer
  cat "$new_cf" > "$CADDYFILE"            # same inode: the container binds this file
  docker exec "$CADDY_CONTAINER" caddy validate --config /etc/caddy/Caddyfile \
    || fail "Caddyfile inválido no contêiner (o Caddy segue com a configuração anterior)"
  docker exec "$CADDY_CONTAINER" caddy reload --config /etc/caddy/Caddyfile \
    || fail "caddy reload falhou (o Caddy segue com a configuração anterior)"
  log "Caddyfile: /ws encaminhado para crptochat:7778"
fi

write_desfazer
log "bootstrap concluído; backup em ${BACKUP_ROOT}"
log "desfazer: sh ${DESFAZER}"

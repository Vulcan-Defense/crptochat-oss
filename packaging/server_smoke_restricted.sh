#!/bin/sh
# Smoke do servidor com as mesmas restrições da produção (dados vazios, sem portas publicadas).
# Uso: server_smoke_restricted.sh IMAGEM VERSAO [TIMEOUT_SEG]
# Falha se "relay vVERSAO" não aparecer a tempo, o contêiner morrer, ou o WebSocket não responder.
# Com CRPTOCHAT_SMOKE_WS_BEHIND_PROXY=1 testa --ws-host 0.0.0.0 e --ws-behind-proxy (modo produção).
set -eu

image=${1:?imagem}
version=${2:?versão (sem v)}
timeout=${3:-20}
ws_port=${CRPTOCHAT_WS_PORT:-7778}
ws_origin=${CRPTOCHAT_WS_ORIGIN:-https://crptochat.acpt.app.br}
behind_proxy=${CRPTOCHAT_SMOKE_WS_BEHIND_PROXY:-0}
ws_host=127.0.0.1
ws_extra=""
if [ "$behind_proxy" = 1 ]; then
  ws_host=0.0.0.0
  ws_extra="--ws-behind-proxy --ws-trusted-proxy 127.0.0.0/8 --ws-trusted-proxy 172.16.0.0/12"
fi

name="crptochat-smoke-$$"

cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# /data vazio e gravável para o crpto (uid 999 no Dockerfile); tmpfs evita volume
# recém-criado pertencente ao root, que quebraria o sqlite antes do banner.
# shellcheck disable=SC2086
if ! docker run -d --name "$name" \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --memory 256m \
  --pids-limit 256 \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --tmpfs /data:rw,noexec,nosuid,size=32m,uid=999,gid=999,mode=700 \
  -u crpto \
  -e PYTHONUNBUFFERED=1 \
  --entrypoint python \
  "$image" \
  -u -m crptochat.server --data /data --ws-host "$ws_host" --ws-port "$ws_port" \
  --ws-origin "$ws_origin" $ws_extra >/dev/null; then
  echo "smoke: docker run falhou" >&2
  exit 1
fi

needle="relay v${version}"
i=0
limit=$((timeout * 2))
while [ "$i" -lt "$limit" ]; do
  if docker logs "$name" 2>&1 | grep -qF "$needle"; then
    break
  fi
  status=$(docker inspect --format '{{.State.Status}}' "$name" 2>/dev/null || echo gone)
  case "$status" in
    running) ;;
    *)
      echo "smoke: contêiner parou ($status)" >&2
      docker logs --tail 200 "$name" 2>&1 >&2 || true
      exit 1
      ;;
  esac
  i=$((i + 1))
  sleep 0.5
done
if [ "$i" -ge "$limit" ]; then
  echo "smoke: tempo esgotado (${timeout}s) sem ${needle}" >&2
  docker logs --tail 200 "$name" 2>&1 >&2 || true
  exit 1
fi

if docker logs "$name" 2>&1 | grep -q "cliente web: desativado"; then
  echo "smoke: WebGate não subiu" >&2
  docker logs --tail 200 "$name" 2>&1 >&2 || true
  exit 1
fi

ws_url=ws://127.0.0.1:${ws_port}
check_ip=""
if [ "$behind_proxy" = 1 ]; then
  ip=$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$name")
  if [ -z "$ip" ]; then
    echo "smoke: IP do contêiner vazio" >&2
    exit 1
  fi
  ws_url=ws://${ip}:${ws_port}
  check_ip="203.0.113.44"
fi

if ! docker exec -e SMOKE_WS_URL="$ws_url" -e SMOKE_ORIGIN="$ws_origin" -e SMOKE_CHECK_IP="$check_ip" "$name" python -c "
import asyncio, json, os, sys
from websockets.asyncio.client import connect
async def main():
    hdr = {'Origin': os.environ['SMOKE_ORIGIN']}
    if os.environ.get('SMOKE_CHECK_IP'):
        hdr['X-Real-IP'] = os.environ['SMOKE_CHECK_IP']
    async with connect(os.environ['SMOKE_WS_URL'], additional_headers=hdr, open_timeout=5) as ws:
        msg = await asyncio.wait_for(ws.recv(), timeout=5)
        if 'welcome' not in msg:
            sys.exit(1)
        if os.environ.get('SMOKE_CHECK_IP'):
            data = json.loads(msg)
            if data.get('your_ip') != os.environ['SMOKE_CHECK_IP']:
                sys.exit(2)
asyncio.run(main())
"; then
  echo "smoke: WebSocket na porta ${ws_port} não respondeu (origem ${ws_origin})" >&2
  docker logs --tail 200 "$name" 2>&1 >&2 || true
  exit 1
fi

echo "smoke: viu ${needle} e WebSocket ok"
exit 0
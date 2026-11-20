#!/bin/sh
# Smoke do servidor com as mesmas restrições da produção (dados vazios, sem portas publicadas).
# Uso: server_smoke_restricted.sh IMAGEM VERSAO [TIMEOUT_SEG]
# Falha se "relay vVERSAO" não aparecer a tempo, o contêiner morrer, ou o WebSocket não responder.
set -eu

image=${1:?imagem}
version=${2:?versão (sem v)}
timeout=${3:-20}
ws_port=${CRPTOCHAT_WS_PORT:-7778}
ws_origin=${CRPTOCHAT_WS_ORIGIN:-https://crptochat.acpt.app.br}

name="crptochat-smoke-$$"

cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# /data vazio e gravável para o crpto (uid 999 no Dockerfile); tmpfs evita volume
# recém-criado pertencente ao root, que quebraria o sqlite antes do banner.
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
  -u -m crptochat.server --data /data --ws-host 127.0.0.1 --ws-port "$ws_port" \
  --ws-origin "$ws_origin" >/dev/null; then
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

if ! docker exec "$name" python -c "
import asyncio, sys
from websockets.asyncio.client import connect
ORIGIN = '''$ws_origin'''
PORT = $ws_port
async def main():
    async with connect(
        f'ws://127.0.0.1:{PORT}',
        additional_headers={'Origin': ORIGIN},
        open_timeout=5,
    ) as ws:
        msg = await asyncio.wait_for(ws.recv(), timeout=5)
        if 'welcome' not in msg:
            sys.exit(1)
asyncio.run(main())
"; then
  echo "smoke: WebSocket na porta ${ws_port} não respondeu (origem ${ws_origin})" >&2
  docker logs --tail 200 "$name" 2>&1 >&2 || true
  exit 1
fi

echo "smoke: viu ${needle} e WebSocket ok"
exit 0
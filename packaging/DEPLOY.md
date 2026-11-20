# Script de deploy na VM (`crptochat-deploy`)

O GitHub Actions manda só a tag por SSH. Na VM o `authorized_keys` fixa o
comando a este script (caminho usual: `/usr/local/sbin/crptochat-deploy`).

## Instalação manual

1. Copie `packaging/crptochat-deploy.sh` para `/usr/local/sbin/crptochat-deploy` e torne executável (`chmod 755`).
2. Garanta `packaging/server_smoke_restricted.sh` no repositório em `/opt/crptochat` (o deploy chama esse caminho).
3. Use `docker-compose.isolated.yml` na raiz do clone (`/opt/crptochat` por padrão).
4. Variáveis de ambiente recomendadas (ex.: `/etc/crptochat/deploy.env` e `set -a; . …; set +a` no wrapper):

   | Variável | Significado |
   | --- | --- |
   | `CLIENT_SIGNING_PUBKEY` | Ed25519 base64 (mesma do `release-client`) para validar `RELEASE` |
   | `CRPTOCHAT_REPO_DIR` | Clone git (padrão `/opt/crptochat`) |
   | `CRPTOCHAT_COMPOSE_FILE` | Compose endurecido (padrão `docker-compose.isolated.yml`) |

5. Diretórios com `umask 077`: `CRPTOCHAT_DEPLOY_LOG_DIR`, `CRPTOCHAT_BACKUP_DIR`, `CRPTOCHAT_STATE_FILE`.

## Comportamento relevante (correção de permissões)

- O script corre com `umask 077`, mas **git fetch/checkout/reset** rodam num subshell com `umask 022`, para o tree não ficar `0600` e nem quebrar o `COPY` do Docker.
- Antes de `docker compose up -d`, roda o smoke de `server_smoke_restricted.sh` (mesmas restrições da produção).
- Se o contêiner de produção falhar, grava `docker logs --tail 200` e `docker inspect` no log de deploy **antes** do rollback.
- Rollback também usa `git_sync_tag` com `umask 022`.

## `authorized_keys` (exemplo)

```
command="/usr/local/sbin/crptochat-deploy",restrict ssh-ed25519 AAAA… deploy-github
```

## Cliente web (`crptochat.acpt.app.br`)

O `docker-compose.isolated.yml` do repositório liga o **WebGate** com:

```yaml
--ws-host 0.0.0.0 --ws-port 7778 --ws-origin https://crptochat.acpt.app.br
```

e publica **só em loopback**:

- `127.0.0.1:7777` — relay TLS (cliente de terminal)
- `127.0.0.1:7778` — WebSocket sem TLS (o proxy faz `wss://`)

### Build estático (`web/dist`)

Na VM (ou em CI e rsync):

```sh
cd /opt/crptochat
python3 web/fetch_vendor.py
(cd web && npm ci)
python3 web/build.py
# resultado: web/dist/
```

Sirva `web/dist/` com **Caddy** (ou nginx) no host, no mesmo host HTTPS da página. Cabeçalhos obrigatórios (ver `web/README.md`):

```
Content-Security-Policy: frame-ancestors 'none'
X-Frame-Options: DENY
```

(arquivos em `b/<hash>/` podem ter `Cache-Control: immutable`; `index.html` e `sw.js` não.)

### Caddy (esboço)

```caddy
crptochat.acpt.app.br {
    root * /var/www/crptochat-web
    encode gzip zstd
    header / X-Frame-Options "DENY"
    header / Content-Security-Policy "frame-ancestors 'none'"
    @immutable path /b/*
    header @immutable Cache-Control "public, max-age=31536000, immutable"
    file_server

    # WebSocket → contêiner (origem conferida no servidor com --ws-origin)
    handle /ws {
        reverse_proxy 127.0.0.1:7778
    }
}
```

Na página de login, o campo **servidor** deve apontar para o `wss://` público, por exemplo `wss://crptochat.acpt.app.br/ws` (se o Caddy expõe `/ws`) ou outro path que você configure — o importante é bater com o proxy e a origem `https://crptochat.acpt.app.br`.

O cliente de **terminal** continua usando `crptochat.acpt.app.br:7777` com `--pin` do certificado em `/data`.
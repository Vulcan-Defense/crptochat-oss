# Script de deploy na VM (`crptochat-deploy`)

O GitHub Actions manda só a tag por SSH. Na VM o `authorized_keys` fixa o
comando a este script (caminho usual: `/usr/local/sbin/crptochat-deploy`).

## Instalação manual

1. Copie `packaging/crptochat-deploy.sh` para `/usr/local/sbin/crptochat-deploy` e torne executável (`chmod 755`).
2. Garanta `packaging/server_smoke_restricted.sh` no repositório em `/opt/crptochat` (o deploy chama esse caminho).
3. O compose de produção fica em `/opt/crptochat-setup/docker-compose.isolated.yml` (padrão); o clone do código em `/opt/crptochat`.
4. Variáveis de ambiente recomendadas (ex.: `/etc/crptochat/deploy.env` e `set -a; . …; set +a` no wrapper):

   | Variável | Significado |
   | --- | --- |
   | `CLIENT_SIGNING_PUBKEY` | Ed25519 base64 (padrão: chave do `release-client`; fingerprint `SHA256 aae1…3890`) |
   | `CRPTOCHAT_REPO_DIR` | Clone git (padrão `/opt/crptochat`) |
   | `CRPTOCHAT_COMPOSE_FILE` | Compose endurecido (padrão `/opt/crptochat-setup/docker-compose.isolated.yml`) |
   | `CRPTOCHAT_COMPOSE_PROJECT` | Nome do projeto compose na VM (padrão: o do setup existente) |
   | `CRPTOCHAT_DATA_VOLUME` | Volume Docker dos dados (padrão `crptochat-data`) |

5. Diretórios com `umask 077`: `CRPTOCHAT_DEPLOY_LOG_DIR`, `CRPTOCHAT_BACKUP_DIR`, `CRPTOCHAT_STATE_FILE`.

## Comportamento relevante

- `flock` em `/var/lock/crptochat-deploy.lock` — um deploy por vez.
- Recusa downgrade de tag, exige tag alcançável de `origin/main` e assinatura `RELEASE` com a chave fixa.
- Aborta se o volume `crptochat-data` não existir; após o `up`, reverte se o PIN TLS mudar.
- O script corre com `umask 077`, mas **git fetch/checkout/reset** rodam num subshell com `umask 022`.
- Antes de `docker compose up -d`, roda o smoke de `server_smoke_restricted.sh` (loopback e modo `0.0.0.0` + `--ws-behind-proxy`).
- Se o contêiner de produção falhar, grava `docker logs --tail 200` e `docker inspect` no log de deploy **antes** do rollback.

## `authorized_keys` (exemplo)

```
command="/usr/local/sbin/crptochat-deploy",restrict ssh-ed25519 AAAA… deploy-github
```

## Cliente web (`crptochat.acpt.app.br`)

O template `docker-compose.isolated.yml` do repositório liga o **WebGate** com:

```yaml
--ws-host 0.0.0.0 --ws-port 7778 --ws-behind-proxy --ws-trusted-proxy 172.16.0.0/12
--ws-origin https://crptochat.acpt.app.br
```

- `7777:7777` no host — relay TLS público para o cliente de terminal.
- **7778 não é publicada no host** — só na rede Docker `crptochat`.
- O Caddy (contêiner) entra na mesma rede e faz `reverse_proxy crptochat:7778`.

### Build estático (`web/dist`)

```sh
cd /opt/crptochat
python3 web/fetch_vendor.py
(cd web && npm ci)
python3 web/build.py
# resultado: web/dist/
```

Sirva `web/dist/` em **`/web/`** no mesmo host HTTPS (a raiz `/` continua a página de downloads).
Cabeçalhos obrigatórios (ver `web/README.md`):

```
Content-Security-Policy: frame-ancestors 'none'
X-Frame-Options: DENY
```

### Caddy (esboço, contêiner na rede `crptochat`)

```caddy
crptochat.acpt.app.br {
    encode gzip zstd
    header / X-Frame-Options "DENY"
    header / Content-Security-Policy "frame-ancestors 'none'"

    # página de downloads na raiz (fora deste arquivo ou file_server separado)
    handle /web/* {
        root * /var/www/crptochat-web
        @immutable path /web/b/*
        header @immutable Cache-Control "public, max-age=31536000, immutable"
        uri strip_prefix /web
        file_server
    }

    handle /ws {
        reverse_proxy crptochat:7778 {
            header_up X-Real-IP {remote_host}
        }
    }
}
```

No `docker-compose` do Caddy, declare `networks: [crptochat]` com `external: true` e nome `crptochat`.

O cliente web usa por padrão `wss://crptochat.acpt.app.br/ws` quando a página está em HTTPS.

O cliente de **terminal** continua usando `crptochat.acpt.app.br:7777` com `--pin` do certificado em `/data`.
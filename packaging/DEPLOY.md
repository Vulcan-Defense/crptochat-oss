# Script de deploy na VM (`crptochat-deploy`)

O GitHub Actions manda só a tag `vX.Y.Z` por SSH (`SSH_ORIGINAL_COMMAND`). Na VM
o `authorized_keys` fixa o comando a `/usr/local/sbin/crptochat-deploy`.

**Bootstrap inicial (uma vez):**

```sh
sudo sh /opt/crptochat/packaging/crptochat-bootstrap.sh
```

## Configuração

1. `/etc/crptochat/deploy.env` (opcional; carregado com `set -a` pelo deploy).
2. Defaults de produção embutidos:

   | Variável | Padrão |
   | --- | --- |
   | `CRPTOCHAT_REPO_DIR` | `/opt/crptochat` |
   | `CRPTOCHAT_COMPOSE_FILE` | `/opt/crptochat-setup/docker-compose.isolated.yml` |
   | `CRPTOCHAT_COMPOSE_PROJECT` | `crptochat-setup` (sempre `docker compose -p …`) |
   | `CRPTOCHAT_DATA_VOLUME` | `crptochat-setup_crptochat-data` |
   | `CRPTOCHAT_SITE_ROOT` | `/opt/crptochat-site` |
   | `CLIENT_SIGNING_PUBKEY` | chave com fingerprint `SHA256 aae1…3890` |

## O que cada deploy faz sozinho

1. Baixa `RELEASE` + `RELEASE.sig` do **crptochat-client** (não usa tags no crptochat-oss).
2. Confere assinatura, versão = tag pedida, `commit` do RELEASE ancestral de `origin/main`
   **antes** de qualquer `git checkout`.
3. Faz checkout do **commit** (não `refs/tags/v…`) e confere `__version__` no tree.
4. Sincroniza compose (volume externo), build `web/dist`, site (`index.html` + `downloads/`),
   binários assinados do RELEASE, smoke, `compose up`, PIN inalterado ou rollback.

Sem `/var/lib/crptochat/deploy.state`: deriva tag e PIN do contêiner em execução ou aborta
**antes** de mudar qualquer coisa.

## `authorized_keys`

```
command="/usr/local/sbin/crptochat-deploy",restrict ssh-ed25519 AAAA… deploy-github
```

## Rede Caddy / WebSocket

Ver seção anterior em `packaging/DEPLOY.md` (rede `crptochat`, `/ws` → `crptochat:7778`,
estático em `/web/`). O bootstrap conecta o contêiner Caddy à rede e acrescenta `handle /ws`
reescrevendo o Caddyfile inteiro (sem `sed -i`), com `caddy validate` antes do reload.
# Script de deploy na VM (`crptochat-deploy`)

O GitHub Actions manda só a tag `vX.Y.Z` por SSH (`SSH_ORIGINAL_COMMAND`). Na VM
o `authorized_keys` fixa o comando a `/usr/local/sbin/crptochat-deploy`.

**Pré-requisito:** `/opt/crptochat` deve estar no merge do **PR #30** (ou posterior)
com `packaging/deploy_lib.py` e este `crptochat-deploy.sh`.

**Bootstrap inicial (uma vez):**

```sh
cd /opt/crptochat && git fetch origin && git checkout main
sudo sh packaging/crptochat-bootstrap.sh
```

Antes de mudar qualquer coisa, o bootstrap copia tudo o que pode tocar para
`/root/bak-vision-<data>/` e grava ali o `DESFAZER.sh`. Esse script é **regravado
antes de cada passo** que muda algo: instalar o comando, `deploy.env`,
`deploy.state`, criar ou ligar a rede `crptochat`, o compose do Caddy e o
Caddyfile. Se ele falhar no meio, inclusive no `caddy validate` ou no `caddy reload`,
a mensagem de erro mostra o comando para desfazer exatamente o que já foi feito:

```sh
sudo sh /root/bak-vision-<data>/DESFAZER.sh
```

O Caddyfile é regravado com `cat >`, no mesmo inode, porque o contêiner
`crptochat-caddy` monta o arquivo por bind. O DESFAZER.sh faz o mesmo.

Se o `caddy reload` falhar, o Caddy segue com a configuração anterior. O
contêiner do servidor não é recriado no bootstrap, então a 7777 e o PIN não mudam.

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
4. Após verificar o RELEASE: `git checkout` do commit, **build da imagem**, **smoke**;
   só então troca compose, site, tarball `crptochat-web-dist.tar.gz`, binários; `compose up`;
   PIN inalterado ou **restaura snapshot** (nunca re-renderiza compose de commit antigo).
5. O que vai para o site (cliente web e downloads) ganha `755` nas pastas e `644`
   nos arquivos **antes** de ser publicado: os downloads rodam com `umask 077`.
   Cada etapa confere os arquivos que baixou contra o SHA-256 da `RELEASE` assinada.

### Falha no meio: restore sempre roda

A VM usa `/bin/sh` = **dash**. No dash, uma função chamada dentro de `||`, `&&`,
`if` ou `!` roda com o `set -e` desligado até lá dentro, mesmo depois do próprio
`set -e`. Por isso o corpo do deploy é chamado como comando simples, com o
`set -e` desligado só em volta da chamada:

```sh
set +e
deploy_body "$new_tag" "$commit" "$pin_before" "$manifest_dir"
rc=$?
set -e
```

Assim, qualquer etapa que falhe (build, `compose up`, health check…) interrompe
o deploy, e em seguida o restore devolve o compose, o site (índice, downloads e
cliente web) e a tag da imagem anterior. O restore tenta todos os passos mesmo
que um falhe.

`tests/test_deploy_dash.py` roda o `main` real no dash, com git e `deploy_lib`
de verdade e uma RELEASE assinada, fazendo uma etapa falhar de propósito.

Sem `/var/lib/crptochat/deploy.state`: deriva tag e PIN do contêiner em execução ou aborta
**antes** de mudar qualquer coisa.

## `authorized_keys`

```
command="/usr/local/sbin/crptochat-deploy",restrict ssh-ed25519 AAAA… deploy-github
```

## Rede Caddy / WebSocket

Rede `crptochat`, `/ws` → `crptochat:7778` e estático em `/web/`. O bootstrap faz três coisas:

- **Liga o contêiner do Caddy à rede.**
- **Ajusta o `/opt/crptochat-setup/docker-compose.caddy.yml`**, para um `docker compose up`
  futuro manter o Caddy na rede:
  - edição **estrutural**: lê a árvore do YAML e só acrescenta
    `networks.crptochat.external: true` no topo e `crptochat` em
    `services.caddy.networks`, mantendo `default` se o serviço não tinha nenhuma rede;
  - comentários e o resto do arquivo ficam como estavam;
  - o resultado passa pelo `docker compose config` antes de substituir o arquivo.
- **Acrescenta `handle /ws` ao Caddyfile**, com `caddy validate` num contêiner descartável
  antes de tocar no Caddy, e de novo dentro dele antes do reload.
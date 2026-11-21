# Script de deploy na VM (`crptochat-deploy`)

O GitHub Actions manda só a tag `vX.Y.Z` por SSH (`SSH_ORIGINAL_COMMAND`). Na VM
o `authorized_keys` fixa o comando a `/usr/local/sbin/crptochat-deploy`.

**Pré-requisito:** `/opt/crptochat` num commit com o `packaging/deploy_lib.py` que tem
`check-compose-config`, `check-server-config` e `check-dry-run`. O bootstrap recusa um
checkout mais antigo (do #30, por exemplo) antes de mudar qualquer coisa.

**Bootstrap inicial (uma vez):**

```sh
cd /opt/crptochat && git fetch origin && git checkout main
sudo sh packaging/crptochat-bootstrap.sh
```

Antes de mudar qualquer coisa, o bootstrap copia tudo o que pode tocar para
`/root/bak-vision-<data>/` e grava ali o `DESFAZER.sh`. Esse script é **regravado
antes de cada passo** que muda algo: `deploy.env`, `deploy.state`, criar ou ligar
a rede `crptochat`, o compose do Caddy, o Caddyfile e, **por último**, instalar o
comando de deploy (assim um deploy nunca roda numa VM com o bootstrap pela metade).
A versão e o PIN lidos do contêiner para o `deploy.state` inicial são validados
(`^[0-9]+(\.[0-9]+){1,3}$` e `^[0-9a-f]{64}$`) antes de gravar. Se ele falhar no meio, inclusive no `caddy validate` ou no `caddy reload`,
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

1. Lê o `deploy.state` **por parsing** (`sed -n 's/^chave=//p'`), nunca com `.`:
   `prev_tag`, `tls_pin`, `built`, `commit`, cada um validado. Sem o arquivo, deriva
   tag e PIN do contêiner em execução (validados) ou aborta antes de mudar qualquer coisa.
2. Confere que o volume de dados e a rede `crptochat` existem (a rede é do bootstrap).
3. Baixa `RELEASE` + `RELEASE.sig` do **crptochat-client** (não usa tags no crptochat-oss)
   e confere assinatura, versão = tag pedida, `commit` ancestral de `origin/main`. Recusa
   downgrade e uma RELEASE da **mesma tag** com `built` mais antigo que o publicado.
4. **Snapshot obrigatório** (se falhar, nada começa): ID da imagem em uso, compose,
   `git rev-parse HEAD` de `/opt/crptochat` e `deploy.state`.
5. Backup do volume, `git checkout` do commit da RELEASE, e então **staging**: cliente web,
   binários (só nomes da lista fechada `DOWNLOAD_ASSET_NAMES`), páginas e compose, tudo
   baixado e conferido contra o SHA-256 da RELEASE, com `755`/`644` antes de publicar.
6. O compose é renderizado **por estrutura** a partir do template: só
   `networks.crptochat` e `volumes.crptochat-data` mudam, ambos `external: true` com o
   nome real. O resultado passa por `docker compose config --format json` e precisa ter
   rede e volume external, a 7777 publicada e `--ws-port 7778`.
7. Build e smoke da imagem.
8. **`docker compose up -d --no-build --dry-run`** com o compose novo, antes de parar
   qualquer coisa: se o plano criar, recriar ou remover rede ou volume, ou mexer em outro
   contêiner (o Caddy), o deploy aborta com o servidor intocado. Nunca `--remove-orphans`:
   o Caddy é "orphan" do projeto `crptochat-setup`.
9. Publica o compose (rename), `compose up -d --no-build` e **health**: rodando,
   `RestartCount` 0, na rede `crptochat`, 7777 publicada, probe TLS com o PIN do
   `deploy.state`, banner da versão, a linha `cliente web: ws atrás do proxy` no log, um
   handshake WebSocket a partir da rede `crptochat` e uma segunda amostra (60 s) ainda
   com `RestartCount` 0.
10. Só então publica site, cliente web e downloads por **rename atômico** e grava o
    `deploy.state` (`prev_tag`, `tls_pin`, `built`, `commit`).

### Falha no meio: restore que prova que restaurou

A VM usa `/bin/sh` = **dash**. No dash, uma função chamada dentro de `||`, `&&`,
`if` ou `!` roda com o `set -e` desligado até lá dentro, mesmo depois do próprio
`set -e`. Por isso o corpo do deploy nunca é chamado nesses contextos: ele roda em
segundo plano e o `main` espera por ele, com o `set -e` desligado só em volta:

```sh
set +e
deploy_body "$new_tag" "$commit" "$pin_before" "$manifest_dir" &
BODY_PID=$!
wait "$BODY_PID"
rc=$?
```

Um INT/TERM/HUP durante o deploy interrompe o `wait`, encerra o corpo (e os processos
filhos) e roda o restore; durante o restore os sinais são ignorados.

O restore devolve por rename o que já tinha sido publicado (sem janela de 404), o
`HEAD` de `/opt/crptochat` (`git checkout -q main; git reset -q --hard <antigo>`), a tag
da imagem anterior e o `deploy.state`. Depois **prova** que a 7777 voltou: contêiner
rodando, rede `crptochat` em `NetworkSettings.Networks`, 7777 publicada e probe TLS com o
PIN do `deploy.state`. Se o `compose up` não basta (o contêiner volta sem rede, como no
incidente do primeiro deploy do #31), recria o serviço com
`up -d --no-build --force-recreate server`. Um compose cujo dry-run mexeria na rede nunca
roda; nesse caso o servidor é recriado na imagem anterior com o compose já verificado.

| rc | stdout/stderr |
| --- | --- |
| 0 | `deploy vX.Y.Z concluído` |
| 1 | `deploy falhou …; estado anterior restaurado` (restore verificado) |
| 2 | `PIN TLS inesperado (estado anterior restaurado)` |
| 3 | `RESTORE FALHOU: 7777 possivelmente fora` (precisa de gente na VM) |

O stdout vira o log público do Actions: ele só tem linhas de resumo e o PIN abreviado
(`abcd…wxyz`). Logs do contêiner, saída do build, do dry-run e do probe ficam só em
`/var/log/crptochat/deploy-AAAAMMDD.log` (root, `600`). Erros vão para o stderr.

`tests/test_deploy_dash.py` roda o `main` real no dash, com git e `deploy_lib`
de verdade e uma RELEASE assinada; o stub do docker reproduz o incidente (rede com
outro hash + endpoint do Caddy, `start` que volta sem rede).

## `authorized_keys`

```
command="/usr/local/sbin/crptochat-deploy",restrict ssh-ed25519 AAAA… deploy-github
```

## Rede Caddy / WebSocket

Rede `crptochat`, `/ws` → `crptochat:7778` e estático em `/web/`. A rede é do
bootstrap, compartilhada com o Caddy; o compose do servidor só a referencia como
`external`. O bootstrap faz três coisas:

- **Cria a rede se faltar e liga o contêiner do Caddy a ela** (checado pelas chaves de
  `NetworkSettings.Networks`).
- **Ajusta o `/opt/crptochat-setup/docker-compose.caddy.yml`**, para um `docker compose up`
  futuro manter o Caddy na rede:
  - edição **estrutural**: lê a árvore do YAML e só acrescenta
    `networks.crptochat.external: true` no topo e `crptochat` em
    `services.caddy.networks`, mantendo `default` se o serviço não tinha nenhuma rede;
  - comentários e o resto do arquivo ficam como estavam;
  - o resultado passa pelo `docker compose config` antes de substituir o arquivo.
- **Acrescenta `handle /ws` ao Caddyfile**, com `caddy validate` num contêiner descartável
  antes de tocar no Caddy, e de novo dentro dele antes do reload.
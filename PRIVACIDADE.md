# Política de privacidade

Este documento descreve o comportamento do software **crptochat** no site de
downloads, no cliente de terminal, no cliente de navegador e no servidor. Ele
descreve o que o código faz, não promete mais do que o código entrega. Onde há
uma exceção, ela está escrita aqui.

Atualizado em 25/11/2026.

## Quem responde por quê

Este documento descreve o comportamento do software. Quem opera um servidor é
responsável por ele: o projeto não coleta nem tem acesso a dados de nenhum
servidor. Se você usa o servidor de outra pessoa, pergunte a quem opera o que
fica em disco, por quanto tempo e como são os backups; o cliente não consegue
impor uma política ao servidor alheio. Se você é quem opera, veja
*Servidor próprio e equipamentos de OT*.

## Resumo

| Peça | O que fica | O que é visível fora do seu dispositivo |
|---|---|---|
| Site de downloads | Nada enviado pela página; a hospedagem pode registrar requisições HTTP comuns | IP, horário e arquivo pedido, para a hospedagem |
| GitHub (código, releases, atualizações) | Nada seu é publicado pelo cliente | IP nas consultas à API e no download dos arquivos |
| Cliente de terminal | Chave cifrada pela sua senha e preferências locais | nick, chave pública e versão no login; envelopes cifrados |
| Servidor | Contas, canais, cargos e bans, chaves públicas e one-time keys, histórico cifrado com autor/horário, recados fixados em claro, envelopes offline; IP pseudonimizado | metadados das conexões, inclusive o IP, durante a sessão e até 10 min depois |
| Cliente web | Chaves e preferências no IndexedDB; arquivos do build no cache do service worker | os mesmos metadados do cliente de terminal |

O servidor é **cego** em relação ao conteúdo: mensagens, voz e arquivos são
cifrados nos clientes e chegam a ele como bytes opacos. Ele não tem as chaves de
nenhuma conversa. Isso não significa que ele não veja nada: os metadados estão
descritos em *O servidor*.

## O site de downloads, o GitHub e proxies

A página em `https://crptochat.acpt.app.br/downloads/` é estática. Ela não
carrega nada de fora, não usa cookies, não tem analytics, não tem formulário e
não envia o que você digita ou clica para o projeto. A política de segurança de
conteúdo (CSP) bloqueia script, estilo e imagem de origem externa; a página usa
script próprio inline.

A hospedagem do site e um proxy ou CDN na frente dele podem guardar logs de
requisição (endereço IP, horário, arquivo pedido, agente de usuário). O mesmo
vale para o **GitHub**: o código, as releases e os arquivos baixados pelo
`/update` ficam lá, e o GitHub vê o IP das consultas à API pública e dos
downloads; o site é o espelho. Não vai token, login ou cookie nessa consulta,
mesmo que `GH_TOKEN` exista no ambiente. `CRPTOCHAT_UPDATE_URL` e
`CRPTOCHAT_UPDATE_FALLBACK=off` permitem fixar ou desligar a origem.

Se houver um **proxy reverso** (Caddy, nginx etc.) na frente do servidor de
chat, ele vê o IP das conexões e pode manter logs de acesso próprios, conforme
a configuração de quem o opera. Quem precisa esconder o IP desses serviços deve
usar Tor ou VPN; o crptochat não faz isso automaticamente.

## O cliente de terminal

- **Chave local.** No primeiro uso de um nick, a chave de identidade é gerada no
  seu computador e salva cifrada com a sua senha em
  `~/.config/crptochat/identities/<nick>.json`. Ela também pode ficar num
  pendrive (`--key-dir`) ou num cofre (`--vault`, `--key-cmd`). A chave privada
  nunca é enviada ao servidor: ele recebe o nick e a chave pública. A
  fingerprint mostrada no cliente é calculada a partir dessa chave pública; não
  é um campo separado guardado no banco.
- **Preferências locais.** Último servidor/nick, cor da interface e opções do
  cliente ficam no seu perfil e não são enviados. A exceção é a **versão do
  cliente**: o handshake de login envia `ver` com o número da versão ao
  servidor, para compatibilidade entre clientes e servidor.
- **Atualizações.** `/update` consulta a release pública do repositório do
  cliente na API do GitHub (com o site como espelho) para baixar um manifesto
  assinado e conferir a versão. O GitHub e a hospedagem veem o IP, como descrito
  acima; o conteúdo da conversa não entra nessa consulta.
- **Mensagens.** Sem histórico ligado, as mensagens existem na memória de quem
  está online. Fechou o cliente, sumiram. Canais privados podem ligar um
  histórico (`/mode #canal +h N`): o conteúdo fica cifrado, com prazo, mas o
  servidor guarda em claro o nick de quem enviou cada linha e o horário. Ele não
  consegue ler o conteúdo; como qualquer intermediário, pode atrasar, omitir ou
  apagar o que repassa. Mensagens diretas com `/ttl` também expiram no
  destinatário.
- **Arquivos.** Por padrão os blocos passam pelo servidor em memória, cifrados, e
  não são gravados lá. Com `/send nick arquivo --p2p` a transferência é direta
  entre as duas máquinas: **cada lado vê o endereço IP do outro** e o cliente
  pede confirmação mostrando isso antes de conectar.
- **Prints e dispositivos comprometidos** não têm como ser impedidos pelo
  protocolo. O código é aberto, mas ainda não passou por auditoria independente.

## O servidor

O `registry.db` guarda:

- **Contas:** nick, chave pública, data de criação, cargo (`role`) e, se houver,
  o banimento da conta (`banned_until`, com o motivo em `ban_reason`, em texto).
  A fingerprint não é armazenada: é calculada a partir da chave pública.
- **Canais:** nome, se são privados, salt e verificador da senha (nos privados),
  política e `epoch`. Cargos e bans de canal ficam por nick em `chan_roles` e
  `chan_bans`.
- **Chaves de conversa:** a prekey pública de cada nick e as one-time keys
  (`prekeys`, `onetime_keys`), usadas para abrir conversas com quem está offline.
- **Histórico de canais privados**, quando ligado: uma cópia cifrada por linha,
  com prazo e `epoch`. O conteúdo é cifrado, mas o **nick de quem enviou
  (`sender`) e o horário (`ts`) ficam em claro** no banco.
- **Recados fixados** no `#lobby`: o **texto fica em claro**, assim como nível,
  autor, data de criação e expiração.
- **Mensagens offline** (`mailbox`), quando a caixa postal está ligada:
  destinatário, horário e bytes opacos, por padrão até 14 dias, com limites por
  destinatário e por remetente. O remetente não é gravado no banco: fica só como
  HMAC na memória, sob uma chave gerada quando o servidor inicia.
- **Endereços:** `registrations` guarda, por até uma hora, o hash do endereço de
  quem criou nick recentemente, se o limite estiver ligado. `ip_bans` guarda o
  **hash do endereço**, o motivo (`reason`), o **nick no rótulo (`label`)**, a
  data e `until`; o banimento pode ser **permanente** (`until = 0`). Assim, o
  ban liga nick e endereço.
- **Configuração** na tabela `meta`, incluindo `ip_salt`, limites e a origem de
  atualizações sugerida aos clientes.

Sobre o hash de IP: a chave do HMAC (`ip_salt`) fica na tabela `meta` do **mesmo
`registry.db`**. Quem tiver o banco ou um backup consegue testar IPv4 por força
bruta e recuperar o endereço. Portanto isso é **pseudonimização, não
anonimização**: o IP não é gravado em texto, mas também não é irreversível.

Em memória ficam, durante a sessão e por mais `ip_memory` (padrão 10 minutos)
após a desconexão, contadores de limite de taxa e infrações por
conexão/endereço. Esses contadores usam o **IP em texto como chave**, então o
IP fica na memória enquanto a sessão está aberta **e por até `ip_memory`
(padrão 10 minutos) depois da desconexão**. Bans de canal de anônimos guardam o endereço **na RAM até o servidor
reiniciar**, sem expiração por tempo.

O painel (`crptochat-dash`) mostra só contagens agregadas: nenhum nick, canal,
endereço ou conteúdo aparece no snapshot. O console de administração
(`crptochat-admin`) mostra nicks, cargos, bans (com motivo e rótulo) e recados
porque a moderação precisa deles; não mostra o conteúdo das conversas.

O servidor **vê metadados**: quem está conectado, em quais canais, para qual
nick um envelope é encaminhado, o horário, a faixa de tamanho dos pacotes e o
**endereço IP da conexão**. Isso é necessário para encaminhar a conversa. Com
`--no-tls`, quem observa a rede também vê esses metadados; o conteúdo continua
cifrado. Um proxy reverso na frente do servidor vê o mesmo e pode registrar em
logs próprios, como descrito acima.

Histórico, envelopes offline e recados expirados são apagados do banco quando
expiram ou quando o operador desliga o recurso. Contas, cargos e bans ficam até
o operador removê-los. Backups feitos pelo operador podem conservar cópias por
mais tempo.

## Servidor próprio e equipamentos de OT

Quem roda o servidor na própria rede — inclusive num gateway, num servidor de
manutenção ou junto de sistemas de OT — decide onde ele escuta e o que fica em
disco. O programa não muda a sua política de rede: ele só atende no endereço e
na porta que você indicar e guarda o que está descrito em *O servidor*.

- **Rede e escuta.** Prefira uma VM ou uma VLAN de administração, separada da
  rede de controle de processo. Use `--host` para a interface da rede de
  manutenção em vez de deixar `0.0.0.0`, e libere no firewall apenas a porta de
  escuta (`7777/TCP` no padrão) para quem deve conectar — e a porta WebSocket
  (`--ws-port`, padrão `7778`) se usar o cliente web. Em rede fechada, o
  certificado autoassinado gerado no primeiro start, com a
  fingerprint passada aos clientes por `--pin`, é suficiente: o conteúdo
  continua cifrado ponta a ponta. `--no-tls` só em segmento isolado e de
  confiança; mesmo assim os metadados trafegam visíveis para quem captura a rede.
- **Cliente web em OT.** A entrada WebSocket fica desligada sem `--ws-port`
  (padrão `7778`, escutando em `127.0.0.1`). O navegador não aceita o certificado
  autoassinado que o terminal fixa por `--pin`: use um proxy reverso com
  certificado de CA confiável pelos navegadores (a documentação do projeto mostra
  CA pública; uma CA interna vale se estiver instalada nos equipamentos) e
  `--ws-origin` com a origem exata da página, ou `--ws-cert`/`--ws-key` com
  esse certificado. `--ws-origin` compara a origem de forma exata e devolve HTTP
  403 para as demais. Sem isso, use o cliente de terminal com `--pin`. O
  WebSocket não exige Internet: exige a página e o servidor na mesma rede e a
  origem conferida.
- **Dados em disco.** O diretório passado em `--data` é criado com permissão
  `0700`; mantenha-o restrito, rode o serviço com usuário dedicado e não o
  compartilhe por NFS/SMB nem exponha `registry.db`. O banco guarda contas,
  canais, prekeys, histórico cifrado (quando ligado), recados fixados **em
  claro** e hashes de IP com a chave `ip_salt` **no mesmo arquivo**: quem copia o
  banco copia também a chave dos hashes. Trate backups como dado sensível,
  defina prazo de retenção e apague cópias antigas.
- **Histórico, caixa postal e recados.** O histórico de canal vem desligado
  (`history 0`); a caixa postal vem **ligada**, com mensagens para offline
  guardadas por 14 dias (`mail_days 14`). Se precisar de histórico, use o menor
  prazo aceitável com `crptochat-admin --data DIR policy #canal history N`;
  para desligar, use `history 0`. Para desligar a caixa postal, use
  `crptochat-admin --data DIR ratelimit set mail_days 0`. Mesmo cifrado, o
  histórico mantém em claro o autor e o horário de cada linha, e os recados
  fixados mantêm o texto em claro: restrinja quem pode publicar recados.
- **Quem entra.** Use `--no-guests` quando só pessoas conhecidas devem conectar
  (o `guests off` do console vale só até o reinício). `--reg-token`, ou a variável
  `CRPTOCHAT_REG_TOKEN`, controla quem pode criar nick; `--reg-limit` ajusta o
  número de nicks novos por IP por hora (padrão 5). Anônimos que recebem ban de
  canal mantêm o endereço na RAM até o servidor reiniciar.
- **IP e limites.** O servidor precisa do endereço para encaminhar e limitar:
  ele fica em texto na RAM durante a sessão e por `ip_memory` (padrão 600 s)
  depois da desconexão. Para reduzir esse tempo,
  `crptochat-admin --data DIR ratelimit set ip_memory 0` — mas a proteção
  contra flood entre reconexões rápidas some. Os limites por IP usam o endereço
  real do socket; na entrada WebSocket atrás de proxy, usam o último
  `X-Forwarded-For` (aceito só de loopback ou de intervalos passados em
  `--ws-trusted-proxy`). Se várias estações passam pelo mesmo NAT/gateway,
  aumente `conns_per_ip` e `anon_per_ip`, ou elas dividirão a mesma cota.
- **Logs e observabilidade.** O `crptochat-server` não mantém log de acesso por
  conexão: na saída padrão saem o banner e a fingerprint do certificado; no
  stderr, só o tipo de erro interno — nunca conteúdo de mensagem. O proxy
  reverso, o balanceador e o Docker/journal podem guardar logs próprios; os do
  proxy incluem IP e horário — configure a retenção deles também.
  `crptochat-dash` mostra só contagens agregadas; `crptochat-admin` mostra nicks,
  cargos, bans e recados (não mostra conversas). Os sockets locais do painel e
  do console são criados com permissão `0600`, mas o diretório que os contém
  também precisa estar protegido.
- **Atualizações em rede fechada.** `/update` consulta GitHub e site por padrão.
  Nos clientes, fixe `CRPTOCHAT_UPDATE_URL` num espelho interno ou use
  `CRPTOCHAT_UPDATE_FALLBACK=off` quando não houver saída para a Internet. O
  servidor pode **sugerir** uma origem com `updaterepo`, mas cada cliente
  confirma a troca e continua exigindo release assinada. Sem Internet, baixe a
  release num computador conectado, confira a assinatura e leve o artefato para
  a rede OT por mídia controlada.
- **Arquivos e P2P.** Blocos enviados pelo servidor são cifrados e passam só pela
  memória; `crptochat-admin --data DIR ratelimit set file_max_mb 0` desliga esse
  envio se a política de OT não o permitir. `/send --p2p` conecta as duas
  máquinas diretamente e revela o IP de cada lado — evite onde isso não for
  aceitável.
- **Manutenção.** Antes de trocar de versão, rode
  `python -m crptochat.selftest` (no Docker,
  `docker exec crptochat python -m crptochat.selftest --probe 127.0.0.1:7777 --stats /data`)
  e confira o painel agregado. Faça backup de todo o diretório `--data` antes de
  atualizar e teste o restore: `ip_salt`, contas e prekeys precisam voltar
  juntos, ou os clientes não abrem conversas com quem estava offline.

## O cliente de navegador

O cliente web em `web/` ainda é uma **prova de conceito**. Quem serve a página
pode trocar o código que o navegador executa; por isso, use-o somente numa origem
de que você confie, a mesma confiança que você deposita no binário assinado do
cliente de terminal.

- Chaves e preferências ficam no **IndexedDB** do navegador, montado como
  `/data` no Pyodide e sincronizado com o disco local. A chave privada e a senha
  não saem do navegador; a chave pública vai ao servidor, como no cliente de
  terminal.
- O **service worker** guarda os arquivos do build visitado num cache do
  navegador para visitas seguintes e uso offline. Limpar os dados do site apaga
  esse cache **e também as chaves guardadas no IndexedDB**: faça backup antes.
- A conexão com o servidor é WebSocket (`wss://` em produção; `ws://localhost`
  apenas para testes) e obedece às mesmas regras do cliente de terminal. O
  servidor vê os mesmos metadados descritos acima.
- A página não tem analytics, fontes externas nem script de terceiros. A CSP
  limita conexões à própria origem e a WebSockets (`wss://` em produção;
  `ws://localhost` só no desenvolvimento); o endereço do chat é o que você
  informa na página.

## O que o projeto não faz

- Não pede e-mail, telefone ou documento. A "conta" é só um nick ligado a uma
  chave pública no servidor que você escolheu.
- Não cria um perfil seu entre sites, não usa cookies de rastreamento e não
  exibe publicidade.
- Não guarda o conteúdo das conversas em claro no servidor. As exceções em claro
  são o texto dos recados fixados e o autor/horário do histórico de canal,
  descritos acima.
- O código do projeto não envia dados a terceiros além do servidor de chat
  escolhido, do GitHub/site (downloads e atualizações) e de proxies da
  hospedagem. Esses serviços veem o IP, como descrito acima. O projeto não vende
  dados e não tem acesso ao que acontece nos servidores. O código está sob a
  licença MIT e pode ser auditado e modificado por qualquer pessoa.

## Anônimos e escolhas

O acesso anônimo (`crptochat host -a`) não cria conta: a chave é descartável e
só existe na memória, e o nick é derivado dela (`anon-1a2b3c4d`). Ainda assim, a
conexão tem um endereço IP, usado para limites e banimentos como qualquer outra;
se receber ban de canal, o endereço fica na RAM até o servidor reiniciar. Um
operador pode desligar anônimos (`--no-guests`).

Para reduzir o que fica: use servidor próprio, mantenha o histórico de canal
desligado (padrão), desligue a caixa postal (`mail_days 0`) se não precisar de
mensagens offline, use `/ttl` nas diretas, guarde a chave num pendrive ou cofre,
evite `--p2p` quando não quiser revelar seu IP ao destinatário e use Tor ou VPN
quando não quiser que o servidor, o GitHub/site ou proxies vejam seu IP. Nada
disso torna o servidor cego aos metadados da conexão.

## Escopo e contato

Este documento descreve o comportamento do software, não a política de um
serviço específico. Quem opera um servidor é responsável por ele; o projeto não
coleta nem tem acesso a dados de nenhum servidor. Dúvidas podem ser abertas como
issue; para um problema de segurança, use o canal privado descrito em
[SECURITY.md](SECURITY.md) — não publique a vulnerabilidade em issue aberta.

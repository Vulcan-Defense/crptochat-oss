# Política de privacidade

Esta política descreve o que o **crptochat** trata como dado pessoal no site de
downloads, no cliente de terminal, no cliente de navegador e no servidor. Ela é
deliberadamente curta: o projeto não tem cadastro, não tem perfil e não vende
dados. Onde há uma exceção, ela está escrita aqui.

Atualizado em 24/11/2026.

## Resumo

| Peça | O que fica | O que não fica |
|---|---|---|
| Site de downloads | Nada enviado pela página; o provedor da hospedagem pode registrar requisições HTTP comuns | cookies, analytics, fontes ou scripts de terceiros |
| Cliente de terminal | A chave do nick, cifrada pela sua senha, no seu dispositivo; preferências locais | mensagens cifradas ficam só na memória, salvo histórico que você ligou |
| Servidor | Nick, chave pública, canais, prekeys e envelopes cifrados para quem está offline | conteúdo das conversas, arquivos em claro, IP em texto, log de chat |
| Cliente web | Chaves e preferências no IndexedDB; arquivos do build no cache do service worker | rastreamento, analytics, conexão com terceiros |

O servidor é **cego** em relação ao conteúdo: as mensagens, a voz e os arquivos
são cifrados nos clientes e chegam a ele como bytes opacos. Ele não tem a chave
de nenhuma conversa.

## O site de downloads

A página em `https://crptochat.acpt.app.br/downloads/` é estática. Ela não
carrega nada de fora, não usa cookies, não tem analytics, não tem formulário e
não envia o que você digita ou clica para o projeto. A política de segurança de
conteúdo (CSP) da página bloqueia script, estilo e imagem de origem externa.

Isso não significa que a hospedagem não registre nada: como qualquer site
estático, o servidor HTTP/provedor pode guardar logs de requisição (endereço IP,
horário, arquivo pedido, agente de usuário). Esses registros são controlados por
quem publica o site, não pelo código deste repositório. Se você publica o site,
defina a sua própria retenção de logs.

## O cliente de terminal

- **Chave local.** No primeiro uso de um nick, a chave de identidade é gerada no
  seu computador e salva cifrada com a sua senha em
  `~/.config/crptochat/identities/<nick>.json`. Ela também pode ficar num
  pendrive (`--key-dir`) ou num cofre (`--vault`, `--key-cmd`). A chave privada
  nunca é enviada ao servidor: ele recebe o nick e a chave pública.
- **Preferências locais.** Último servidor/nick, cor da interface, opções do
  cliente e a versão/build instalada ficam no seu perfil. Nada disso é enviado.
- **Atualizações.** `/update` consulta uma origem pública (por padrão, a release
  mais recente do repositório do cliente na API pública do GitHub, com o site
  como espelho) para baixar um manifesto assinado e verificar a versão. Não vai
  nenhum token, login ou cookie nessa consulta, mesmo que `GH_TOKEN` exista no
  seu ambiente. `CRPTOCHAT_UPDATE_URL` e `CRPTOCHAT_UPDATE_FALLBACK=off` permitem
  fixar ou desligar a origem.
- **Mensagens.** Sem histórico ligado, as mensagens existem na memória de quem
  está online. Fechou o cliente, sumiram. Canais privados podem ligar um
  histórico cifrado com a senha do canal (`/mode #canal +h N`); o servidor guarda
  só o cifrado, com prazo, e não consegue ler nem alterar as linhas. Mensagens
  diretas com `/ttl` também expiram no destinatário.
- **Arquivos.** Por padrão os blocos passam pelo servidor em memória, cifrados, e
  não são gravados lá. Com `/send nick arquivo --p2p` a transferência é direta
  entre as duas máquinas: **cada lado vê o endereço IP do outro** e o cliente
  pede confirmação mostrando isso antes de conectar.
- **Prints e dispositivos comprometidos** não têm como ser impedidos pelo
  protocolo. O código é aberto, mas ainda não passou por auditoria independente.

## O servidor

Quem roda o servidor é o controlador desses dados. Se você usa um servidor de
outra pessoa, pergunte a ela o que fica em disco, por quanto tempo e como são os
backups; o cliente não consegue impor uma política ao servidor alheio.

O `registry.db` guarda:

- nick → chave pública e fingerprint;
- nome, tipo, salt e verificador dos canais;
- prekeys públicas de cada nick, usadas para abrir conversas com quem está
  offline;
- envelopes cifrados deixados para quem está offline (destinatário, horário e
  bytes opacos), quando a caixa postal está ligada — por padrão até 14 dias,
  com limites por destinatário e por remetente;
- por até uma hora, hashes com chave secreta dos endereços que criaram nick
  recentemente, se o limite de registros por IP estiver ligado. O **IP em texto
  nunca é gravado**; banimentos por endereço também usam hash com chave secreta.

Em memória, por poucos minutos, ficam contadores de limite de taxa e infrações
por conexão/endereço. O painel do operador (`crptochat-dash`) mostra só contagens
agregadas: nenhum nick, canal, endereço ou conteúdo aparece no snapshot. O
console de administração (`crptochat-admin`) mostra nicks porque moderação
precisa deles, mas não mostra mensagens nem endereços em texto.

O servidor **vê metadados**: quem está conectado, em quais canais, para qual nick
um envelope é encaminhado, o horário e a faixa de tamanho dos pacotes. Isso é
necessário para encaminhar a conversa. Com `--no-tls`, esses metadados também
ficam visíveis para quem observa a rede; o conteúdo continua cifrado.

Histórico de canal e envelopes offline são apagados do banco quando expiram ou
quando o operador desliga o recurso. Backups feitos pelo operador podem
conservar cópias até que o próprio backup expire.

## O cliente de navegador

O cliente web em `web/` ainda é uma **prova de conceito**. Quem serve a página
pode trocar o código que o navegador executa; por isso, use-o somente numa origem
de que você confie, a mesma confiança que você deposita no binário assinado do
cliente de terminal.

- Chaves e preferências ficam no **IndexedDB** do navegador, montado como
  `/data` no Pyodide e sincronizado com o disco local. Elas não são enviadas para
  o site nem para o servidor de chat; a senha da chave é digitada na página.
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

- Não cria conta, não pede e-mail, telefone ou documento.
- Não cria um perfil seu entre sites, não usa cookies de rastreamento e não
  exibe publicidade.
- Não guarda mensagens legíveis, arquivos em claro nem histórico de conversa no
  servidor, exceto o cifrado descrito acima.
- Não vende, compartilha ou transfere dados a terceiros. O código está sob a
  licença MIT e pode ser auditado e modificado por qualquer pessoa.

## Anônimos e escolhas

O acesso anônimo (`crptochat host -a`) não cria conta: a chave é descartável e
só existe na memória, e o nick é derivado dela (`anon-1a2b3c4d`). Ainda assim, a
conexão tem um endereço IP, usado para limites e banimentos como qualquer outra.
Um operador pode desligar anônimos (`--no-guests`).

Para reduzir o que fica: use servidor próprio, mantenha o histórico de canal
desligado (padrão), desligue a caixa postal (`mail_days 0`) se não precisar de
mensagens offline, use `/ttl` nas diretas, guarde a chave num pendrive ou cofre e
evite `--p2p` quando não quiser revelar seu IP ao destinatário.

## Mudanças e contato

Alterações nesta política vêm junto com uma release do projeto e são
discutidas em pull request público. Dúvidas podem ser abertas como issue; para
um problema de segurança, use o canal privado descrito em
[SECURITY.md](SECURITY.md) — não publique a vulnerabilidade em issue aberta.

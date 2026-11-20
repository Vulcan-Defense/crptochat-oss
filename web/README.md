# crptochat no navegador (prova de conceito)

O **mesmo** cliente de terminal (o pacote `crptochat`, com a mesma
criptografia, o mesmo protocolo e a mesma interface Textual) rodando dentro do
navegador. O Python roda em WebAssembly (Pyodide), a interface é desenhada num
xterm.js e a conexão com o servidor usa a entrada WebSocket
(`crptochat-server --ws-port`, veja o README principal).

> **Prova de conceito.** Serve para medir e decidir. Não publique para
> usuários: ainda falta o que está em *Próximos passos*, principalmente a
> parte de integridade de quem serve a página.

## Como funciona

| Peça | Onde |
|---|---|
| Python 3.14 + `cryptography` 47 em WebAssembly | `vendor/pyodide/` (Pyodide 314.0.8) |
| Textual, Rich e dependências (wheels puros) | `vendor/wheels/` |
| Terminal na página | `vendor/xterm/` (xterm.js 6) |
| Driver do Textual para o xterm.js | `py/crptoweb/driver.py` |
| Conexão por WebSocket no lugar do TCP | `py/crptoweb/net.py` |
| Login (abre ou cria a chave) e início | `py/crptoweb/boot.py`, `src/main.js` |
| Chaves e preferências | IndexedDB (sistema de arquivos `/data` do Pyodide, sincronizado a cada 3 s e ao sair) |

- **Nada vem de CDN em tempo de execução.** `fetch_vendor.py` baixa tudo uma
  vez e confere cada arquivo contra `vendor.lock.json`: tarballs do npm pelo
  sha512, wheels do PyPI pelo sha256, e os pacotes compilados do Pyodide pelo
  sha256 do `pyodide-lock.json`, que vem do tarball do npm já conferido.
  O `loadPackage` do Pyodide confere de novo o sha256 de cada pacote dele.
- **CSP rígida** na página:
  - `script-src 'self' 'wasm-unsafe-eval'`, então não roda script inline,
    `eval` nem script de outra origem;
  - `connect-src` limitado a `wss:` (e `ws://localhost` para testes);
  - nada de analytics nem fontes externas.
  - Exceção: `style-src` precisa de `'unsafe-inline'`, porque o xterm.js
    aplica estilos inline. Estilo não executa código.
- **`build.py`** gera `dist/`, que é o site estático:
  - `app.zip` é determinístico (datas e ordem fixas) e leva `crptochat/` (sem
    os módulos do servidor) e `crptoweb/`;
  - `SHA256SUMS` lista todos os arquivos.
- **Sem threads no navegador.** `asyncio.to_thread` roda em linha, então o
  scrypt de canal com senha trava a tela por um instante.

## Chaves e cofres de senha

O navegador não roda `pass`, `secret-tool` nem `vault` como o terminal. A
integração passa então pelo que os cofres já fazem no navegador:

- **Senha da chave no gerenciador de senhas.** O formulário segue o que os
  gerenciadores procuram: nick como usuário, `current-password` para entrar,
  `new-password` com confirmação ao criar uma chave (aí eles oferecem gerar uma
  senha forte). Funciona com o gerenciador do Chrome, o iCloud Keychain, e com
  Bitwarden, 1Password e KeePassXC pela extensão. No Chromium a página também
  pede para salvar pela Credential Management API, depois que a senha abriu a chave.
- **A chave no cofre, para backup e para levar entre aparelhos.** "Exportar /
  backup" dá o **mesmo blob** que o terminal guarda no cofre: a semente Ed25519
  já cifrada com a senha (scrypt + ChaCha20-Poly1305, presa ao nick). Ele é
  baixado como arquivo ou copiado, e vai no gerenciador como nota segura ou
  anexo, ou no `pass insert -m crptochat/NICK`. "Importar chave" aceita
  esse blob, o arquivo `~/.config/crptochat/NICK.json` do terminal ou a saída
  de `pass show crptochat/NICK`. A senha é conferida no navegador antes de
  guardar. Uma chave diferente para o mesmo nick só é substituída se a pessoa
  marcar "substituir".
- **Backup obrigatório ao criar uma chave:** antes de abrir o chat aparece o
  painel de backup, porque a chave existe só naquele navegador. No iPhone o
  Safari pode apagar os dados do site.

Quem pegar o blob ainda precisa da senha. Guardar os dois no mesmo cofre deixa
o cofre como a única proteção, igual a usar `--pass-cmd` no terminal.

Ainda não: desbloquear com Face ID ou digital (passkey com a extensão PRF do
WebAuthn), e mover a chave por dentro do chat (o "Mover minha chave" do menu
chama programas locais e não funciona no navegador).

## Rodar localmente

```sh
python3 web/fetch_vendor.py          # baixa e confere web/vendor/ (~19 MB)
python3 web/build.py                 # gera web/dist/

# servidor com a entrada WebSocket, aceitando a página servida em localhost:8123
crptochat-server --no-tls --ws-port 7778 --ws-origin http://localhost:8123

# servir web/dist (precisa do tipo certo para .mjs e .wasm; o http.server do Python 3.13 já serve)
python3 -m http.server 8123 --bind 127.0.0.1 --directory web/dist
```

Abra `http://localhost:8123`, informe `ws://localhost:7778`, um nick e uma
senha. Em produção o endereço é `wss://`, com certificado de CA pública.

## O que a PoC mostrou

Testado com Chromium headless (Playwright), contra um servidor local:

| | Resultado |
|---|---|
| Carregamento no desktop (sem limitação) | **~5 s** até o botão "entrar" (Pyodide 2,3 s · pacotes 0,6 s · imports 2 s) |
| Login até ONLINE | < 1 s |
| DM navegador → terminal e terminal → navegador | **funciona**, cifrada de ponta a ponta, decifrada pelo cliente de terminal de verdade |
| Chave no IndexedDB | sobrevive a recarregar; senha errada é recusada; a mesma chave é aceita pelo servidor |
| Memória | ~80 MB de memória do WebAssembly + ~20 MB de heap JS |
| Tamanho | 18,9 MB sem compressão; **~11,4 MB com gzip** (o wasm cai de 9,6 para 3,5 MB) |
| Celular emulado (Pixel 7, CPU 4× mais lenta, 4G de 9 Mbit/s) | **42 s** no primeiro acesso, 33 s com cache: o gargalo é CPU (iniciar o Python e importar), não rede |
| Layout em tela de celular | **quebra**: os painéis laterais têm largura fixa e espremem o chat |

Achado que virou correção no cliente principal: o `cryptography` do Pyodide
tem o módulo ML-KEM, mas o OpenSSL dele não tem o algoritmo. O
`crypto.HAS_MLKEM` agora testa gerando uma chave, não só importando. No
navegador, por enquanto, **não há a camada pós-quântica** (fica só o X25519,
como nos clientes com `cryptography` < 47). A negociação de capacidades (`pq`)
já trata isso com os outros clientes.

## Limitações conhecidas

- Sem camada pós-quântica (acima).
- Transferência P2P de arquivos e mensagens de voz não funcionam (precisam de
  socket e de ffmpeg); arquivos pelo servidor devem funcionar, mas não testei.
- `/update` não se aplica: quem atualiza é o site.
- Não testado em Safari/iOS de verdade, nem em Firefox.

## Próximos passos

1. **Velocidade no celular.** O objetivo é abaixo de 10 s num aparelho médio:
   - snapshot de memória do Pyodide (carregar o Python já inicializado);
   - `.pyc` pré-compilados no `app.zip`;
   - imports preguiçosos;
   - uma `python_stdlib.zip` só com o necessário;
   - service worker com cache.
2. **Layout estreito na TUI.** Abaixo de ~80 colunas, esconder os painéis
   laterais (abrir pelo menu). Isso melhora também o terminal pequeno.
3. **Publicação** (só com isto pronto):
   - build reproduzível no CI;
   - manifesto assinado na release;
   - hospedagem fora do servidor do chat;
   - monitor externo comparando o que é servido com a release;
   - página "verificar esta instalação".
4. Aviso de "adicione à tela inicial" no iOS antes de criar a chave, e
   desbloqueio por passkey (PRF) como atalho opcional.

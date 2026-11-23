# Laboratório de red team (local)

O servidor e a rede no papel de atacante, contra o cliente de verdade. Tudo roda
em `127.0.0.1`, com servidores, identidades e certificados descartáveis num
diretório temporário. Nenhum host de fora é contatado.

```sh
python3 lab/redteam/attacks.py          # todos os cenários
python3 lab/redteam/attacks.py s2 s5    # só alguns
```

**Não aponte nada daqui para um servidor de produção.** O lab não lê
`deploy.env`, não usa `--pin` real e só conecta em `127.0.0.1`.

## Cenários e resultado esperado

| | Ataque | Resultado |
|---|---|---|
| S1 | Operador grava todos os frames e procura texto em claro | Nenhum segredo nos frames. Ele vê metadados: quem, para quem, quando, tipo de frame, tamanho em faixa. |
| S2 | MITM no **primeiro contato**: o servidor entrega a chave dele no lugar das duas e repassa | **Funciona**: é o limite do TOFU. A conversa direta abre com o aviso "ainda não foi conferido". Comparando o número de segurança (`/whois`, `/verify`) por outro canal, os números diferem e o MITM aparece. |
| S3 | Trocar a chave **depois** do primeiro contato | Recusado; aviso de chave trocada; mensagens bloqueadas até `/verify`. |
| S4 | Proxy TLS com outro certificado | Com `--pin`: não conecta. Sem `--pin`, na primeira conexão: conecta e fixa o certificado do proxy (o cliente avisa e mostra a fingerprint), mas o proxy só vê blobs cifrados. |
| S5 | Alterar um bit / reenviar uma mensagem direta | Alterada: descartada. Reenviada: exibida uma vez, antes e depois da primeira resposta (corrigido na 1.21.1; antes disso, enquanto o outro não respondia, o envelope v1 podia ser reenviado). |
| S6 | Remover a prekey pós-quântica antes do **primeiro** recado offline | Limite do primeiro contato: o recado sai só com X25519, e o cliente avisa. Depois de ver a prekey ML-KEM uma vez, ele recusa (`tests/test_pq.py`). |
| S7 | Reenviar uma mensagem de canal (aberto e privado) | Exibida uma vez. |

"Funciona" em S2, S4 (sem `--pin`) e S6 não é bug escondido: é o modelo de
confiança no primeiro uso, o mesmo do Signal sem conferir o número de segurança.
A defesa é conferir: `--pin` publicado no site, e `/verify` com quem importa.

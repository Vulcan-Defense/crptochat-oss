#!/usr/bin/env python3
"""Laboratório de red team do crptochat: o servidor (e a rede) como atacante.

Roda tudo em 127.0.0.1, com servidores, identidades e certificados descartáveis
num diretório temporário. Não fala com nenhum host de fora e recusa rodar se
CRPTOCHAT_HOME apontar para algo que não seja o diretório do lab.

    python3 lab/redteam/attacks.py            # todos os cenários
    python3 lab/redteam/attacks.py s2 s4      # só alguns

Cada cenário imprime o que o atacante tentou, o que se esperava e o que
aconteceu. Os resultados "o ataque funciona" são achados do modelo (TOFU, primeiro
contato), não bugs: ver lab/redteam/README.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

LAB = Path(tempfile.mkdtemp(prefix="crptochat-redteam-"))
os.environ["CRPTOCHAT_HOME"] = str(LAB / "cfg")          # nunca o ~/.config de verdade

from crptochat import crypto  # noqa: E402
from crptochat.client import ChatClient, ChatError  # noqa: E402
from crptochat.protocol import b64d, b64e  # noqa: E402
from crptochat.server import Server, ensure_cert, make_ssl_context  # noqa: E402
from crptochat.store import Identity, Store  # noqa: E402

crypto.SCRYPT_N = 2 ** 10            # identidades descartáveis: scrypt leve para o lab ser rápido
HOST = "127.0.0.1"                   # o lab só escuta e conecta aqui
PW = "senha-da-chave-lab"
RESULTS: list[tuple[str, str, str, str]] = []   # (cenário, ataque, esperado, observado)


def note(scn: str, attack: str, expected: str, observed: str) -> None:
    RESULTS.append((scn, attack, expected, observed))
    print(f"  [{scn}] {attack}\n      esperado:  {expected}\n      observado: {observed}")


async def until(cond, timeout=8.0) -> bool:
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > end:
            return False
        await asyncio.sleep(0.02)
    return True


def received(client, buffer=None) -> list[tuple[str, str]]:
    return [(d["nick"], d["text"]) for k, d in client.events
            if k == "message" and not d["mine"] and (buffer is None or d["buffer"] == buffer)]


class Lab:
    """Servidores e clientes de um cenário; tudo é fechado no fim."""

    def __init__(self, name: str):
        self.dir = LAB / name
        self.dir.mkdir(parents=True)
        self.servers: list[Server] = []
        self.clients: list[tuple[ChatClient, asyncio.Task]] = []
        self.closers: list = []

    async def server(self, tag: str, tls: bool = False, spy: list | None = None) -> tuple[Server, int]:
        srv = Server(str(self.dir / f"srv-{tag}"), reg_limit=0)
        if spy is not None:                      # o operador grava tudo o que chega
            dispatch = srv._dispatch

            def logged(sess, msg, _d=dispatch):
                spy.append(json.dumps(msg, ensure_ascii=False))
                return _d(sess, msg)
            srv._dispatch = logged
        ctx = make_ssl_context(*ensure_cert(str(self.dir / f"srv-{tag}"))) if tls else None
        port = await srv.start(HOST, 0, ctx)
        self.servers.append(srv)
        return srv, port

    async def client(self, port: int, nick: str, *, identity: Identity | None = None, tls: bool = False,
                     pin: str | None = None, store_id: str | None = None) -> ChatClient:
        ident = identity or Identity.create(nick, PW, key_dir=str(self.dir / f"keys-{nick}-{port}"))
        events: list = []
        client = ChatClient(HOST, port, ident, Store(store_id or f"{self.dir.name}:{port}:{nick}"),
                            lambda k, d: events.append((k, d)), tls=tls, pin=pin,
                            download_dir=str(self.dir / f"dl-{nick}"))
        client.events = events
        task = asyncio.create_task(client.run())
        self.clients.append((client, task))
        await until(lambda: client.online or any(k == "fatal" for k, _ in events))
        return client

    async def close(self):
        for client, task in self.clients:
            try:
                await client.close()
            except Exception:
                pass
            task.cancel()
        for closer in self.closers:
            closer()
        for srv in self.servers:
            await srv.stop()


# ------------------------------------------------------------------ cenários

async def s1_passive_relay():
    """O operador grava tudo o que passa pelo servidor e procura texto em claro."""
    print("\nS1 · servidor honesto, operador curioso (grava todos os frames)")
    lab, wire = Lab("s1"), []
    try:
        _, port = await lab.server("s1", spy=wire)
        alice, bob, carol = [await lab.client(port, n) for n in ("alice", "bob", "carol")]
        for c in (alice, bob, carol):
            await c.join("#lobby")
        await until(lambda: len(alice.channels["#lobby"].members) == 2)
        alice.send_message("#lobby", "SEGREDO-CANAL-ABERTO-1")
        await alice.create("#cofre", "SENHA-DO-CANAL-PRIVADO")
        await bob.join("#cofre", "SENHA-DO-CANAL-PRIVADO")
        await until(lambda: "bob" in alice.channels["#cofre"].recv)
        bob.send_message("#cofre", "SEGREDO-CANAL-PRIVADO-2")
        await alice.send_dm("carol", "SEGREDO-DM-3")
        await until(lambda: len(received(carol)) >= 2 and received(alice, "#cofre"))
        secrets = ["SEGREDO-CANAL-ABERTO-1", "SENHA-DO-CANAL-PRIVADO", "SEGREDO-CANAL-PRIVADO-2", "SEGREDO-DM-3", PW]
        leaked = [s for s in secrets if any(s in f for f in wire)]
        note("S1", f"procurar {len(secrets)} segredos em {len(wire)} frames gravados",
             "nenhum texto, senha de canal ou senha de chave no servidor",
             "VAZOU: " + ", ".join(leaked) if leaked else "nenhum segredo nos frames (só blobs cifrados)")
        kinds = sorted({json.loads(f).get("t") for f in wire})
        directs = [json.loads(f) for f in wire if json.loads(f).get("t") == "direct"]
        note("S1", "o que o operador consegue tirar dos frames (metadados)",
             "quem, para quem, quando, tipo de frame, faixa de tamanho",
             f"tipos {kinds}; diretas visíveis como 'alice → {directs[0]['to']}' com "
             f"{len(b64d(directs[0]['body']))} bytes (tamanho em faixa, não exato)" if directs else f"tipos {kinds}")
    finally:
        await lab.close()


async def s2_first_contact_mitm():
    """Servidor malicioso no meio desde o primeiro contato (ninguém fez /verify)."""
    print("\nS2 · servidor malicioso, MITM no primeiro contato (sem /verify)")
    lab = Lab("s2")
    stolen: list[str] = []
    try:
        # O operador separa os dois usuários e põe um "sósia" de cada um do outro lado.
        # Numa implantação real é um processo só; aqui são duas portas do mesmo atacante.
        _, port_a = await lab.server("lado-alice")
        _, port_b = await lab.server("lado-bob")
        alice = await lab.client(port_a, "alice")
        bob = await lab.client(port_b, "bob")
        fake_bob = await lab.client(port_a, "bob", identity=Identity.create("bob", "x", key_dir=str(lab.dir / "mallory-bob")))
        fake_alice = await lab.client(port_b, "alice", identity=Identity.create("alice", "x", key_dir=str(lab.dir / "mallory-alice")))

        async def relay(shadow: ChatClient, peer: str, forward: ChatClient, to: str):
            """O sósia decifra (a chave é do atacante), guarda e repassa com a outra chave falsa."""
            seen = 0
            while True:
                msgs = received(shadow, "@" + peer)
                for _, text in msgs[seen:]:
                    stolen.append(text)
                    await forward.send_dm(to, text)
                seen = len(msgs)
                await asyncio.sleep(0.02)
        for args in ((fake_bob, "alice", fake_alice, "bob"), (fake_alice, "bob", fake_bob, "alice")):
            lab.closers.append(asyncio.create_task(relay(*args)).cancel)

        await alice.send_dm("bob", "oi bob, aqui é a alice: SEGREDO-MITM-1")
        ok1 = await until(lambda: received(bob, "@alice"))
        await bob.send_dm("alice", "oi alice: SEGREDO-MITM-2")
        ok2 = await until(lambda: received(alice, "@bob"))
        note("S2", "trocar as chaves de bob e alice no primeiro contato e repassar as mensagens",
             "limite do TOFU: funciona sem conferência fora de banda (a conversa abre com o aviso de não conferido)",
             f"atacante leu {stolen!r}; bob recebeu={ok1}, alice recebeu={ok2}, "
             f"nenhum aviso de chave: {not any(k == 'key_changed' for k, _ in alice.events + bob.events)}")

        sn_alice = crypto.safety_number(alice.identity.ik, alice.peers["bob"].ik)
        sn_bob = crypto.safety_number(bob.identity.ik, bob.peers["alice"].ik)
        note("S2", "alice e bob comparam o número de segurança (/whois + /verify) por outro canal",
             "os números diferem: o MITM aparece",
             "DIFEREM, MITM detectado" if sn_alice != sn_bob else "IGUAIS (o MITM passaria despercebido)")
    finally:
        await lab.close()


async def s3_key_swap_after_contact():
    """O servidor troca a chave de bob depois que alice já falou com ele."""
    print("\nS3 · servidor troca a chave depois do primeiro contato")
    lab = Lab("s3")
    try:
        srv, port = await lab.server("s3")
        alice, bob = await lab.client(port, "alice"), await lab.client(port, "bob")
        await alice.send_dm("bob", "primeiro contato")
        await until(lambda: received(bob, "@alice"))
        mallory, mallory_dh = crypto.ed_generate(), crypto.x_generate()
        sess = srv.sessions["bob"]
        sess.ik = crypto.ed_pub(mallory)
        sess.dh = crypto.x_pub(mallory_dh)
        sess.dh_sig = crypto.sign(mallory, crypto.dh_message("bob", sess.dh))
        try:
            await alice.resolve("bob")
            await alice.send_dm("bob", "SEGREDO-DEPOIS-DA-TROCA")
            outcome = "ENVIOU para a chave do atacante"
        except ChatError as exc:
            outcome = f"recusado ({exc.code}); evento key_changed: {any(k == 'key_changed' for k, _ in alice.events)}"
        note("S3", "apresentar outra chave (assinada pelo atacante) para bob",
             "recusado: a chave fixada vence e as mensagens ficam bloqueadas até /verify", outcome)
    finally:
        await lab.close()


async def s4_tls_mitm():
    """Proxy TLS na rede, com certificado próprio, entre o cliente e o servidor."""
    print("\nS4 · MITM de rede no TLS (proxy com outro certificado)")
    lab, seen = Lab("s4"), []
    try:
        _, real_port = await lab.server("s4", tls=True)
        probe = await lab.client(real_port, "probe", tls=True, store_id="s4-probe")
        real_pin = probe.store.server_pin()

        proxy_dir = lab.dir / "proxy"
        proxy_dir.mkdir()
        proxy_ctx = make_ssl_context(*ensure_cert(str(proxy_dir)))
        upstream_ctx = ssl.create_default_context()
        upstream_ctx.check_hostname = False
        upstream_ctx.verify_mode = ssl.CERT_NONE

        async def handle(r, w):
            ur, uw = await asyncio.open_connection(HOST, real_port, ssl=upstream_ctx)

            async def pipe(src, dst, log):
                try:
                    while line := await src.readline():
                        if log:
                            seen.append(line.decode(errors="replace"))
                        dst.write(line)
                        await dst.drain()
                finally:
                    dst.close()
            await asyncio.gather(pipe(r, uw, True), pipe(ur, w, False), return_exceptions=True)
        proxy = await asyncio.start_server(handle, HOST, 0, ssl=proxy_ctx)
        lab.closers.append(proxy.close)
        proxy_port = proxy.sockets[0].getsockname()[1]

        pinned = await lab.client(proxy_port, "alice", tls=True, pin=real_pin, store_id="s4-alice")
        fatal = [d.get("code") for k, d in pinned.events if k == "fatal"]
        note("S4", "cliente com --pin (o do site) atravessa o proxy",
             "recusa conectar (pin)", f"online={pinned.online}, erro={fatal or 'nenhum'}")

        naive = await lab.client(proxy_port, "bob", tls=True, store_id="s4-bob")
        await naive.join("#lobby")
        naive.send_message("#lobby", "SEGREDO-ATRAS-DO-PROXY")
        await asyncio.sleep(0.3)
        leaked = any("SEGREDO-ATRAS-DO-PROXY" in f for f in seen)
        note("S4", "cliente SEM --pin, primeira conexão, atravessa o proxy",
             "conecta (TOFU fixa o certificado do proxy), mas o proxy só vê o que o servidor veria",
             f"online={naive.online}, pin gravado = do proxy: {naive.store.server_pin() != real_pin}, "
             f"texto em claro no proxy: {leaked} ({len(seen)} frames lidos)")
    finally:
        await lab.close()


async def s5_tamper_and_replay():
    """O servidor altera ou reenvia uma mensagem direta."""
    print("\nS5 · servidor adultera e reenvia mensagens diretas")
    lab = Lab("s5")
    try:
        srv, port = await lab.server("s5")
        alice, bob = await lab.client(port, "alice"), await lab.client(port, "bob")
        await alice.send_dm("bob", "aquecimento")
        await until(lambda: received(bob, "@alice"))
        mode = {"m": None}
        captured: list[dict] = []
        original = srv._op_direct

        def evil(sess, msg):
            if sess.nick == "alice" and mode["m"] == "tamper":
                body = bytearray(b64d(msg["body"]))
                body[len(body) // 2] ^= 0x01
                msg = {**msg, "body": b64e(bytes(body))}
            if sess.nick == "alice" and mode["m"] == "replay":
                captured.append(msg)
            return original(sess, msg)
        srv._op_direct = evil

        mode["m"] = "tamper"
        before = len(received(bob, "@alice"))
        await alice.send_dm("bob", "SEGREDO-ADULTERADO")
        await asyncio.sleep(0.4)
        got = received(bob, "@alice")[before:]
        note("S5", "trocar um bit do envelope no caminho", "bob descarta (autenticação falha)",
             f"bob recebeu {got!r}" if got else "descartado, nada exibido")

        async def replay(text: str) -> str:
            mode["m"] = "replay"
            await alice.send_dm("bob", text)
            await until(lambda: received(bob, "@alice")[-1:] == [("alice", text)])
            mode["m"] = None
            body = b64d(captured[-1]["body"])
            fmt = {28: "v1", 21: "v2", 57: "v3 (Double Ratchet)"}.get(len(body) % 64, "?")
            srv.sessions["bob"].send({"t": "direct", "from": "alice", "body": captured[-1]["body"]})
            await asyncio.sleep(0.4)
            return f"envelope {fmt}: exibida {sum(t == text for _, t in received(bob, '@alice'))}x"

        # bob ainda não respondeu: alice não sabe se ele tem cliente novo e manda v1
        note("S5", "reenviar um envelope antes de bob responder (conversa de mão única)",
             "bob mostra uma vez só", await replay("SEGREDO-REENVIADO-1"))
        await bob.send_dm("alice", "respondi")
        await until(lambda: received(alice, "@bob"))
        await asyncio.sleep(0.2)
        note("S5", "reenviar um envelope depois de bob responder",
             "bob mostra uma vez só", await replay("SEGREDO-REENVIADO-2"))
    finally:
        await lab.close()


async def s6_pq_downgrade():
    """O servidor tira a prekey pós-quântica de bob antes do primeiro recado offline."""
    print("\nS6 · downgrade do pós-quântico (ML-KEM) no recado offline")
    lab = Lab("s6")
    try:
        srv, port = await lab.server("s6")
        bob = await lab.client(port, "bob")
        from crptochat import mailbox
        await until(lambda: srv.db.execute("SELECT COUNT(*) FROM onetime_keys WHERE nick_lower='bob'").fetchone()[0]
                    == mailbox.OPK_TARGET and not bob.mail.busy)
        # alice ainda não falou com bob: primeiro contato
        srv.db.execute("UPDATE prekeys SET kem=NULL, kem_sig=NULL WHERE nick_lower='bob'")
        srv.db.commit()
        alice = await lab.client(port, "alice")
        await bob.close()
        await until(lambda: "bob" not in srv.sessions)
        try:
            await alice.send_dm("bob", "SEGREDO-OFFLINE")
            row = srv.db.execute("SELECT body FROM mailbox WHERE to_lower='bob'").fetchone()
            pq = bytes(row[0])[0] == crypto.MAIL_PQ
            out = f"enviou {'COM' if pq else 'SEM'} ML-KEM (só X25519)"
        except ChatError as exc:
            out = f"recusado ({exc.code})"
        warned = any(k == "system" and "sem a camada pós-quântica" in d.get("text", "") for k, d in alice.events)
        note("S6", "remover a prekey ML-KEM antes do primeiro recado offline",
             "limite do primeiro contato: sai só com X25519, e alice é avisada; depois de ver a "
             "ML-KEM uma vez, recusa (tests/test_pq.py)", f"{out}; aviso para alice: {warned}")
    finally:
        await lab.close()


async def s7_channel_replay():
    """O servidor reenvia uma mensagem de canal (aberto e privado)."""
    print("\nS7 · servidor reenvia mensagens de canal")
    for private in (False, True):
        lab, wire = Lab(f"s7-{'priv' if private else 'pub'}"), []
        try:
            srv, port = await lab.server("s7", spy=wire)
            alice, bob = await lab.client(port, "alice"), await lab.client(port, "bob")
            chan = "#cofre" if private else "#lobby"
            if private:
                await alice.create(chan, "senha-do-lab")
                await bob.join(chan, "senha-do-lab")
                await until(lambda: "bob" in alice.channels[chan].recv)
            else:
                await alice.join(chan)
                await bob.join(chan)
                await until(lambda: len(alice.channels[chan].members) == 1)
            await asyncio.sleep(0.2)
            alice.send_message(chan, "SEGREDO-DO-CANAL")
            await until(lambda: received(bob, chan))
            frame = [json.loads(f) for f in wire if json.loads(f).get("t") == "msg"][-1]
            srv.sessions["bob"].send({"t": "msg", "chan": chan, "from": "alice", "body": frame["body"]})
            await asyncio.sleep(0.4)
            n = [t for _, t in received(bob, chan)].count("SEGREDO-DO-CANAL")
            note("S7", f"reenviar uma mensagem em canal {'privado' if private else 'aberto'}",
                 "bob mostra uma vez só", f"exibida {n}x")
        finally:
            await lab.close()


SCENARIOS = {"s1": s1_passive_relay, "s2": s2_first_contact_mitm, "s3": s3_key_swap_after_contact,
             "s4": s4_tls_mitm, "s5": s5_tamper_and_replay, "s6": s6_pq_downgrade, "s7": s7_channel_replay}


async def main(names):
    print(f"laboratório em {LAB} (só {HOST}, tudo descartável)")
    for name in names:
        try:
            await SCENARIOS[name]()
        except Exception as exc:          # um cenário quebrado não esconde os outros
            note(name.upper(), "cenário", "rodar até o fim", f"ERRO NO LAB: {type(exc).__name__}: {exc}")
    print("\nresumo")
    for scn, attack, _, observed in RESULTS:
        print(f"  {scn}  {attack}\n       → {observed}")


if __name__ == "__main__":
    picked = [a.lower() for a in sys.argv[1:]] or list(SCENARIOS)
    unknown = [p for p in picked if p not in SCENARIOS]
    if unknown:
        sys.exit(f"cenário desconhecido: {', '.join(unknown)} (use {', '.join(SCENARIOS)})")
    asyncio.run(main(picked))

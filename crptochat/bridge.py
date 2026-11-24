"""Bridge headless: chat cifrado com LLM local/API e adapters IT/OT.

O processo é um cliente comum do crptochat, com nick registrado e identidade
persistente. Tudo o que vem ou vai para o chat entra em envelopes cifrados de
mensagem direta; o relay só vê metadados. A bridge não abre canais, não aceita
arquivos e não executa comandos de servidor em claro.

Privacidade: o E2EE garantido aqui vai do cliente de chat até a bridge. Dali em
diante o prompt segue em plaintext para o backend escolhido (Ollama local ou
API). Detalhes em PRIVACIDADE.md e no README.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass, field

from . import vault
from .bridge_llm import Backend, LLMError, build_backend
from .bridge_ot import OTAdapters, OTError
from .client import ChatClient
from .protocol import DEFAULT_PORT, valid_nick
from .store import Identity, Store, config_dir

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PIN_RE = re.compile(r"^[0-9a-fA-F]{64}$")
KNOWN = {"server", "nick", "tls_pin", "allowed_users", "passphrase_env", "reg_token_env",
         "key_dir", "vault", "llm", "ot", "chunk_chars", "max_reply_chars",
         "max_prompt_chars", "min_interval_s"}
DEFAULT_SYSTEM = (
    "Você é um assistente de operação conectado a um chat cifrado. "
    "Responda em português, de forma curta e objetiva. "
    "Use apenas os dados de equipamentos que receber no prompt; nunca invente leituras."
)


class ConfigError(Exception):
    """Configuração inválida ou incompleta (nunca contém segredos)."""


def _env_name(value, what: str, default: str | None = None) -> str:
    name = default if value is None else value
    if not isinstance(name, str) or not ENV_NAME.match(name):
        raise ConfigError(f"{what}: nome de variável de ambiente inválido")
    return name


def _env_value(name: str, what: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{what}: variável {name} não está definida")
    return value


def _limit(value, default, low, high, what: str):
    number = default if value is None else value
    if isinstance(number, bool) or not isinstance(number, (int, float)) or not low <= number <= high:
        raise ConfigError(f"{what}: use um número entre {low} e {high}")
    return number


@dataclass
class BridgeConfig:
    server: str
    nick: str
    tls_pin: str
    allowed_users: tuple[str, ...]
    passphrase_env: str = "CRPTOCHAT_PASSPHRASE"
    reg_token_env: str | None = None
    key_dir: str | None = None
    vault: str | None = None
    llm: dict = field(default_factory=dict)
    ot: dict = field(default_factory=dict)
    chunk_chars: int = 1800
    max_reply_chars: int = 12000
    max_prompt_chars: int = 8000
    min_interval_s: float = 1.0

    @property
    def host(self) -> str:
        return self.server.rsplit(":", 1)[0]

    @property
    def port(self) -> int:
        return int(self.server.rsplit(":", 1)[1])

    @property
    def server_id(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def allowed(self) -> set[str]:
        return {n.lower() for n in self.allowed_users}

    @classmethod
    def load(cls, path: str) -> "BridgeConfig":
        if os.name == "posix" and os.path.exists(path):
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode & 0o077:
                raise ConfigError(f"{path}: permissão aberta demais; use chmod 600 (está {oct(mode)})")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            raise ConfigError(f"configuração não encontrada: {path}") from None
        except (OSError, ValueError) as exc:
            raise ConfigError(f"{path}: JSON inválido ({type(exc).__name__})") from None
        if not isinstance(data, dict):
            raise ConfigError("a configuração deve ser um objeto JSON")
        unknown = set(data) - KNOWN
        if unknown:
            raise ConfigError("campos desconhecidos: " + ", ".join(sorted(unknown)))

        server = data.get("server")
        if not isinstance(server, str) or not server.strip():
            raise ConfigError("server: use host[:porta]")
        server = server.strip()
        if ":" in server:
            host, _, port_text = server.rpartition(":")
            if not host or not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
                raise ConfigError("server: porta inválida")
        else:
            server = f"{server}:{DEFAULT_PORT}"

        nick = data.get("nick")
        if not isinstance(nick, str) or not valid_nick(nick) or nick.lower().startswith("anon-"):
            raise ConfigError("nick: use um nick registrado (3-20 letras, números, _ ou -)")
        pin = data.get("tls_pin")
        if not isinstance(pin, str) or not PIN_RE.match(pin.lower().replace(":", "")):
            raise ConfigError("tls_pin: fingerprint SHA-256 do certificado (64 hex)")
        pin = pin.lower().replace(":", "")

        users = data.get("allowed_users")
        if (not isinstance(users, list) or not users or len(users) > 50 or
                any(not isinstance(n, str) or not valid_nick(n) or n.lower().startswith("anon-") for n in users)):
            raise ConfigError("allowed_users: lista de nicks registrados (1 a 50)")
        allowed = tuple(dict.fromkeys(n.lower() for n in users))
        if nick.lower() in allowed:
            raise ConfigError("allowed_users: a bridge não pode falar consigo mesma")

        passphrase_env = _env_name(data.get("passphrase_env"), "passphrase_env", "CRPTOCHAT_PASSPHRASE")
        _env_value(passphrase_env, "passphrase_env")
        reg_token_env = None
        if data.get("reg_token_env") is not None:
            reg_token_env = _env_name(data["reg_token_env"], "reg_token_env")
            _env_value(reg_token_env, "reg_token_env")

        key_dir = data.get("key_dir")
        vault_name = data.get("vault")
        if key_dir is not None and vault_name is not None:
            raise ConfigError("use key_dir ou vault, não os dois")
        if key_dir is not None and (not isinstance(key_dir, str) or not key_dir.strip()):
            raise ConfigError("key_dir: caminho inválido")
        source = None
        if vault_name is not None:
            try:
                source = vault.parse(vault_name)
            except vault.VaultError as exc:
                raise ConfigError(f"vault: {exc}") from None

        llm = data.get("llm")
        if not isinstance(llm, dict):
            raise ConfigError("llm: objeto obrigatório")
        if set(llm) & {"api_key", "token", "password", "secret"}:
            raise ConfigError("llm: nunca coloque segredos no JSON; use api_key_env")
        system = llm.get("system", DEFAULT_SYSTEM)
        if not isinstance(system, str) or len(system) > 4000:
            raise ConfigError("llm.system: texto inválido (máximo 4000 caracteres)")
        llm = dict(llm)
        llm["system"] = system
        timeout = _limit(llm.get("timeout_s"), 60, 1, 300, "llm.timeout_s")
        llm["timeout_s"] = timeout
        try:
            build_backend(llm)       # valida URL, modelo e variáveis de segredo
        except LLMError as exc:
            raise ConfigError(str(exc)) from None
        try:
            OTAdapters(data.get("ot"))
        except OTError as exc:
            raise ConfigError(str(exc)) from None

        return cls(
            server=server, nick=nick, tls_pin=pin, allowed_users=allowed,
            passphrase_env=passphrase_env, reg_token_env=reg_token_env,
            key_dir=key_dir.strip() if key_dir else None,
            vault=vault_name, llm=llm, ot=data.get("ot") or {},
            chunk_chars=int(_limit(data.get("chunk_chars"), 1800, 200, 1800, "chunk_chars")),
            max_reply_chars=int(_limit(data.get("max_reply_chars"), 12000, 200, 20000,
                                       "max_reply_chars")),
            max_prompt_chars=int(_limit(data.get("max_prompt_chars"), 8000, 100, 20000,
                                        "max_prompt_chars")),
            min_interval_s=float(_limit(data.get("min_interval_s"), 1.0, 0, 60, "min_interval_s")),
        )

    def identity(self):
        source = vault.parse(self.vault) if self.vault else None
        if not Identity.exists(self.nick, self.key_dir, source):
            raise ConfigError(
                f"identidade de '{self.nick}' não encontrada. Crie o nick antes com o cliente "
                "normal (crptochat), guardando a chave no mesmo key_dir/cofre, e mantenha-a.")
        return Identity.load(self.nick, _env_value(self.passphrase_env, "passphrase_env"),
                             self.key_dir, source)

    def example(self) -> dict:
        return {
            "server": "chat.exemplo.invalid:7777",
            "nick": "otbot",
            "tls_pin": "COLOQUE_A_FINGERPRINT_SHA256_DO_SERVIDOR_AQUI",
            "allowed_users": ["alice", "bob"],
            "passphrase_env": "CRPTOCHAT_PASSPHRASE",
            "reg_token_env": None,
            "key_dir": None,
            "vault": None,
            "llm": {"backend": "ollama", "base_url": "http://127.0.0.1:11434",
                    "model": "llama3.1", "timeout_s": 60},
            "ot": {"http": {"endpoints": {"temperatura": {
                "url": "https://clp.exemplo.invalid/api/temperatura", "field": "value"}}}},
            "chunk_chars": 1800, "max_reply_chars": 12000,
            "max_prompt_chars": 8000, "min_interval_s": 1.0,
        }


def write_private_json(path: str, obj: dict) -> None:
    """Escreve JSON com 0600 e substituição atômica, sem sobrescrever arquivos."""
    folder = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".bridge-", dir=folder)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.exists(path):
            raise FileExistsError(path)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def chunk_reply(text: str, size: int = 1800, limit: int = 12000) -> list[str]:
    """Quebra uma resposta longa em DMs curtas, dentro de MAX_TEXT."""
    text = str(text)[:limit]
    if not text:
        return []
    parts: list[str] = []
    current = ""
    for line in text.splitlines() or [text]:
        for piece in ([line[i:i + size] for i in range(0, len(line), size)] or [""]):
            if current and len(current) + 1 + len(piece) > size:
                parts.append(current)
                current = ""
            candidate = piece if not current else current + "\n" + piece
            if len(candidate) <= size:
                current = candidate
            else:
                if current:
                    parts.append(current)
                current = piece
    if current:
        parts.append(current)
    if len(parts) == 1:
        return parts
    total = len(parts)
    return [f"[{i}/{total}] {part}" for i, part in enumerate(parts, 1)]


class BridgeService:
    """Event loop headless: recebe DMs permitidas e responde pela mesma via."""

    def __init__(self, cfg: BridgeConfig, *, identity=None, llm_backend: Backend | None = None,
                 adapters: OTAdapters | None = None, client=None, log=None):
        self.cfg = cfg
        self.identity = identity
        self.llm_backend = llm_backend
        self.adapters = adapters
        self.client = client
        self.log = log or (lambda message, level="info":
                           print(f"[bridge] {level}: {message}", file=sys.stderr))
        self.allowed = cfg.allowed
        self.history: dict[str, list[dict]] = {}
        self.last: dict[str, float] = {}
        self.blocked: set[str] = set()
        self.busy: set[str] = set()

    async def run(self) -> None:
        identity = self.identity or self.cfg.identity()
        backend = self.llm_backend or build_backend(self.cfg.llm)
        adapters = self.adapters or OTAdapters(self.cfg.ot)
        if self.client is None:
            store = Store(self.cfg.server_id)
            download_dir = os.path.join(config_dir(), "bridge-downloads")
            self.client = ChatClient(self.cfg.host, self.cfg.port, identity, store, self.on_event,
                                     tls=True, pin=self.cfg.tls_pin,
                                     reg_token=os.getenv(self.cfg.reg_token_env)
                                     if self.cfg.reg_token_env else None,
                                     download_dir=download_dir)
        self.log(f"bridge {identity.nick} em {self.cfg.server}; allowlist: "
                 + ", ".join(sorted(self.allowed)))
        await adapters.start(lambda message: self.log(message, "err"))
        try:
            await self.client.run()
        finally:
            await adapters.close()

    def on_event(self, kind: str, data: dict) -> None:
        if kind == "status" and data.get("state") in ("online", "offline"):
            self.log("conectado" if data["state"] == "online" else "desconectado")
        elif kind == "fatal":
            self.log("conexão encerrada: " + str(data.get("code", "fatal")), "err")
        elif kind == "key_changed":
            nick = str(data.get("nick", "")).lower()
            self.blocked.add(nick)
            self.log(f"chave de {nick} mudou; respondendo bloqueado até verificação manual "
                     "(a bridge nunca aceita mudanças sozinha)", "err")
        elif (kind == "message" and not data.get("mine") and
              str(data.get("buffer", "")).startswith("@")):
            nick = str(data.get("nick", ""))
            key = nick.lower()
            if key not in self.allowed or key in self.blocked or key in self.busy:
                return
            self.busy.add(key)
            self.client.spawn(self._handle(nick, str(data.get("text", ""))))

    async def _handle(self, nick: str, text: str) -> None:
        key = nick.lower()
        try:
            now = asyncio.get_running_loop().time()
            if now - self.last.get(key, 0.0) < self.cfg.min_interval_s:
                return
            self.last[key] = now
            reply = await self._dispatch(key, text[:self.cfg.max_prompt_chars])
            if reply:
                for part in chunk_reply(reply, self.cfg.chunk_chars, self.cfg.max_reply_chars):
                    await self.client.send_dm(nick, part)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Nunca registramos prompt, resposta ou corpo de erro de terceiros.
            self.log(f"falha ao responder {key}: {type(exc).__name__}", "err")
            try:
                await self.client.send_dm(nick, "não consegui responder agora; tente de novo.")
            except Exception:
                pass
        finally:
            self.busy.discard(key)

    async def _dispatch(self, key: str, text: str) -> str:
        text = text.strip()
        lowered = text.lower()
        if lowered in ("ajuda", "help", "/help"):
            return ("comandos: llm <pergunta> (ou só a pergunta) · "
                    "llmctx <pergunta> (inclui leituras IT/OT) · "
                    "ot lista · ot ler tipo:nome · ot contexto · status · ajuda")
        if lowered == "status":
            return (f"online · backend {self.llm_backend.name} · adapters "
                    + (", ".join(a.kind for a in (self.adapters.http, self.adapters.modbus,
                                                 self.adapters.mqtt) if a) or "nenhum"))
        if lowered in ("ot lista", "ot list"):
            names = self.adapters.names()
            return "recursos: " + (", ".join(names) if names else "nenhum")
        if lowered == "ot contexto":
            lines = await self.adapters.context()
            return "\n".join(lines) if lines else "sem leituras disponíveis"
        if lowered.startswith("ot ler "):
            return await self.adapters.read(text[7:].strip())
        if lowered.startswith("llmctx "):
            prompt = text[7:].strip()
        elif lowered.startswith("llm "):
            prompt = text[4:].strip()
        else:
            prompt = text
        if not prompt:
            return "escreva uma pergunta depois de llm (ou llmctx)."
        messages = await self._messages(key, prompt, with_context=lowered.startswith("llmctx "))
        answer = (await self.llm_backend.chat(messages, self.cfg.llm["timeout_s"])).strip()
        if not answer:
            return "o backend não devolveu resposta."
        self._remember(key, prompt, answer)
        return answer

    async def _messages(self, key: str, prompt: str, with_context: bool) -> list[dict]:
        history = self.history.setdefault(key, [])
        if with_context:
            lines = await self.adapters.context()
            if lines:
                prompt = ("Dados de equipamentos (somente leitura):\n" + "\n".join(lines)
                          + "\n\nPergunta: " + prompt)
        return ([{"role": "system", "content": self.cfg.llm["system"]}]
                + history[-6:] + [{"role": "user", "content": prompt[:self.cfg.max_prompt_chars]}])

    def _remember(self, key: str, prompt: str, answer: str) -> None:
        history = self.history.setdefault(key, [])
        history.extend([{"role": "user", "content": prompt[:self.cfg.max_prompt_chars]},
                        {"role": "assistant", "content": answer[:self.cfg.max_reply_chars]}])
        while len(history) > 6 or sum(len(m["content"]) for m in history) > 12000:
            history.pop(0)


def default_config_path() -> str:
    return os.environ.get("CRPTOCHAT_BRIDGE_CONFIG") or os.path.join(config_dir(), "bridge.json")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="crptochat-bridge",
        description="Bridge headless: LLM local/API e IT/OT por mensagens diretas cifradas",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("O prompt e a resposta são cifrados entre o cliente de chat e a bridge. "
                "A bridge repassa o texto ao backend em claro: provedores remotos de LLM "
                "veem o prompt; Ollama local mantém tudo na máquina da LLM."))
    ap.add_argument("--config", help=f"arquivo JSON (padrão: {default_config_path()})")
    ap.add_argument("--init", metavar="ARQUIVO", help="cria um modelo de config com 0600 e sai")
    ap.add_argument("--check", action="store_true", help="valida a config e sai")
    args = ap.parse_args(argv)

    try:
        if args.init:
            write_private_json(args.init, BridgeConfig("", "", "", ()).example())
            print(f"modelo criado em {args.init} (chmod 600). Preencha tls_pin e os segredos por env.")
            return
        cfg = BridgeConfig.load(args.config or default_config_path())
        if args.check:
            print(f"ok: {cfg.nick} em {cfg.server}; backend {cfg.llm['backend']}; "
                  f"allowlist {', '.join(cfg.allowed_users)}")
            return
        asyncio.run(BridgeService(cfg).run())
    except (ConfigError, OSError) as exc:
        sys.exit(f"crptochat-bridge: {exc}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

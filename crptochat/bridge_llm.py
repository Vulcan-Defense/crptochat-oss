"""Backends de LLM para a bridge.

Limite de confiança: o crptochat cifra a conversa entre o cliente de chat e o
processo da bridge. Da bridge para o backend a mensagem é plaintext. Com Ollama
em 127.0.0.1 o texto não sai da máquina; com uma API OpenAI-compatible o
provedor recebe o prompt e a resposta em claro. Isto não é E2EE com o provedor.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

import certifi

MAX_RESPONSE = 2 * 1024 * 1024
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class LLMError(Exception):
    """Falha de configuração ou de chamada ao backend (sem texto do prompt)."""


def check_url(value: str, what: str) -> str:
    """Só https, ou http em loopback (para Ollama/llama.cpp locais)."""
    if not isinstance(value, str) or not value.strip():
        raise LLMError(f"{what}: URL obrigatória")
    parts = urllib.parse.urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise LLMError(f"{what}: use uma URL http:// ou https://")
    if parts.username is not None or parts.password is not None:
        raise LLMError(f"{what}: a URL não pode conter usuário/senha")
    if parts.scheme == "http" and parts.hostname.lower() not in LOOPBACK:
        raise LLMError(f"{what}: http:// só é permitido em loopback; use https://")
    return value.strip().rstrip("/")


def secret_from_env(name: str, what: str) -> str:
    """O config guarda o *nome* da variável, nunca o segredo."""
    if not isinstance(name, str) or not ENV_NAME.match(name):
        raise LLMError(f"{what}: nome de variável de ambiente inválido")
    value = os.environ.get(name)
    if not value:
        raise LLMError(f"{what}: variável {name} não está definida")
    return value


def _endpoint(base: str, suffixes: tuple[str, ...]) -> str:
    for suffix in suffixes:
        if base.endswith(suffix):
            return base
    return base + suffixes[0]


def _safe_error(exc: BaseException) -> LLMError:
    if isinstance(exc, urllib.error.HTTPError):
        return LLMError(f"HTTP {exc.code}")
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return LLMError("tempo esgotado")
    if isinstance(exc, urllib.error.URLError):
        return LLMError("falha de rede")
    if isinstance(exc, (ValueError, json.JSONDecodeError)):
        return LLMError("resposta inválida")
    return LLMError(type(exc).__name__)


async def post_json(url: str, payload: dict, headers: dict | None = None,
                    timeout: float = 30.0) -> dict:
    """POST small JSON, in a thread, with verified TLS. Never logs bodies."""

    def send():
        data = json.dumps(payload, ensure_ascii=True).encode("ascii")
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **(headers or {}),
        }, method="POST")
        ctx = ssl.create_default_context(cafile=certifi.where()) if url.startswith("https://") else None
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as reply:
                raw = reply.read(MAX_RESPONSE + 1)
        except Exception as exc:
            raise _safe_error(exc) from None
        if len(raw) > MAX_RESPONSE:
            raise LLMError("resposta grande demais")
        try:
            obj = json.loads(raw)
        except ValueError:
            raise LLMError("resposta inválida") from None
        if not isinstance(obj, dict):
            raise LLMError("resposta inválida")
        return obj

    return await asyncio.to_thread(send)


class Backend:
    name = "?"

    async def chat(self, messages: list[dict], timeout: float) -> str:
        raise NotImplementedError


class OllamaBackend(Backend):
    """LLM local: por padrão http://127.0.0.1:11434/api/chat."""

    name = "ollama"

    def __init__(self, config: dict):
        self.base = check_url(config.get("base_url", "http://127.0.0.1:11434"), "llm.base_url")
        self.url = _endpoint(self.base, ("/api/chat",))
        self.model = config.get("model")
        if not isinstance(self.model, str) or not self.model.strip():
            raise LLMError("llm.model: modelo obrigatório")
        self.options = config.get("options") or {}
        if not isinstance(self.options, dict) or len(self.options) > 20:
            raise LLMError("llm.options: objeto muito grande")
        for key, value in self.options.items():
            if not isinstance(key, str) or not isinstance(value, (str, int, float, bool)):
                raise LLMError("llm.options: use valores simples")

    async def chat(self, messages: list[dict], timeout: float) -> str:
        payload = {"model": self.model, "messages": messages, "stream": False}
        if self.options:
            payload["options"] = self.options
        data = await post_json(self.url, payload, timeout=timeout)
        try:
            return str(data["message"]["content"])
        except (KeyError, TypeError):
            raise LLMError("resposta inválida") from None


class OpenAICompatibleBackend(Backend):
    """API OpenAI-compatible; a chave vem só de uma variável de ambiente."""

    name = "openai"

    def __init__(self, config: dict):
        self.base = check_url(config.get("base_url", "https://api.openai.com"), "llm.base_url")
        self.url = _endpoint(self.base, ("/v1/chat/completions", "/chat/completions"))
        self.model = config.get("model")
        if not isinstance(self.model, str) or not self.model.strip():
            raise LLMError("llm.model: modelo obrigatório")
        self.api_key = secret_from_env(config.get("api_key_env", "OPENAI_API_KEY"), "llm.api_key_env")
        self.organization = None
        if config.get("organization_env"):
            self.organization = secret_from_env(config["organization_env"], "llm.organization_env")

    async def chat(self, messages: list[dict], timeout: float) -> str:
        headers = {"Authorization": "Bearer " + self.api_key}
        if self.organization:
            headers["OpenAI-Organization"] = self.organization
        data = await post_json(self.url, {"model": self.model, "messages": messages},
                               headers, timeout=timeout)
        try:
            choices = data["choices"]
            return str(choices[0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            raise LLMError("resposta inválida") from None


def build_backend(config: dict) -> Backend:
    if not isinstance(config, dict):
        raise LLMError("llm: objeto obrigatório")
    name = config.get("backend")
    if name == "ollama":
        return OllamaBackend(config)
    if name == "openai":
        return OpenAICompatibleBackend(config)
    raise LLMError("llm.backend: use 'ollama' ou 'openai'")

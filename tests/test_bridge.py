"""Bridge LLM/IT-OT: configuração, adapters somente leitura e privacidade dos DMs."""

import asyncio
import json
import os
import struct

import pytest

from crptochat import bridge, bridge_llm, bridge_ot, crypto
from crptochat.client import ChatClient
from crptochat.store import Identity, Store
from test_e2e import Harness, messages, run, until


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CRPTOCHAT_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("CRPTOCHAT_PASSPHRASE", "pw")
    monkeypatch.setattr(crypto, "SCRYPT_N", 2 ** 10)
    return tmp_path


def config_dict(**over):
    data = {
        "server": "127.0.0.1:7777",
        "nick": "otbot",
        "tls_pin": "a" * 64,
        "allowed_users": ["alice"],
        "llm": {"backend": "ollama", "model": "teste", "base_url": "http://127.0.0.1:11434"},
        "ot": {"http": {"endpoints": {
            "temp": {"url": "https://clp.invalid/api", "field": "valor"},
        }}},
        "min_interval_s": 0,
    }
    data.update(over)
    return data


def write_config(tmp_path, data):
    path = tmp_path / "bridge.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


class FakeBackend(bridge_llm.Backend):
    name = "fake"

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def chat(self, messages, timeout):
        self.calls.append(messages)
        return self.reply


def test_config_rejects_missing_pin_guest_and_secret(home):
    bad = config_dict()
    bad.pop("tls_pin")
    with pytest.raises(bridge.ConfigError, match="tls_pin"):
        bridge.BridgeConfig.load(write_config(home, bad))

    bad = config_dict(allowed_users=["anon-1a2b3c4d"])
    with pytest.raises(bridge.ConfigError, match="allowed_users"):
        bridge.BridgeConfig.load(write_config(home, bad))

    bad = config_dict(llm={"backend": "openai", "model": "m", "api_key": "sk-vazamento"})
    with pytest.raises(bridge.ConfigError, match="segredos"):
        bridge.BridgeConfig.load(write_config(home, bad))

    good = bridge.BridgeConfig.load(write_config(home, config_dict()))
    assert good.port == 7777 and good.allowed == {"alice"}
    assert good.chunk_chars == 1800


@pytest.mark.skipif(os.name != "posix", reason="permissões POSIX")
def test_config_requires_private_file(home):
    path = write_config(home, config_dict())
    os.chmod(path, 0o644)
    with pytest.raises(bridge.ConfigError, match="chmod 600"):
        bridge.BridgeConfig.load(path)


def test_openai_key_comes_from_env_name_only(home, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(bridge_llm.LLMError, match="OPENAI_API_KEY"):
        bridge_llm.build_backend({"backend": "openai", "model": "m"})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-teste")
    backend = bridge_llm.build_backend({"backend": "openai", "model": "m"})
    assert backend.api_key == "sk-teste"
    assert "sk-teste" not in json.dumps(config_dict())

    with pytest.raises(bridge_llm.LLMError, match="https"):
        bridge_llm.check_url("http://api.exemplo.invalid", "llm.base_url")


def test_llm_backends_post_to_expected_endpoints(monkeypatch):
    seen = {}

    async def fake(url, payload, headers=None, timeout=30):
        seen.update(url=url, payload=payload, headers=headers or {})
        if url.endswith("/api/chat"):
            return {"message": {"content": "resposta local"}}
        return {"choices": [{"message": {"content": "resposta api"}}]}

    monkeypatch.setattr(bridge_llm, "post_json", fake)
    ollama = bridge_llm.build_backend({"backend": "ollama", "model": "m"})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-teste")
    api = bridge_llm.build_backend({"backend": "openai", "model": "m",
                                    "api_key_env": "OPENAI_API_KEY"})
    assert asyncio.run(ollama.chat([{"role": "user", "content": "oi"}], 10)) == "resposta local"
    assert seen["url"] == "http://127.0.0.1:11434/api/chat"
    assert asyncio.run(api.chat([{"role": "user", "content": "oi"}], 10)) == "resposta api"
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer sk-teste"


def test_http_adapter_is_get_only_and_allowlisted(monkeypatch):
    seen = {}

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _size):
            return b'{"sensores":[{"t":21.5}]}'

    def fake(req, timeout=None, context=None):
        seen["method"] = req.get_method()
        seen["url"] = req.full_url
        return Reply()

    monkeypatch.setattr(bridge_ot.urllib.request, "urlopen", fake)
    adapter = bridge_ot.HTTPAdapter({"endpoints": {
        "temp": {"url": "https://clp.invalid/api", "field": "sensores.0.t"},
    }})
    assert asyncio.run(adapter.read("temp")) == "21.5"
    assert seen == {"method": "GET", "url": "https://clp.invalid/api"}
    with pytest.raises(bridge_ot.OTError, match="não permitida"):
        asyncio.run(adapter.read("outro"))


def test_modbus_adapter_only_reads_registers():
    seen = []

    async def handler(reader, writer):
        head = await reader.readexactly(7)
        txid, proto, length, unit = struct.unpack(">HHHB", head)
        body = await reader.readexactly(length - 1)
        function, address, count = struct.unpack(">BHH", body[:5])
        seen.append((function, address, count))
        answer = struct.pack(">BBH", function, 2, 1234)
        writer.write(struct.pack(">HHHB", txid, 0, len(answer) + 1, unit) + answer)
        await writer.drain()
        writer.close()

    async def main():
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            adapter = bridge_ot.ModbusTCPAdapter({"devices": {"clp": {
                "host": "127.0.0.1", "port": port, "unit": 1,
                "registers": {"temp": {"function": 4, "address": 100, "scale": 0.1}},
            }}})
            assert await adapter.read("clp.temp") == "123.4"
            assert seen == [(4, 100, 1)]
            with pytest.raises(bridge_ot.OTError, match="não permitido"):
                await adapter.read("clp.outro")
            with pytest.raises(bridge_ot.OTError, match="leitura"):
                bridge_ot.ModbusTCPAdapter({"devices": {"clp": {
                    "host": "127.0.0.1",
                    "registers": {"temp": {"function": 6, "address": 1}},
                }}})
        finally:
            server.close()
            await server.wait_closed()

    run(main())


def test_mqtt_adapter_only_subscribes():
    frames = []

    async def handler(reader, writer):
        header = await reader.readexactly(2)
        frames.append(header[0])
        size = await bridge_ot.MQTTAdapter._remaining(reader, header[1], 2)
        await reader.readexactly(size)
        writer.write(b"\x20\x02\x00\x00")
        await writer.drain()
        header = await reader.readexactly(2)
        frames.append(header[0])
        size = await bridge_ot.MQTTAdapter._remaining(reader, header[1], 2)
        await reader.readexactly(size)
        writer.write(b"\x90\x03\x00\x01\x00")
        topic = b"planta/1/temperatura"
        payload = b"23.5"
        body = struct.pack(">H", len(topic)) + topic + payload
        writer.write(b"\x30" + bridge_ot._varint(len(body)) + body)
        await writer.drain()
        await asyncio.sleep(0.3)

    async def main():
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        adapter = bridge_ot.MQTTAdapter({"brokers": {"local": {
            "host": "127.0.0.1", "port": port, "tls": False, "timeout_s": 2,
            "topics": ["planta/1/temperatura"],
        }}})
        await adapter.start(lambda message: None)
        try:
            await until(lambda: "planta/1/temperatura" in adapter.values)
            assert await adapter.read("planta/1/temperatura") == "23.5"
            assert frames[:2] == [0x10, 0x82]      # CONNECT e SUBSCRIBE, nunca PUBLISH
            with pytest.raises(bridge_ot.OTError, match="não permitido"):
                await adapter.read("planta/+/outro")
        finally:
            await adapter.close()
            server.close()
            await server.wait_closed()

    run(main())


def test_chunk_reply_stays_under_protocol_limit():
    parts = bridge.chunk_reply("a" * 4000, size=1000, limit=10000)
    assert len(parts) == 4
    assert all(len(part) <= 1006 for part in parts)
    assert "".join(part.split("] ", 1)[1] for part in parts) == "a" * 4000
    assert bridge.chunk_reply("") == []


def test_key_change_blocks_without_accepting(home):
    cfg = bridge.BridgeConfig.load(write_config(home, config_dict()))

    class DummyClient:
        def __init__(self):
            self.spawned = []

        def spawn(self, coro):
            self.spawned.append(coro)
            coro.close()

    service = bridge.BridgeService(cfg, identity=object(), llm_backend=FakeBackend("x"),
                                   adapters=bridge.OTAdapters(cfg.ot), log=lambda *a: None)
    service.client = DummyClient()
    service.on_event("key_changed", {"nick": "alice"})
    assert "alice" in service.blocked
    service.on_event("message", {"buffer": "@alice", "nick": "alice",
                                 "mine": False, "text": "oi"})
    service.on_event("message", {"buffer": "@outro", "nick": "outro",
                                 "mine": False, "text": "oi"})
    assert service.client.spawned == []


def test_bridge_dms_are_e2ee_and_long_replies_are_chunked(home):
    async def main():
        async with Harness(home) as h:
            alice = await h.client("alice")
            data = config_dict(server=f"127.0.0.1:{h.port}")
            cfg = bridge.BridgeConfig.load(write_config(home, data))
            ident = Identity.create("otbot", "pw")
            backend = FakeBackend("R" * 4000)
            service = bridge.BridgeService(cfg, identity=ident, llm_backend=backend,
                                           adapters=bridge.OTAdapters(cfg.ot),
                                           log=lambda *a: None)
            store = Store(f"127.0.0.1:{h.port}")
            bot = ChatClient("127.0.0.1", h.port, ident, store, service.on_event,
                             tls=False, download_dir=str(home / "dl"))
            service.client = bot
            task = asyncio.create_task(bot.run())
            h.clients.append((bot, task))
            await until(lambda: bot.online)

            await alice.send_dm("otbot", "prompt ultra secreto")
            await until(lambda: len(messages(alice)) >= 3)
            texts = [text for _nick, text in messages(alice)]
            assert len(texts) == 3
            assert texts[0].startswith("[1/3] ")
            assert "R" * 100 not in " ".join(h.wire)
            assert "prompt ultra secreto" not in " ".join(h.wire)
            assert backend.calls[-1][-1]["content"] == "prompt ultra secreto"

    run(main())

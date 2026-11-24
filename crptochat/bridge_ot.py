"""Adapters IT/OT da bridge: HTTP/JSON, Modbus TCP e MQTT.

Todos são **somente leitura**: GET, leitura de registradores e subscrição.
A bridge nunca publica em MQTT, nunca escreve registradores e nunca chama
endpoints de escrita. Nomes de métricas/tópicos são allowlists explícitas; o
config guarda apenas nomes de variáveis de ambiente para credenciais.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import ssl
import struct
import urllib.parse
import urllib.request

import certifi

MAX_VALUE = 4000
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ITEM_NAME = re.compile(r"^[A-Za-z0-9_.:/+@-]{1,80}$")


class OTError(Exception):
    """Falha de adapter (sem incluir respostas de equipamentos no log)."""


def _env(name: str, what: str) -> str:
    if not isinstance(name, str) or not ENV_NAME.match(name):
        raise OTError(f"{what}: nome de variável de ambiente inválido")
    value = os.environ.get(name)
    if not value:
        raise OTError(f"{what}: variável {name} não está definida")
    return value


def _url(value: str, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OTError(f"{what}: URL obrigatória")
    parts = urllib.parse.urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise OTError(f"{what}: use http:// ou https://")
    if parts.username is not None or parts.password is not None:
        raise OTError(f"{what}: a URL não pode conter credenciais")
    if parts.scheme == "http" and parts.hostname.lower() not in LOOPBACK:
        raise OTError(f"{what}: http:// só em loopback; use https://")
    return value.strip()


def _name(value: str) -> str:
    if not isinstance(value, str) or not ITEM_NAME.match(value):
        raise OTError("nome de métrica/tópico inválido")
    return value


def _timeout(value, default: float, what: str) -> float:
    number = default if value is None else value
    if isinstance(number, bool) or not isinstance(number, (int, float)) or not 0.1 <= number <= 30:
        raise OTError(f"{what}: timeout deve estar entre 0.1 e 30 segundos")
    return float(number)


def _show(value) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    if len(text) > MAX_VALUE:
        raise OTError("valor grande demais")
    return text


class Adapter:
    kind = "?"

    def names(self) -> list[str]:
        return []

    async def read(self, name: str) -> str:
        raise NotImplementedError

    async def start(self, on_error) -> None:
        pass

    async def close(self) -> None:
        pass


class HTTPAdapter(Adapter):
    """Endpoint HTTPS/JSON fixo; apenas GET e campos permitidos no config."""

    kind = "http"

    def __init__(self, config: dict):
        if not isinstance(config, dict):
            raise OTError("ot.http: objeto obrigatório")
        endpoints = config.get("endpoints") or {}
        if not isinstance(endpoints, dict) or not endpoints or len(endpoints) > 50:
            raise OTError("ot.http.endpoints: de 1 a 50 métricas")
        self.endpoints = {}
        for key, spec in endpoints.items():
            name = _name(key)
            if not isinstance(spec, dict):
                raise OTError(f"ot.http.{name}: objeto obrigatório")
            url = _url(spec.get("url"), f"ot.http.{name}.url")
            field = spec.get("field") or ""
            if field and not isinstance(field, str):
                raise OTError(f"ot.http.{name}.field: inválido")
            headers_env = spec.get("headers_env") or {}
            if not isinstance(headers_env, dict) or len(headers_env) > 10:
                raise OTError(f"ot.http.{name}.headers_env: inválido")
            for header, variable in headers_env.items():
                if not isinstance(header, str) or not re.match(r"^[A-Za-z0-9-]{1,40}$", header):
                    raise OTError(f"ot.http.{name}.headers_env: cabeçalho inválido")
                _env(variable, f"ot.http.{name}.headers_env.{header}")
            self.endpoints[name] = {
                "url": url, "field": field, "headers_env": dict(headers_env),
                "timeout": _timeout(spec.get("timeout_s"), 5.0, f"ot.http.{name}.timeout_s"),
            }

    def names(self) -> list[str]:
        return sorted(self.endpoints)

    async def read(self, name: str) -> str:
        spec = self.endpoints.get(_name(name))
        if spec is None:
            raise OTError(f"métrica não permitida: {name}")
        headers = {header: _env(variable, "ot.http.headers_env")
                   for header, variable in spec["headers_env"].items()}

        def get():
            req = urllib.request.Request(spec["url"], headers=headers, method="GET")
            ctx = ssl.create_default_context(cafile=certifi.where()) if spec["url"].startswith("https://") else None
            try:
                with urllib.request.urlopen(req, timeout=spec["timeout"], context=ctx) as reply:
                    raw = reply.read(MAX_VALUE + 1)
            except Exception as exc:
                if hasattr(exc, "code"):
                    raise OTError(f"HTTP {exc.code}") from None
                raise OTError(type(exc).__name__) from None
            if len(raw) > MAX_VALUE:
                raise OTError("resposta grande demais")
            try:
                return json.loads(raw)
            except ValueError:
                raise OTError("JSON inválido") from None

        data = await asyncio.to_thread(get)
        if spec["field"]:
            for part in spec["field"].split("."):
                try:
                    data = data[int(part)] if isinstance(data, list) else data[part]
                except (KeyError, IndexError, TypeError, ValueError):
                    raise OTError("campo ausente") from None
        return _show(data)


class ModbusTCPAdapter(Adapter):
    """Leitura de holding/input registers (FC 03/04). Nenhuma escrita."""

    kind = "modbus"

    def __init__(self, config: dict):
        if not isinstance(config, dict):
            raise OTError("ot.modbus: objeto obrigatório")
        devices = config.get("devices") or {}
        if not isinstance(devices, dict) or not devices or len(devices) > 20:
            raise OTError("ot.modbus.devices: de 1 a 20 devices")
        self.registers: dict[str, tuple[dict, dict]] = {}
        for device_name, spec in devices.items():
            dev = _name(device_name)
            if not isinstance(spec, dict):
                raise OTError(f"ot.modbus.{dev}: objeto obrigatório")
            host = spec.get("host")
            if not isinstance(host, str) or not host.strip() or ":" in host:
                raise OTError(f"ot.modbus.{dev}.host: inválido")
            port = spec.get("port", 502)
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                raise OTError(f"ot.modbus.{dev}.port: inválido")
            unit = spec.get("unit", 1)
            if not isinstance(unit, int) or isinstance(unit, bool) or not 1 <= unit <= 247:
                raise OTError(f"ot.modbus.{dev}.unit: inválido")
            regs = spec.get("registers") or {}
            if not isinstance(regs, dict) or not regs or len(regs) > 50:
                raise OTError(f"ot.modbus.{dev}.registers: de 1 a 50 registros")
            common = {"host": host.strip(), "port": port, "unit": unit,
                      "timeout": _timeout(spec.get("timeout_s"), 3.0, f"ot.modbus.{dev}.timeout_s")}
            for key, reg in regs.items():
                metric = f"{dev}.{_name(key)}"
                if not isinstance(reg, dict):
                    raise OTError(f"ot.modbus.{metric}: objeto obrigatório")
                function = reg.get("function", 3)
                address, count = reg.get("address"), reg.get("count", 1)
                if function not in (3, 4):
                    raise OTError(f"ot.modbus.{metric}: só leitura (function 3 ou 4)")
                if not isinstance(address, int) or isinstance(address, bool) or not 0 <= address <= 65535:
                    raise OTError(f"ot.modbus.{metric}.address: inválido")
                if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 125:
                    raise OTError(f"ot.modbus.{metric}.count: inválido")
                scale = reg.get("scale", 1)
                if isinstance(scale, bool) or not isinstance(scale, (int, float)) or scale <= 0:
                    raise OTError(f"ot.modbus.{metric}.scale: inválido")
                self.registers[metric] = (common, {"function": function, "address": address,
                                                   "count": count, "scale": float(scale)})

    def names(self) -> list[str]:
        return sorted(self.registers)

    async def read(self, name: str) -> str:
        entry = self.registers.get(_name(name))
        if entry is None:
            raise OTError(f"registro não permitido: {name}")
        dev, reg = entry
        txid = int.from_bytes(os.urandom(2), "big")
        pdu = struct.pack(">BHH", reg["function"], reg["address"], reg["count"])
        frame = struct.pack(">HHHB", txid, 0, len(pdu) + 1, dev["unit"]) + pdu
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(dev["host"], dev["port"]), dev["timeout"])
            try:
                writer.write(frame)
                await asyncio.wait_for(writer.drain(), dev["timeout"])
                head = await asyncio.wait_for(reader.readexactly(7), dev["timeout"])
                answer_tx, proto, length, unit = struct.unpack(">HHHB", head)
                if answer_tx != txid or proto != 0 or unit != dev["unit"] or not 3 <= length <= 258:
                    raise OTError("resposta Modbus inválida")
                body = await asyncio.wait_for(reader.readexactly(length - 1), dev["timeout"])
                if (len(body) < 2 or body[0] != reg["function"] or
                        body[1] != reg["count"] * 2 or len(body) != 2 + body[1]):
                    raise OTError("resposta Modbus inválida")
                values = struct.unpack(">" + "H" * reg["count"], body[2:])
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
        except OTError:
            raise
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            raise OTError("falha de comunicação Modbus") from None
        result = [round(v * reg["scale"], 6) for v in values]
        return _show(result[0] if len(result) == 1 else result)


def _str(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value % 128
        value //= 128
        out.append(byte | (128 if value else 0))
        if not value:
            return bytes(out)


class MQTTAdapter(Adapter):
    """Subscrição MQTT 3.1.1; último valor por tópico em memória. Sem PUBLISH."""

    kind = "mqtt"

    def __init__(self, config: dict):
        if not isinstance(config, dict):
            raise OTError("ot.mqtt: objeto obrigatório")
        brokers = config.get("brokers") or {}
        if not isinstance(brokers, dict) or not brokers or len(brokers) > 10:
            raise OTError("ot.mqtt.brokers: de 1 a 10 brokers")
        self.brokers = []
        self.topics: set[str] = set()
        self.values: dict[str, str] = {}
        self.tasks: list[asyncio.Task] = []
        for key, spec in brokers.items():
            name = _name(key)
            if not isinstance(spec, dict):
                raise OTError(f"ot.mqtt.{name}: objeto obrigatório")
            host, port = spec.get("host"), spec.get("port", 8883)
            if not isinstance(host, str) or not host.strip() or ":" in host:
                raise OTError(f"ot.mqtt.{name}.host: inválido")
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                raise OTError(f"ot.mqtt.{name}.port: inválido")
            topics = spec.get("topics") or []
            if not isinstance(topics, list) or not topics or len(topics) > 50:
                raise OTError(f"ot.mqtt.{name}.topics: de 1 a 50 tópicos")
            topics = [_name(t) for t in topics]
            self.topics.update(topics)
            user = password = None
            if spec.get("username_env"):
                user = _env(spec["username_env"], f"ot.mqtt.{name}.username_env")
            if spec.get("password_env"):
                password = _env(spec["password_env"], f"ot.mqtt.{name}.password_env")
            self.brokers.append({"host": host.strip(), "port": port, "topics": topics,
                                 "tls": bool(spec.get("tls", True)), "user": user, "pass": password,
                                 "timeout": _timeout(spec.get("timeout_s"), 5.0,
                                                     f"ot.mqtt.{name}.timeout_s")})

    def names(self) -> list[str]:
        return sorted(self.topics)

    async def read(self, name: str) -> str:
        topic = _name(name)
        if topic not in self.topics:
            raise OTError(f"tópico não permitido: {topic}")
        if topic not in self.values:
            raise OTError("tópico ainda sem valor")
        return self.values[topic]

    async def start(self, on_error) -> None:
        for broker in self.brokers:
            self.tasks.append(asyncio.create_task(self._listen(broker, on_error)))

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        self.tasks.clear()

    @staticmethod
    def _connect(broker: dict) -> bytes:
        flags = 0x02 | (0x80 if broker["user"] else 0) | (0x40 if broker["pass"] else 0)
        variable = _str("MQTT") + bytes([4, flags]) + struct.pack(">H", 60)
        payload = _str("crptochat-bridge-" + os.urandom(4).hex())
        if broker["user"]:
            payload += _str(broker["user"])
        if broker["pass"]:
            payload += _str(broker["pass"])
        body = variable + payload
        return b"\x10" + _varint(len(body)) + body

    @staticmethod
    def _subscribe(broker: dict) -> bytes:
        body = struct.pack(">H", 1)
        for topic in broker["topics"]:
            body += _str(topic) + b"\x00"      # QoS 0
        return b"\x82" + _varint(len(body)) + body

    async def _listen(self, broker: dict, on_error) -> None:
        try:
            ctx = ssl.create_default_context(cafile=certifi.where()) if broker["tls"] else None
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(broker["host"], broker["port"], ssl=ctx), broker["timeout"])
            try:
                writer.write(self._connect(broker))
                await asyncio.wait_for(writer.drain(), broker["timeout"])
                connack = await asyncio.wait_for(reader.readexactly(4), broker["timeout"])
                if connack[0] != 0x20 or connack[3] != 0:
                    raise OTError("CONNACK recusado")
                writer.write(self._subscribe(broker))
                await asyncio.wait_for(writer.drain(), broker["timeout"])
                head = await asyncio.wait_for(reader.readexactly(2), broker["timeout"])
                size = await self._remaining(reader, head[1], broker["timeout"])
                await asyncio.wait_for(reader.readexactly(size), broker["timeout"])  # SUBACK
                while True:
                    head = await asyncio.wait_for(reader.readexactly(2), broker["timeout"])
                    kind = head[0] >> 4
                    size = await self._remaining(reader, head[1], broker["timeout"])
                    body = await asyncio.wait_for(reader.readexactly(size), broker["timeout"])
                    if kind == 3:                       # PUBLISH recebido
                        topic_len = struct.unpack(">H", body[:2])[0]
                        topic = body[2:2 + topic_len].decode("utf-8", "replace")
                        pos = 2 + topic_len
                        if head[0] & 0x06:              # QoS 1/2: packet id
                            pos += 2
                        if topic in self.topics:
                            self.values[topic] = body[pos:].decode("utf-8", "replace")[:MAX_VALUE]
                    # PINGRESP e outros são ignorados; a bridge não publica.
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            safe = "MQTT: " + (str(exc) if isinstance(exc, OTError) else type(exc).__name__)
            try:
                on_error(safe)
            except Exception:
                pass

    @staticmethod
    async def _remaining(reader, first: int, timeout: float) -> int:
        multiplier, value, byte = 1, first, first
        while byte & 128:
            if multiplier > 128 * 128 * 128:
                raise OTError("pacote MQTT inválido")
            byte = (await asyncio.wait_for(reader.readexactly(1), timeout))[0]
            value += (byte & 127) * multiplier
            multiplier *= 128
        return value


class OTAdapters:
    """Conjunto de adapters; nomes ficam prefixados com o tipo."""

    def __init__(self, config: dict | None = None):
        config = config or {}
        if not isinstance(config, dict):
            raise OTError("ot: objeto obrigatório")
        unknown = set(config) - {"http", "modbus", "mqtt"}
        if unknown:
            raise OTError("ot: seções desconhecidas: " + ", ".join(sorted(unknown)))
        self.http = HTTPAdapter(config["http"]) if config.get("http") else None
        self.modbus = ModbusTCPAdapter(config["modbus"]) if config.get("modbus") else None
        self.mqtt = MQTTAdapter(config["mqtt"]) if config.get("mqtt") else None
        if not (self.http or self.modbus or self.mqtt):
            raise OTError("ot: configure ao menos um adapter (http, modbus ou mqtt)")

    def names(self) -> list[str]:
        out = []
        for adapter in (self.http, self.modbus, self.mqtt):
            if adapter:
                out.extend(f"{adapter.kind}:{name}" for name in adapter.names())
        return sorted(out)

    async def read(self, reference: str) -> str:
        if ":" not in reference:
            raise OTError("use tipo:nome (http:, modbus: ou mqtt:)")
        kind, name = reference.split(":", 1)
        adapter = {"http": self.http, "modbus": self.modbus, "mqtt": self.mqtt}.get(kind)
        if adapter is None:
            raise OTError("adapter não configurado: " + kind)
        return await adapter.read(name)

    async def start(self, on_error) -> None:
        if self.mqtt:
            await self.mqtt.start(on_error)

    async def close(self) -> None:
        if self.mqtt:
            await self.mqtt.close()

    async def context(self, limit: int = 10, per_value: int = 200) -> list[str]:
        out = []
        for reference in self.names()[:limit]:
            try:
                value = await asyncio.wait_for(self.read(reference), 5)
                out.append(f"{reference}: {value[:per_value]}")
            except (OTError, asyncio.TimeoutError):
                continue
        return out

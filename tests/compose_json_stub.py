"""`docker compose config --format json` for the deploy tests, without PyYAML.

A small indentation parser for the YAML subset of the two compose files the VM
uses (block mappings, lists of scalars, flow lists, comments), then the shape
compose prints: networks/volumes with `name` (and `external`), service networks
as a mapping, ports and volumes in long form. Knob: COMPOSE_CONFIG_DROP=<key of
the server service> leaves that key out (a template that lost it).
"""

import json
import os
import sys


def _lines(text):
    out = []
    for raw in text.splitlines():
        if raw.lstrip().startswith("#"):
            continue
        s = raw.split(" #", 1)[0].rstrip()
        if s.strip():
            out.append((len(s) - len(s.lstrip()), s.strip()))
    return out


def _scalar(v):
    v = v.strip()
    if v in ("true", "false"):
        return v == "true"
    if v == "{}":
        return {}
    if v.startswith("[") and v.endswith("]"):
        return [_scalar(x) for x in v[1:-1].split(",") if x.strip()]
    if v.startswith("{") and v.endswith("}"):
        return {k.strip(): {} for k in v[1:-1].split(",") if k.strip()}
    return v.strip("\"'")


def parse(text):
    lines = _lines(text)
    pos = 0

    def block(ind):
        nonlocal pos
        if lines[pos][1].startswith("- "):
            items = []
            while pos < len(lines) and lines[pos][0] == ind and lines[pos][1].startswith("- "):
                items.append(_scalar(lines[pos][1][2:]))
                pos += 1
            return items
        out = {}
        while pos < len(lines) and lines[pos][0] == ind and not lines[pos][1].startswith("- "):
            key, _, value = lines[pos][1].partition(":")
            key = key.strip().strip("\"'")
            pos += 1
            if value.strip():
                out[key] = _scalar(value)
            elif pos < len(lines) and (lines[pos][0] > ind or (lines[pos][0] == ind and lines[pos][1].startswith("- "))):
                out[key] = block(lines[pos][0])
            else:
                out[key] = None
        return out

    return block(lines[0][0]) if lines else {}


def config(doc, project):
    def top(kind):
        out = {}
        for key, opts in (doc.get(kind) or {}).items():
            opts = dict(opts or {})
            opts.setdefault("name", key if opts.get("external") else f"{project}_{key}")
            out[key] = opts
        return out

    services = {}
    for name, svc in (doc.get("services") or {}).items():
        svc = dict(svc)
        nets = svc.get("networks")
        svc["networks"] = {n: None for n in nets} if isinstance(nets, list) else (nets or {"default": None})
        ports = []
        for p in svc.get("ports") or []:
            published, _, target = str(p).rpartition(":")
            ports.append({"mode": "ingress", "target": int(target), "published": published.split(":")[-1],
                          "protocol": "tcp"})
        svc["ports"] = ports
        vols = []
        for v in svc.get("volumes") or []:
            if isinstance(v, str):
                src, _, rest = v.partition(":")
                vols.append({"type": "bind" if src.startswith("/") else "volume", "source": src,
                             "target": rest.split(":")[0]})
        svc["volumes"] = vols
        services[name] = svc
    drop = os.environ.get("COMPOSE_CONFIG_DROP")
    if drop and "server" in services:
        services["server"].pop(drop, None)
    return {"name": project, "services": services, "networks": top("networks"), "volumes": top("volumes")}


if __name__ == "__main__":
    path, project = sys.argv[1], sys.argv[2]
    print(json.dumps(config(parse(open(path, encoding="utf-8").read()), project)))

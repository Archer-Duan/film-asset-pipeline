"""ComfyUI transport and scoped conversion of saved UI workflows to API graphs."""

from __future__ import annotations

import copy
import json
import mimetypes
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class ComfyClient:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def request(self, route, payload=None, *, data=None, headers=None):
        if payload is not None:
            data = json.dumps(payload).encode()
            headers = {"Content-Type": "application/json"}
        try:
            with urlopen(
                Request(self.url + route, data=data, headers=headers or {}), timeout=30
            ) as response:
                return json.load(response)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1500]
            raise ValueError(f"ComfyUI HTTP {exc.code}: {detail}") from exc

    def upload(self, path: Path):
        boundary = uuid.uuid4().hex
        name = uuid.uuid4().hex + path.suffix.lower()
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode() + path.read_bytes()
        body += f"\r\n--{boundary}--\r\n".encode()
        result = self.request(
            "/upload/image",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        return "/".join(filter(None, [result.get("subfolder"), result["name"]]))

    def download(self, file):
        query = urlencode(
            {k: file.get(k, "") for k in ("filename", "subfolder", "type")}
        )
        with urlopen(self.url + "/view?" + query, timeout=120) as response:
            return response.read()


def compile_workflow(workflow: dict, schema: dict, targets: list[int], overrides: dict):
    """Resolve only target dependencies; inline subgraphs and constant switches.

    The source JSON is immutable. Overrides are keyed by (outer node id, input name).
    Named saved widgets take precedence; positional widgets support modern exports.
    Unsupported/missing inputs fail closed instead of silently changing the graph.
    """
    if "nodes" not in workflow:
        return compile_api_workflow(workflow, schema, targets, overrides)
    definitions = {
        g["id"]: g for g in workflow.get("definitions", {}).get("subgraphs", [])
    }
    result, cache, visiting = {}, {}, set()
    missing = object()
    used_overrides = set()

    def context(graph, prefix="", boundary=None):
        links = {}
        for link in graph.get("links", []):
            if isinstance(link, dict):
                links[link["id"]] = (link["origin_id"], link["origin_slot"])
            else:
                links[link[0]] = (link[1], link[2])
        return ({n["id"]: n for n in graph["nodes"]}, links, prefix, boundary or {})

    def widgets(node):
        values = copy.deepcopy(node.get("widgets_values_named", {}))
        raw = iter(node.get("widgets_values") or [])
        for inp in node.get("inputs", []):
            if not inp.get("widget"):
                continue
            value = next(raw, missing)
            if value is not missing:
                values.setdefault(inp["name"], value)
            if inp["name"] in ("seed", "noise_seed") and node["type"] == "KSampler":
                next(raw, None)  # frontend's control_after_generate, not an API input
        return values

    def inputs(ctx, node):
        nodes, links, prefix, boundary = ctx
        values = widgets(node)
        for inp in node.get("inputs", []):
            name = inp["name"]
            if not prefix and (node["id"], name) in overrides:
                values[name] = overrides[(node["id"], name)]
                used_overrides.add((node["id"], name))
            elif inp.get("link") is not None:
                origin, slot = links[inp["link"]]
                value = (
                    boundary.get(slot, missing)
                    if origin == -10
                    else resolve(ctx, origin, slot)
                )
                if value is missing:
                    values.pop(name, None)
                else:
                    values[name] = value
        return values

    def resolve(ctx, nid, slot=0):
        nodes, links, prefix, boundary = ctx
        key = (prefix, nid, slot)
        if key in cache:
            return cache[key]
        if key in visiting:
            raise ValueError("工作流包含循环连接")
        visiting.add(key)
        node = nodes[nid]
        kind = node["type"]
        if node.get("mode", 0) != 0:
            raise ValueError(f"输出依赖了旁路或禁用节点 {nid}，请检查工作流连接")
        vals = inputs(ctx, node)
        if kind in definitions:
            graph = definitions[kind]
            bound = {
                i: vals.get(item["name"], missing)
                for i, item in enumerate(graph["inputs"])
            }
            child = context(graph, f"{prefix}{nid}_", bound)
            output_link = graph["outputs"][slot]["linkIds"][0]
            src, out = child[1][output_link]
            value = resolve(child, src, out)
        elif kind == "Reroute":
            value = next(iter(vals.values()))
        elif kind.startswith("Primitive") and "value" in vals:
            value = vals["value"]
        elif kind == "ComfySwitchNode":
            switch = vals.get("switch")
            if not isinstance(switch, bool):
                raise ValueError("工作流开关必须为已知布尔值")
            value = vals.get("on_true" if switch else "on_false", missing)
        else:
            if kind not in schema:
                raise ValueError(f"ComfyUI 缺少节点：{kind}")
            spec = schema[kind]["input"]
            allowed = {**spec.get("required", {}), **spec.get("optional", {})}
            if not prefix:
                for override_id, name in overrides:
                    if override_id == nid and name not in allowed:
                        raise ValueError(f"节点 {nid} 不接受参数 {name}")
            vals = {
                k: v
                for k, v in vals.items()
                if k in allowed or k.split(".")[0] in allowed
            }
            for name in spec.get("required", {}):
                if name not in vals:
                    raise ValueError(f"节点 {nid} ({kind}) 缺少参数 {name}")
            api_id = f"{prefix}{nid}"
            result[api_id] = {"class_type": kind, "inputs": vals}
            value = [api_id, slot]
        visiting.remove(key)
        cache[key] = value
        return value

    root = context(workflow)
    for target in targets:
        resolve(root, target)
    if set(overrides) - used_overrides:
        raise ValueError("输入映射未连接到所选输出节点")
    # Constant branches were evaluated above; remove their unused dependencies.
    needed = set()

    def walk(nid):
        if nid in needed:
            return
        needed.add(nid)
        for value in result[nid]["inputs"].values():
            if (
                isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], str)
                and value[0] in result
            ):
                walk(value[0])

    for target in targets:
        walk(str(target))
    return {key: val for key, val in result.items() if key in needed}


def validate_models(graph, schema):
    errors = []
    for node in graph.values():
        spec = schema[node["class_type"]]["input"]
        for name, definition in {
            **spec.get("required", {}),
            **spec.get("optional", {}),
        }.items():
            value = node["inputs"].get(name)
            if (
                name.endswith("_name")
                and isinstance(value, str)
                and isinstance(definition[0], list)
                and value not in definition[0]
            ):
                errors.append(f"缺少模型：{value}")
    return sorted(set(errors))


def compile_api_workflow(workflow, schema, targets, overrides):
    graph = copy.deepcopy(workflow)
    for (node_id, name), value in overrides.items():
        node_id = str(node_id)
        if node_id not in graph or name not in graph[node_id].get("inputs", {}):
            raise ValueError(f"输入映射不存在：{node_id}.{name}")
        graph[node_id]["inputs"][name] = value
    result, visiting = {}, set()

    def walk(nid):
        nid = str(nid)
        if nid in visiting:
            raise ValueError("工作流包含循环连接")
        if nid in result:
            return
        if nid not in graph:
            raise ValueError(f"工作流缺少节点 {nid}")
        visiting.add(nid)
        node = graph[nid]
        kind = node.get("class_type")
        if kind not in schema:
            raise ValueError(f"ComfyUI 缺少节点：{kind}")
        if not isinstance(node.get("inputs"), dict):
            raise ValueError(f"节点 {nid} 缺少 inputs")
        spec = schema[kind]["input"]
        allowed = {**spec.get("required", {}), **spec.get("optional", {})}
        for node_id, name in overrides:
            if str(node_id) == nid and name not in allowed:
                raise ValueError(f"节点 {nid} 不接受参数 {name}")
        for name in spec.get("required", {}):
            if name not in node["inputs"]:
                raise ValueError(f"节点 {nid} 缺少参数 {name}")
        for value in node["inputs"].values():
            if (
                isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], str)
                and type(value[1]) is int
            ):
                walk(value[0])
        visiting.remove(nid)
        result[nid] = {"class_type": kind, "inputs": node["inputs"]}

    for target in targets:
        walk(target)
    if any(str(nid) not in result for nid, _ in overrides):
        raise ValueError("输入映射未连接到所选输出节点")
    return result

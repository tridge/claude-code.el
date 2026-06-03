"""Generic MCP proxy with a runtime HTTP control plane.

Speaks MCP over stdio to a single client (Claude), and acts as an MCP
client to one or more upstream stdio MCP servers defined in a TOML
config file. Aggregates upstream tools under namespaced names and
forwards calls. A localhost HTTP control plane lets callers toggle the
proxy globally or per-upstream without restarting Claude.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tomllib
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import web
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import EmbeddedResource, ImageContent, TextContent, Tool

NAME_SEP = "__"
log = logging.getLogger("mcp_proxy")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


ALLOW = Decision(allowed=True)


class Policy:
    """Per-upstream call policy. Override `inspect_call` to enforce rules."""

    def inspect_call(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> Decision:
        return ALLOW

    def state(self) -> dict[str, Any]:
        return {"type": "none"}


@dataclass
class GdriveWritePolicy(Policy):
    """Restrict write tools to a runtime-mutable folder allowlist.

    A call is allowed if its tool name is not in `write_tools`, or if the
    tool is in `write_tools` and every parent-folder argument value is in
    `writable_folders`. If a write tool is invoked with no recognized
    parent argument, behavior is governed by `no_parent_action`.
    """

    write_tools: set[str]
    parent_keys: list[str]
    writable_folders: set[str]
    no_parent_action: str = "deny"
    block_tools: set[str] = field(default_factory=set)

    def inspect_call(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> Decision:
        if tool_name in self.block_tools:
            return Decision(
                False,
                f"Tool {tool_name!r} is unconditionally blocked by proxy policy.",
            )
        if tool_name not in self.write_tools:
            return ALLOW
        parents = self._extract_parents(arguments)
        if parents is None:
            if self.no_parent_action == "allow":
                return ALLOW
            return Decision(
                False,
                f"Write tool {tool_name!r} blocked by proxy policy: "
                f"no parent-folder argument (looked for {self.parent_keys}). "
                f"Pass an explicit parent in the writable allowlist.",
            )
        if not parents:
            return Decision(
                False,
                f"Write tool {tool_name!r} blocked by proxy policy: "
                f"empty parents list.",
            )
        bad = [p for p in parents if p not in self.writable_folders]
        if bad:
            return Decision(
                False,
                f"Write tool {tool_name!r} blocked by proxy policy: "
                f"parent folder(s) {bad} not in writable allowlist "
                f"{sorted(self.writable_folders)}.",
            )
        return ALLOW

    def _extract_parents(
        self, arguments: dict[str, Any]
    ) -> list[str] | None:
        for key in self.parent_keys:
            if key in arguments:
                v = arguments[key]
                if isinstance(v, list):
                    return [str(x) for x in v]
                if isinstance(v, str):
                    return [v]
                return [str(v)]
        return None

    def state(self) -> dict[str, Any]:
        return {
            "type": "gdrive_writes",
            "write_tools": sorted(self.write_tools),
            "parent_keys": list(self.parent_keys),
            "writable_folders": sorted(self.writable_folders),
            "no_parent_action": self.no_parent_action,
            "block_tools": sorted(self.block_tools),
        }


def _build_gdrive_policy(c: dict[str, Any]) -> GdriveWritePolicy:
    return GdriveWritePolicy(
        write_tools=set(c.get("write_tools", [])),
        parent_keys=list(c.get("parent_keys", ["parents"])),
        writable_folders=set(c.get("writable_folders", [])),
        no_parent_action=c.get("no_parent_action", "deny"),
        block_tools=set(c.get("block_tools", [])),
    )


POLICY_BUILDERS = {"gdrive_writes": _build_gdrive_policy}


@dataclass
class UpstreamConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    policy: Policy = field(default_factory=Policy)


@dataclass
class ProxyConfig:
    upstreams: list[UpstreamConfig]
    control_host: str = "127.0.0.1"
    control_port: int = 8765

    @classmethod
    def load(cls, path: Path) -> ProxyConfig:
        raw = tomllib.loads(path.read_text())
        upstreams: list[UpstreamConfig] = []
        for u in raw.get("upstream", []):
            name = u["name"]
            if NAME_SEP in name:
                raise ValueError(
                    f"upstream name {name!r} must not contain {NAME_SEP!r}"
                )
            policy: Policy = Policy()
            if "policy" in u:
                if len(u["policy"]) != 1:
                    raise ValueError(
                        f"upstream {name!r}: exactly one policy type expected"
                    )
                ptype, pcfg = next(iter(u["policy"].items()))
                if ptype not in POLICY_BUILDERS:
                    raise ValueError(
                        f"upstream {name!r}: unknown policy type {ptype!r}"
                    )
                policy = POLICY_BUILDERS[ptype](pcfg)
            upstreams.append(
                UpstreamConfig(
                    name=name,
                    command=u["command"],
                    args=list(u.get("args", [])),
                    env=dict(u.get("env", {})),
                    policy=policy,
                )
            )
        control = raw.get("control", {})
        return cls(
            upstreams=upstreams,
            control_host=control.get("host", "127.0.0.1"),
            control_port=int(control.get("port", 8765)),
        )


@dataclass
class Upstream:
    config: UpstreamConfig
    session: ClientSession
    tools: list[Tool] = field(default_factory=list)
    enabled: bool = True


class Proxy:
    def __init__(self, config: ProxyConfig) -> None:
        self.config = config
        self.enabled = True
        self.upstreams: dict[str, Upstream] = {}
        self._stack = AsyncExitStack()

    async def start_upstreams(self) -> None:
        for u in self.config.upstreams:
            try:
                params = StdioServerParameters(
                    command=u.command,
                    args=u.args,
                    # Merge with parent env so PATH/HOME etc. propagate.
                    env={**os.environ, **u.env},
                )
                read, write = await self._stack.enter_async_context(
                    stdio_client(params)
                )
                session = await self._stack.enter_async_context(
                    ClientSession(read, write)
                )
                await session.initialize()
                tools = (await session.list_tools()).tools
                self.upstreams[u.name] = Upstream(
                    config=u, session=session, tools=tools
                )
                log.info("upstream %s ready (%d tools)", u.name, len(tools))
            except Exception:
                log.exception("upstream %s failed to start", u.name)

    async def aclose(self) -> None:
        await self._stack.aclose()

    def visible_tools(self) -> list[Tool]:
        if not self.enabled:
            return []
        out: list[Tool] = []
        for name, up in self.upstreams.items():
            if not up.enabled:
                continue
            for t in up.tools:
                out.append(
                    Tool(
                        name=f"{name}{NAME_SEP}{t.name}",
                        description=t.description,
                        inputSchema=t.inputSchema,
                    )
                )
        return out

    async def call_tool(
        self, qualified: str, arguments: dict[str, Any]
    ) -> list[TextContent | ImageContent | EmbeddedResource]:
        if not self.enabled:
            return [
                TextContent(
                    type="text",
                    text="MCP access is currently disabled by the local proxy.",
                )
            ]
        if NAME_SEP not in qualified:
            return [
                TextContent(
                    type="text",
                    text=f"Malformed tool name {qualified!r}: missing {NAME_SEP!r} prefix.",
                )
            ]
        server_name, tool_name = qualified.split(NAME_SEP, 1)
        up = self.upstreams.get(server_name)
        if up is None:
            return [
                TextContent(
                    type="text", text=f"Unknown upstream {server_name!r}."
                )
            ]
        if not up.enabled:
            return [
                TextContent(
                    type="text",
                    text=f"Upstream {server_name!r} is disabled by the local proxy.",
                )
            ]
        decision = up.config.policy.inspect_call(tool_name, arguments)
        if not decision.allowed:
            return [TextContent(type="text", text=decision.reason)]
        result = await up.session.call_tool(tool_name, arguments)
        return result.content


def build_server(proxy: Proxy) -> Server:
    server: Server = Server("mcp-proxy")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return proxy.visible_tools()

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any]
    ) -> list[TextContent | ImageContent | EmbeddedResource]:
        return await proxy.call_tool(name, arguments or {})

    return server


async def start_control_plane(proxy: Proxy) -> web.AppRunner:
    routes = web.RouteTableDef()

    @routes.get("/status")
    async def _status(_req: web.Request) -> web.Response:
        return web.json_response(
            {
                "enabled": proxy.enabled,
                "upstreams": {
                    name: {
                        "enabled": up.enabled,
                        "tools": [t.name for t in up.tools],
                        "policy": up.config.policy.state(),
                    }
                    for name, up in proxy.upstreams.items()
                },
            }
        )

    @routes.post("/enabled")
    async def _set_enabled(req: web.Request) -> web.Response:
        body = await req.json()
        proxy.enabled = bool(body["enabled"])
        log.info("global enabled -> %s", proxy.enabled)
        return web.json_response({"enabled": proxy.enabled})

    @routes.post("/servers/{name}/enabled")
    async def _set_server_enabled(req: web.Request) -> web.Response:
        name = req.match_info["name"]
        up = proxy.upstreams.get(name)
        if up is None:
            return web.json_response(
                {"error": f"unknown upstream {name!r}"}, status=404
            )
        body = await req.json()
        up.enabled = bool(body["enabled"])
        log.info("upstream %s enabled -> %s", name, up.enabled)
        return web.json_response({"name": name, "enabled": up.enabled})

    def _require_gdrive_policy(name: str):
        up = proxy.upstreams.get(name)
        if up is None:
            return None, web.json_response(
                {"error": f"unknown upstream {name!r}"}, status=404
            )
        policy = up.config.policy
        if not isinstance(policy, GdriveWritePolicy):
            return None, web.json_response(
                {"error": f"upstream {name!r} has no gdrive_writes policy"},
                status=400,
            )
        return policy, None

    @routes.get("/servers/{name}/writable-folders")
    async def _get_writable_folders(req: web.Request) -> web.Response:
        policy, err = _require_gdrive_policy(req.match_info["name"])
        if err:
            return err
        return web.json_response({"folders": sorted(policy.writable_folders)})

    @routes.put("/servers/{name}/writable-folders")
    async def _set_writable_folders(req: web.Request) -> web.Response:
        name = req.match_info["name"]
        policy, err = _require_gdrive_policy(name)
        if err:
            return err
        body = await req.json()
        folders = body.get("folders", [])
        if not isinstance(folders, list):
            return web.json_response(
                {"error": "folders must be a list"}, status=400
            )
        policy.writable_folders = {str(f) for f in folders}
        log.info(
            "upstream %s writable_folders -> %s",
            name,
            sorted(policy.writable_folders),
        )
        return web.json_response({"folders": sorted(policy.writable_folders)})

    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, proxy.config.control_host, proxy.config.control_port)
    await site.start()
    log.info(
        "control plane on http://%s:%d",
        proxy.config.control_host,
        proxy.config.control_port,
    )
    return runner


async def main_async(config_path: Path) -> None:
    # All logging must go to stderr — stdout is the MCP transport.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    config = ProxyConfig.load(config_path)
    proxy = Proxy(config)
    await proxy.start_upstreams()
    runner = await start_control_plane(proxy)
    server = build_server(proxy)
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await runner.cleanup()
        await proxy.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generic MCP proxy.")
    parser.add_argument(
        "--config", "-c", type=Path, required=True, help="Path to TOML config file."
    )
    args = parser.parse_args()
    asyncio.run(main_async(args.config))


if __name__ == "__main__":
    main()

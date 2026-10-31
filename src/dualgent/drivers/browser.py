from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from fastapi import WebSocket

from ..contracts import Action, InteractionError, Observation, Receipt, Surface, identity


@dataclass
class Connection:
    socket: WebSocket
    name: str
    pending: dict[str, asyncio.Future] = field(default_factory=dict)


class BrowserBridge:
    """One authenticated extension connection per browser profile. No MCP or raw CDP."""

    def __init__(self):
        self.connections: dict[str, Connection] = {}

    async def serve(self, socket: WebSocket, instance: str, name: str):
        previous = self.connections.get(instance)
        if previous:
            await previous.socket.close(code=4001, reason="profile reconnected")
        connection = Connection(socket, name)
        self.connections[instance] = connection
        try:
            await socket.send_json({"type": "connected"})
            while True:
                message = await socket.receive_json()
                if message.get("type") == "ping":
                    await socket.send_json({"type": "pong"})
                    continue
                future = connection.pending.get(message.get("id"))
                if future and not future.done():
                    future.set_result(message)
        finally:
            if self.connections.get(instance) is connection:
                self.connections.pop(instance)
            for future in connection.pending.values():
                if not future.done():
                    future.set_exception(InteractionError("浏览器扩展已断开", unknown=True))

    async def call(self, instance: str, command: str, arguments: dict, *, timeout: float = 20):
        connection = self.connections.get(instance)
        if connection is None:
            raise InteractionError("浏览器扩展未连接，请在扩展设置中重新配对。")
        request_id = identity()
        future = asyncio.get_running_loop().create_future()
        connection.pending[request_id] = future
        try:
            await connection.socket.send_json(
                {"id": request_id, "command": command, "arguments": arguments}
            )
            response = await asyncio.wait_for(future, timeout)
            if "error" in response:
                error = response["error"]
                raise InteractionError(error["detail"], unknown=error.get("unknown", False))
            return response["result"]
        except TimeoutError as error:
            # Closing the connection fences delayed commands; the extension detaches all claims.
            await connection.socket.close(code=4002, reason="command timeout")
            raise InteractionError(
                "浏览器命令超时，结果可能未知；请检查页面。", unknown=True
            ) from error
        except asyncio.CancelledError:
            await connection.socket.close(code=4002, reason="command interrupted")
            raise
        finally:
            connection.pending.pop(request_id, None)


class BrowserDriver:
    def __init__(self, bridge: BrowserBridge):
        self.bridge = bridge

    @staticmethod
    def address(surface: Surface) -> tuple[str, int]:
        _, instance, tab = surface.id.split(":")
        return instance, int(tab)

    async def discover(self) -> list[Surface]:
        surfaces = []
        for instance, connection in list(self.bridge.connections.items()):
            try:
                tabs = await self.bridge.call(instance, "discover", {})
            except InteractionError:
                continue
            surfaces.extend(
                Surface(
                    id=f"web:{instance}:{tab['id']}",
                    kind="web",
                    title=tab["title"],
                    url=tab["url"],
                    application=connection.name,
                    actions=["click", "fill", "press", "scroll", "navigate"],
                )
                for tab in tabs
            )
        return surfaces

    async def acquire(self, surface: Surface, owner: str):
        instance, tab = self.address(surface)
        await self.bridge.call(
            instance, "acquire", {"tab": tab, "owner": owner, "expected_url": surface.url}
        )

    async def observe(self, surface: Surface, owner: str) -> Observation:
        instance, tab = self.address(surface)
        result = await self.bridge.call(instance, "observe", {"tab": tab, "owner": owner})
        current = surface.model_copy(
            update={"title": result.pop("title"), "url": result.pop("url")}
        )
        return Observation(surface=current, **result)

    async def act(
        self, surface: Surface, owner: str, observation_id: str, action: Action
    ) -> Receipt:
        instance, tab = self.address(surface)
        result = await self.bridge.call(
            instance,
            "act",
            {
                "tab": tab,
                "owner": owner,
                "observation_id": observation_id,
                "action": action.model_dump(),
            },
        )
        return Receipt.model_validate(result)

    async def release(self, surface: Surface, owner: str):
        instance, tab = self.address(surface)
        await self.bridge.call(instance, "release", {"tab": tab, "owner": owner})

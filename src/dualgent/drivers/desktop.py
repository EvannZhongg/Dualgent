"""OS isolation: native accessibility work runs in a killable, serialized helper."""

from __future__ import annotations

import asyncio
import json
import sys

from ..contracts import Action, InteractionError, Observation, Receipt, Surface


class DesktopDriver:
    def __init__(self):
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return sys.platform == "win32"

    async def _call(self, command: str, arguments: dict):
        if not self.available:
            raise InteractionError("此平台的桌面驱动尚未实现；浏览器扩展仍可使用。")
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "dualgent.drivers.windows_worker",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=0x08000000,  # CREATE_NO_WINDOW
                    limit=1024 * 1024,
                )
            process = self.process
            sent = False
            try:
                process.stdin.write(
                    (json.dumps({"command": command, "arguments": arguments}) + "\n").encode()
                )
                sent = True
                await process.stdin.drain()
                line = await asyncio.wait_for(process.stdout.readline(), 15)
                if not line:
                    raise ConnectionError("桌面驱动已退出；请确认已安装 Windows 依赖。")
                response = json.loads(line)
                if "error" in response:
                    raise InteractionError(
                        response["error"], unknown=response.get("unknown", False)
                    )
                return response["result"]
            except InteractionError:
                raise
            except asyncio.CancelledError:
                await self._stop()
                raise
            except (TimeoutError, ConnectionError, OSError, ValueError) as error:
                await self._stop()
                raise InteractionError(
                    f"桌面驱动中断：{type(error).__name__}；请检查窗口后继续。",
                    unknown=sent and command == "act",
                ) from error

    async def discover(self) -> list[Surface]:
        if not self.available:
            return []
        return [Surface.model_validate(s) for s in await self._call("discover", {})]

    async def acquire(self, surface: Surface, owner: str):
        await self._call("acquire", {"surface": surface.model_dump(), "owner": owner})

    async def observe(self, surface: Surface, owner: str) -> Observation:
        return Observation.model_validate(
            await self._call("observe", {"id": surface.id, "owner": owner})
        )

    async def act(
        self, surface: Surface, owner: str, observation_id: str, action: Action
    ) -> Receipt:
        return Receipt.model_validate(
            await self._call(
                "act",
                {
                    "id": surface.id,
                    "owner": owner,
                    "observation_id": observation_id,
                    "action": action.model_dump(),
                },
            )
        )

    async def release(self, surface: Surface, owner: str):
        if self.process and self.process.returncode is None:
            await self._call("release", {"id": surface.id, "owner": owner})

    async def _stop(self):
        process = self.process
        self.process = None
        if process and process.returncode is None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                # Windows venv python.exe can be a redirector with a child interpreter.
                # Terminate the helper's entire private process tree, never other apps.
                if sys.platform == "win32":
                    import psutil

                    try:
                        for child in psutil.Process(process.pid).children(recursive=True):
                            try:
                                child.kill()
                            except psutil.NoSuchProcess:
                                pass
                    except psutil.NoSuchProcess:
                        pass
                if process.returncode is None:
                    process.kill()
                await process.wait()

    async def close(self):
        await self._stop()

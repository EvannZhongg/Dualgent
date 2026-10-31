from __future__ import annotations

import asyncio
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field

from . import __version__
from .agent import InteractAgent
from .config import Settings
from .contracts import Record
from .drivers.browser import BrowserBridge, BrowserDriver
from .drivers.desktop import DesktopDriver
from .model import DeepSeekModel
from .registry import Environments
from .store import TaskStore

ASSETS = Path(__file__).parent / "web"


class NewTask(Record):
    goal: str = Field(min_length=1, max_length=8000)
    surface_ids: list[str] = Field(min_length=1, max_length=4)
    max_steps: int = Field(default=24, ge=1, le=80)


class ResumeTask(Record):
    note: str = Field(min_length=1, max_length=4000)


def connection_token(directory: Path) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "connection-token"
    if not path.exists():
        try:
            with path.open("x", encoding="utf-8") as file:
                file.write(secrets.token_urlsafe(32))
            path.chmod(0o600)
        except FileExistsError:
            pass
    token = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise ValueError("本机连接码文件损坏；停止服务并移除该文件后重新启动。")
    return token


def create_app(settings: Settings, *, model=None, environments=None, token: str | None = None):
    token = token or connection_token(settings.data_dir)
    store = TaskStore(settings.data_dir / "tasks.sqlite3")
    bridge = BrowserBridge()
    desktop = DesktopDriver()
    environments = environments or Environments({"web": BrowserDriver(bridge), "desktop": desktop})
    model = model or DeepSeekModel(settings)
    agent = InteractAgent(store, environments, model)

    @asynccontextmanager
    async def lifespan(app):
        agent.start()
        yield
        await agent.close()
        await desktop.close()
        if hasattr(model, "close"):
            await model.close()
        store.close()

    app = FastAPI(
        title="Dualgent",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store, app.state.agent, app.state.bridge = store, agent, bridge

    @app.middleware("http")
    async def protect(request: Request, call_next):
        hosts = {f"127.0.0.1:{settings.port}", f"localhost:{settings.port}"}
        if request.headers.get("host") not in hosts:
            return JSONResponse({"detail": "invalid host"}, status_code=403)
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and origin not in {f"http://{host}" for host in hosts}:
                return JSONResponse({"detail": "invalid origin"}, status_code=403)
            authorization = request.headers.get("authorization", "")
            if not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
                return JSONResponse({"detail": "请使用本机连接码登录。"}, status_code=401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.get("/")
    async def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/assets/{name}")
    async def asset(name: str):
        if name not in {"app.js", "app.css"}:
            raise HTTPException(404)
        return FileResponse(ASSETS / name)

    @app.get("/api/status")
    async def status():
        return {
            "version": __version__,
            "model": settings.model,
            "model_ready": bool(settings.api_key),
            "browsers": [
                {"id": key, "name": value.name} for key, value in bridge.connections.items()
            ],
            "desktop": "windows" if desktop.available else "unavailable",
        }

    @app.get("/api/connection")
    async def connection():
        return {
            "token": token,
            "port": settings.port,
            "extension_path": str(Path(__file__).parent / "extension"),
        }

    @app.get("/api/surfaces")
    async def surfaces():
        available = await environments.discover()
        return {"surfaces": [s.model_dump() for s in available], "errors": environments.errors}

    @app.get("/api/tasks")
    async def tasks():
        return store.list()

    @app.post("/api/tasks", status_code=201)
    async def create(body: NewTask):
        if not body.goal.strip():
            raise HTTPException(422, "任务描述不能为空。")
        available = {s.id: s for s in await environments.discover()}
        if not set(body.surface_ids).issubset(available):
            raise HTTPException(409, "所选环境已变化，请刷新后重选。")
        selected = [available[sid] for sid in dict.fromkeys(body.surface_ids)]
        task = store.create(body.goal.strip(), selected, body.max_steps)
        agent.notify()
        return task

    def require_task(task_id):
        try:
            return store.get(task_id)
        except KeyError:
            raise HTTPException(404, "任务不存在。") from None

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str):
        return require_task(task_id)

    @app.get("/api/tasks/{task_id}/events")
    async def events(task_id: str, after: int = 0):
        require_task(task_id)
        return store.events(task_id, max(0, after))

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(task_id: str):
        require_task(task_id)
        store.cancel(task_id)
        return store.get(task_id)

    @app.post("/api/tasks/{task_id}/resume")
    async def resume(task_id: str, body: ResumeTask):
        require_task(task_id)
        if not body.note.strip():
            raise HTTPException(422, "请说明你已检查的结果或补充任务信息。")
        try:
            store.resume(task_id, body.note.strip())
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        agent.notify()
        return store.get(task_id)

    @app.websocket("/extension")
    async def extension(socket: WebSocket):
        if not re.fullmatch(r"chrome-extension://[a-p]{32}", socket.headers.get("origin", "")):
            await socket.close(code=1008)
            return
        if socket.headers.get("host") not in {
            f"127.0.0.1:{settings.port}",
            f"localhost:{settings.port}",
        }:
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            hello = await asyncio.wait_for(socket.receive_json(), 5)
            if (
                hello.get("type") != "hello"
                or not isinstance(hello.get("token"), str)
                or not secrets.compare_digest(hello["token"].encode(), token.encode())
                or not re.fullmatch(r"[a-f0-9-]{32,36}", str(hello.get("instance", "")))
            ):
                await socket.close(code=1008)
                return
            await bridge.serve(socket, hello["instance"], str(hello.get("name", "Browser"))[:80])
        except (WebSocketDisconnect, TimeoutError, ValueError, RuntimeError):
            pass

    return app

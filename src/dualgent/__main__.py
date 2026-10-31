from __future__ import annotations

import argparse
import asyncio
import webbrowser
from pathlib import Path

import uvicorn

from .config import Settings
from .server import connection_token, create_app


def main():
    parser = argparse.ArgumentParser(description="Dualgent · Web 与桌面交互智能体")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path(".dualgent"))
    parser.add_argument("--open", action="store_true", help="在默认浏览器中打开已认证的工作台")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    settings = Settings.load(args.data_dir, args.port)
    token = connection_token(settings.data_dir)
    print(f"Dualgent: http://127.0.0.1:{args.port}")
    print(f"本机连接码文件: {settings.data_dir / 'connection-token'}")

    async def serve():
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(settings, token=token),
                host="127.0.0.1",
                port=args.port,
                access_log=False,
                ws_max_size=1024 * 1024,
                timeout_graceful_shutdown=5,
            )
        )
        running = asyncio.create_task(server.serve())
        if args.open:
            while not server.started and not running.done():
                await asyncio.sleep(0.05)
            if server.started:
                await asyncio.to_thread(
                    webbrowser.open, f"http://127.0.0.1:{args.port}/#token={token}"
                )
        await running

    asyncio.run(serve())


if __name__ == "__main__":
    main()

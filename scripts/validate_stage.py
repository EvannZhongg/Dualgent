"""Opt-in real-model validation against disposable, local browser/Windows fixtures.

Playwright only provisions an isolated browser, pairs the extension, and reads assertions.
All task input is performed by the production Dualgent drivers and DeepSeek decision loop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import uvicorn
from playwright.async_api import async_playwright

from dualgent.config import Settings
from dualgent.server import connection_token, create_app

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


async def wait_task(client, task_id, *, seconds=240):
    deadline = time.monotonic() + seconds
    previous = ""
    while time.monotonic() < deadline:
        task = (await client.get(f"/api/tasks/{task_id}")).json()
        if task["state"] != previous:
            print(f"  {task_id[:8]}: {task['state']}", flush=True)
            previous = task["state"]
        if task["state"] in {"succeeded", "failed", "cancelled", "needs_attention"}:
            return task
        await asyncio.sleep(0.4)
    await client.post(f"/api/tasks/{task_id}/cancel")
    raise TimeoutError("Live task exceeded validation deadline")


def launch_desktop(directory: Path):
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    exe = directory / "DualgentFixture.exe"
    subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:winexe",
            f"/out:{exe}",
            "/r:System.Windows.Forms.dll",
            "/r:System.Drawing.dll",
            str(FIXTURES / "DesktopFixture.cs"),
        ],
        check=True,
        capture_output=True,
    )
    output = directory / "desktop-result.txt"
    process = subprocess.Popen([str(exe), str(output)], creationflags=0x08000000)
    return process, output


async def main(args):
    directory = ROOT / ".dualgent" / f"validation-{time.strftime('%Y%m%d-%H%M%S')}"
    directory.mkdir(parents=True)
    settings = Settings.load(directory, args.port)
    if not settings.api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is required for live validation")
    token = connection_token(directory)
    app = create_app(settings, token=token)
    environment = app.state.agent.environments
    original_discover = environment.discover
    fixture_url = f"http://127.0.0.1:{args.port + 1}/form.html"

    async def fixture_discovery():
        return [
            s
            for s in await original_discover()
            if s.url == fixture_url or s.title == "Dualgent desktop verification lab"
        ]

    # The validation app exposes only its fixtures, including in screenshots and reports.
    environment.discover = fixture_discovery
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=args.port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=5,
        )
    )
    serving = asyncio.create_task(server.serve())
    fixture_server = ThreadingHTTPServer(
        ("127.0.0.1", args.port + 1), partial(QuietHandler, directory=str(FIXTURES))
    )
    thread = threading.Thread(target=fixture_server.serve_forever, daemon=True)
    thread.start()
    desktop_process = None
    report = {
        "model": settings.model,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "tasks": [],
        "assertions": {},
    }
    try:
        while not server.started:
            if serving.done():
                await serving
                raise RuntimeError("Validation server did not start")
            await asyncio.sleep(0.05)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{args.port}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        ) as client:
            async with async_playwright() as playwright:
                extension = ROOT / "src" / "dualgent" / "extension"
                launch_options = {
                    "headless": True,
                    "args": [
                        f"--disable-extensions-except={extension}",
                        f"--load-extension={extension}",
                    ],
                }
                if args.chromium:
                    launch_options["executable_path"] = args.chromium
                context = await playwright.chromium.launch_persistent_context(
                    directory / "browser-profile", **launch_options
                )
                report["chromium"] = context.browser.version
                try:
                    worker = (
                        context.service_workers[0]
                        if context.service_workers
                        else await context.wait_for_event("serviceworker")
                    )
                    await worker.evaluate(
                        """async settings => {
                        await chrome.storage.local.set(settings);
                        await chrome.alarms.create('validation-connect', {when:Date.now()+50});
                    }""",
                        {
                            "token": token,
                            "port": args.port,
                            "name": "Isolated verification browser",
                        },
                    )
                    page = await context.new_page()
                    await page.goto(fixture_url)
                    for _ in range(100):
                        discovered = (await client.get("/api/surfaces")).json()
                        web = next(
                            (s for s in discovered["surfaces"] if s.get("url") == fixture_url), None
                        )
                        if web:
                            break
                        await asyncio.sleep(0.15)
                    else:
                        raise RuntimeError(f"Extension failed to connect: {discovered['errors']}")
                    web_goal = (
                        "在 Project request 页面将 Project name 填为 Dualgent，Owner 填为 Ada，"
                        "保持 Priority 为 Normal，预览后确认保存，验证最终页面的保存结果和回执。"
                        "这是本地可丢弃测试页面，我明确授权提交其中的测试表单。"
                    )
                    response = await client.post(
                        "/api/tasks", json={"goal": web_goal, "surface_ids": [web["id"]]}
                    )
                    response.raise_for_status()
                    task = await wait_task(client, response.json()["id"])
                    task["events"] = (await client.get(f"/api/tasks/{task['id']}/events")).json()
                    report["tasks"].append(task)
                    stored = await page.evaluate(
                        "JSON.parse(localStorage.getItem('dualgent-fixture'))"
                    )
                    report["assertions"]["web_saved_exact_record"] = stored == {
                        "project": "Dualgent",
                        "owner": "Ada",
                        "priority": "Normal",
                    }
                    report["assertions"]["web_task_verified"] = task["state"] == "succeeded"
                    if args.desktop:
                        if sys.platform != "win32":
                            raise RuntimeError("Desktop fixture requires Windows")
                        desktop_process, desktop_output = launch_desktop(directory)
                        for _ in range(50):
                            discovered = (await client.get("/api/surfaces")).json()
                            desktop = next(
                                (
                                    s
                                    for s in discovered["surfaces"]
                                    if s["title"] == "Dualgent desktop verification lab"
                                ),
                                None,
                            )
                            if desktop:
                                break
                            await asyncio.sleep(0.1)
                        else:
                            raise RuntimeError(
                                f"Desktop fixture not discovered: {discovered['errors']}"
                            )
                        goal = (
                            "先读取 Project request 网页已经保存的项目、Owner 和 Receipt 回执，"
                            "再把它们分别填入桌面 Dualgent desktop verification lab 窗口的 Project name、Owner、Web receipt。"
                            "点击 Preview record，核对预览后点击 Confirm save，并验证桌面保存结果以及网页回执仍存在。"
                            "这是两个本地可丢弃测试环境，我明确授权保存测试记录。不要猜测回执，请以网页内容为准。"
                        )
                        response = await client.post(
                            "/api/tasks",
                            json={
                                "goal": goal,
                                "surface_ids": [web["id"], desktop["id"]],
                                "max_steps": 24,
                            },
                        )
                        response.raise_for_status()
                        task = await wait_task(client, response.json()["id"])
                        task["events"] = (
                            await client.get(f"/api/tasks/{task['id']}/events")
                        ).json()
                        report["tasks"].append(task)
                        actual = desktop_output.read_text() if desktop_output.exists() else ""
                        report["assertions"]["cross_surface_exact_record"] = (
                            actual.splitlines() == ["Dualgent", "Ada", "DG-4827"]
                        )
                        report["assertions"]["cross_surface_task_verified"] = (
                            task["state"] == "succeeded"
                        )
                    # Screenshot only the local workbench and fixture data, never personal browser tabs.
                    workbench = await context.new_page()
                    await workbench.goto(f"http://127.0.0.1:{args.port}/#token={token}")
                    await workbench.get_by_role("button", name="新任务").wait_for()
                    await workbench.locator("#timeline .message").first.wait_for(timeout=10000)
                    await workbench.set_viewport_size({"width": 1440, "height": 1000})
                    await workbench.screenshot(path=directory / "workbench.png", full_page=True)
                finally:
                    await context.close()
    finally:
        if desktop_process:
            desktop_process.terminate()
            desktop_process.wait(timeout=10)
        fixture_server.shutdown()
        fixture_server.server_close()
        server.should_exit = True
        await serving
        (directory / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Report: {directory / 'report.json'}", flush=True)
        print(json.dumps(report["assertions"], ensure_ascii=False), flush=True)
    if not report["assertions"] or not all(report["assertions"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop", action="store_true")
    parser.add_argument("--port", type=int, default=8877)
    parser.add_argument("--chromium", help="Optional path to an installed Playwright Chromium")
    asyncio.run(main(parser.parse_args()))

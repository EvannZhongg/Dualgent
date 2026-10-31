import asyncio
import sys

import pytest

from dualgent.contracts import InteractionError
from dualgent.drivers.browser import BrowserBridge, Connection


async def test_browser_timeout_fences_connection_and_marks_unknown():
    class SilentSocket:
        closed = False

        async def send_json(self, message):
            pass

        async def close(self, **kwargs):
            self.closed = True

    bridge = BrowserBridge()
    socket = SilentSocket()
    connection = Connection(socket, "Test")
    bridge.connections["instance"] = connection
    with pytest.raises(InteractionError) as result:
        await bridge.call("instance", "act", {}, timeout=0.01)
    assert result.value.unknown
    assert socket.closed
    assert not connection.pending


@pytest.mark.skipif(sys.platform != "win32", reason="Windows helper")
async def test_desktop_helper_closes_without_child_process_leak():
    psutil = pytest.importorskip("psutil")
    pytest.importorskip("uiautomation")
    from dualgent.drivers.desktop import DesktopDriver

    driver = DesktopDriver()
    await driver.discover()
    process = psutil.Process(driver.process.pid)
    pids = [p.pid for p in process.children(recursive=True)] + [process.pid]
    await driver.close()
    async with asyncio.timeout(4):
        while any(psutil.pid_exists(pid) for pid in pids):
            await asyncio.sleep(0.05)

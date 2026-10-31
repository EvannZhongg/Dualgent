import { createBrowser } from "./browser.js";

const browser = createBrowser(chrome);
let socket,
  timer,
  heartbeat,
  connecting = false;
let cleanup = Promise.resolve();
let status = "未连接";

async function connect() {
  if (connecting || socket?.readyState === WebSocket.OPEN) return;
  connecting = true;
  try {
    await cleanup;
    const settings = await chrome.storage.local.get(["token", "port", "name"]);
    if (!settings.token) {
      status = "请先在设置中配对";
      return;
    }
    const session = await chrome.storage.session.get("runId");
    const instance = session.runId || crypto.randomUUID();
    if (!session.runId) await chrome.storage.session.set({ runId: instance });
    const current = new WebSocket(
      `ws://127.0.0.1:${settings.port || 8765}/extension`,
    );
    socket = current;
    current.onopen = () => {
      current.send(
        JSON.stringify({
          type: "hello",
          token: settings.token,
          instance,
          name:
            settings.name ||
            (/Edg\//.test(navigator.userAgent) ? "Edge" : "Chrome"),
        }),
      );
      heartbeat = setInterval(() => {
        if (current.readyState === WebSocket.OPEN)
          current.send(JSON.stringify({ type: "ping" }));
      }, 20000);
    };
    current.onmessage = async (event) => {
      let request;
      try {
        request = JSON.parse(event.data);
        if (request.type === "connected") {
          status = "已连接";
          await chrome.action.setBadgeText({ text: "ON" });
          await chrome.action.setBadgeBackgroundColor({ color: "#2366bc" });
          return;
        }
        if (!request.id) return;
        const result = await browser.execute(
          request.command,
          request.arguments,
        );
        if (current.readyState === WebSocket.OPEN)
          current.send(JSON.stringify({ id: request.id, result }));
      } catch (error) {
        if (request?.id && current.readyState === WebSocket.OPEN)
          current.send(
            JSON.stringify({
              id: request.id,
              error: { detail: error.message, unknown: false },
            }),
          );
      }
    };
    current.onclose = () => {
      clearInterval(heartbeat);
      status = "连接已断开";
      cleanup = browser.cleanup();
      // Keep rejected cleanup observable and block reattachment rather than stealing ownership.
      cleanup.catch((error) => {
        status = error.message;
      });
      chrome.action.setBadgeText({ text: "OFF" }).catch(() => {});
      clearTimeout(timer);
      timer = setTimeout(connect, 2000);
    };
    current.onerror = () => current.close();
  } catch (error) {
    status = error.message;
  } finally {
    connecting = false;
  }
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id) return;
  if (message.type === "status") {
    respond({ status });
    return;
  }
  if (message.type === "configure") {
    chrome.storage.local.set(message.settings).then(async () => {
      if (socket && socket.readyState < WebSocket.CLOSING) socket.close();
      else await connect();
      respond({ ok: true });
    });
    return true;
  }
});
chrome.alarms.onAlarm.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
chrome.alarms.create("dualgent-connect", { periodInMinutes: 0.5 });
connect();

const form = document.querySelector("form");
const token = document.querySelector("#token");
const port = document.querySelector("#port");
const name = document.querySelector("#name");
const status = document.querySelector("#status");
const saved = await chrome.storage.local.get(["token", "port", "name"]);
token.value = saved.token || "";
port.value = saved.port || 8765;
name.value = saved.name || "";
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  await chrome.runtime.sendMessage({
    type: "configure",
    settings: {
      token: token.value.trim(),
      port: Number(port.value),
      name: name.value.trim(),
    },
  });
  status.textContent = "设置已保存，正在连接…";
});
setInterval(async () => {
  try {
    status.textContent = (
      await chrome.runtime.sendMessage({ type: "status" })
    ).status;
  } catch {
    status.textContent = "扩展已重载，请重新打开设置。";
  }
}, 1500);

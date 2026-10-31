import assert from "node:assert/strict";
import { test } from "node:test";
import { createBrowser } from "../src/dualgent/extension/browser.js";

function fixture() {
  const state = {
    url: "https://fixture.test/form",
    title: "Form",
    attached: false,
    loader: "doc-1",
    role: "textbox",
    name: "Name",
    value: "",
    sent: [],
    failRelease: false,
    failAfterDispatch: false,
  };
  const listener = () => ({ addListener() {} });
  const node = () => ({
    backendDOMNodeId: 42,
    nodeId: "42",
    role: { value: state.role },
    name: { value: state.name },
    value: { value: state.value },
    properties: [],
  });
  const chrome = {
    tabs: {
      query: async () => [{ id: 1, url: state.url, title: state.title }],
      get: async () => ({ id: 1, url: state.url, title: state.title }),
      onRemoved: listener(),
    },
    debugger: {
      onDetach: listener(),
      onEvent: listener(),
      attach: async () => {
        state.attached = true;
      },
      detach: async () => {
        if (state.failRelease) throw new Error("detach failed");
        state.attached = false;
      },
      sendCommand: async (_, method, params) => {
        if (method === "Page.getFrameTree")
          return {
            frameTree: { frame: { id: "root", loaderId: state.loader } },
          };
        if (
          [
            "Accessibility.getFullAXTree",
            "Accessibility.getPartialAXTree",
          ].includes(method)
        )
          return { nodes: [node()] };
        if (method === "DOM.resolveNode")
          return { object: { objectId: "node-object" } };
        if (method === "Runtime.callFunctionOn")
          return {
            result: {
              value: params.functionDeclaration.includes(
                "getBoundingClientRect",
              )
                ? { x: 20, y: 20, ok: true }
                : {
                    connected: true,
                    editable: true,
                    disabled: false,
                    password: false,
                  },
            },
          };
        if (method.startsWith("Input.")) {
          state.sent.push({ method, params });
          if (state.failAfterDispatch)
            throw new Error("transport lost after dispatch");
          if (method === "Input.insertText") state.value = params.text;
        }
        return {};
      },
    },
  };
  return { state, browser: createBrowser(chrome) };
}
async function claim(browser) {
  await browser.execute("discover", {});
  await browser.execute("acquire", {
    tab: 1,
    owner: "task",
    expected_url: "https://fixture.test/form",
  });
  return browser.execute("observe", { tab: 1, owner: "task" });
}
const actionArgs = (obs) => ({
  tab: 1,
  owner: "task",
  observation_id: obs.id,
  action: { kind: "fill", ref: obs.elements[0].ref, text: "Ada" },
});

test("native input receipt is distinct from verification, refs expire after dispatch", async () => {
  const { state, browser } = fixture();
  const obs = await claim(browser);
  assert.equal(
    (await browser.execute("act", actionArgs(obs))).status,
    "dispatched",
  );
  assert.equal(state.value, "Ada");
  assert.equal(
    (await browser.execute("act", actionArgs(obs))).status,
    "rejected",
  );
  assert.equal(
    state.sent.filter((s) => s.method === "Input.insertText").length,
    1,
  );
});
test("new observations invalidate old references", async () => {
  const { state, browser } = fixture();
  const old = await claim(browser);
  const latest = await browser.execute("observe", { tab: 1, owner: "task" });
  assert.notEqual(old.id, latest.id);
  assert.equal(
    (await browser.execute("act", actionArgs(old))).status,
    "rejected",
  );
  assert.equal(state.sent.length, 0);
});
test("node identity and navigation changes reject input before dispatch", async () => {
  for (const change of [
    (s) => (s.name = "Different"),
    (s) => (s.loader = "doc-2"),
    (s) => (s.url = "https://other.test/form"),
  ]) {
    const { state, browser } = fixture();
    const obs = await claim(browser);
    change(state);
    assert.equal(
      (await browser.execute("act", actionArgs(obs))).status,
      "rejected",
    );
    assert.equal(state.sent.length, 0);
  }
});
test("partial input failure is unknown, never silently replayed", async () => {
  const { state, browser } = fixture();
  const obs = await claim(browser);
  state.failAfterDispatch = true;
  assert.equal(
    (await browser.execute("act", actionArgs(obs))).status,
    "unknown",
  );
  assert.equal(state.sent.length, 1);
});
test("leases isolate owners and cleanup failure does not transfer ownership", async () => {
  const { state, browser } = fixture();
  await claim(browser);
  await assert.rejects(
    browser.execute("acquire", {
      tab: 1,
      owner: "other",
      expected_url: state.url,
    }),
    /another task/,
  );
  state.failRelease = true;
  await assert.rejects(browser.cleanup(), /Cannot release/);
  await assert.rejects(
    browser.execute("acquire", {
      tab: 1,
      owner: "other",
      expected_url: state.url,
    }),
    /another task/,
  );
});
test("discovery identity and origin scope are enforced", async () => {
  const { state, browser } = fixture();
  await browser.execute("discover", {});
  state.url = "https://fixture.test/changed";
  await assert.rejects(
    browser.execute("acquire", {
      tab: 1,
      owner: "task",
      expected_url: "https://fixture.test/form",
    }),
    /identity changed/,
  );
  state.url = "https://fixture.test/form";
  await claim(browser);
  const obs = await browser.execute("observe", { tab: 1, owner: "task" });
  const receipt = await browser.execute("act", {
    tab: 1,
    owner: "task",
    observation_id: obs.id,
    action: { kind: "navigate", url: "https://other.test/" },
  });
  assert.equal(receipt.status, "rejected");
  assert.equal(state.sent.length, 0);
});

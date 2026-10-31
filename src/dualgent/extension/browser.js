// The extension owns tab leases and observation-scoped node identity, not agent decisions.
export function createBrowser(chrome) {
  const claims = new Map();
  const queues = new Map();
  const seen = new Map();
  const cdp = (tab, method, params = {}) =>
    chrome.debugger.sendCommand({ tabId: tab }, method, params);
  const fail = (message) => {
    throw new Error(message);
  };
  const origin = (url) => {
    const parsed = new URL(url);
    if (
      !["http:", "https:"].includes(parsed.protocol) ||
      parsed.username ||
      parsed.password
    ) {
      fail("Only HTTP(S) pages without embedded credentials are supported.");
    }
    return parsed.origin;
  };
  const serial = (tab, work) => {
    const previous = queues.get(tab) || Promise.resolve();
    const next = previous.catch(() => {}).then(work);
    queues.set(tab, next);
    next
      .finally(() => {
        if (queues.get(tab) === next) queues.delete(tab);
      })
      .catch(() => {});
    return next;
  };
  async function lease(tab, owner) {
    const claim = claims.get(tab);
    if (!claim || claim.owner !== owner || claim.invalid)
      fail("Tab ownership was lost.");
    const current = await chrome.tabs.get(tab);
    if (origin(current.url) !== claim.origin)
      fail("Page left the authorized origin. Select it again in a new task.");
    return claim;
  }
  const frame = async (tab) => (await cdp(tab, "Page.getFrameTree")).frameTree;
  const value = (property) => property?.value;
  const roleOf = (node) => String(value(node.role) || "unknown");
  const nameOf = (node) => String(value(node.name) || "");
  const roles = new Set([
    "button",
    "link",
    "textbox",
    "searchbox",
    "combobox",
    "checkbox",
    "radio",
    "switch",
    "tab",
    "menuitem",
    "option",
    "slider",
    "spinbutton",
    "listbox",
    "treeitem",
    "scrollbar",
  ]);

  async function observe(tab, owner) {
    const claim = await lease(tab, owner);
    const before = await frame(tab);
    const { nodes } = await cdp(tab, "Accessibility.getFullAXTree");
    const after = await frame(tab);
    if (before.frame.loaderId !== after.frame.loaderId)
      fail("Document changed during observation. Observe again.");
    const id = crypto.randomUUID();
    const refs = new Map();
    const elements = [];
    const text = [];
    let truncated = Boolean(after.childFrames?.length);
    let size = 0;
    for (const node of nodes) {
      if (node.ignored) continue;
      const role = roleOf(node),
        name = nameOf(node);
      if (role === "InlineTextBox" || ["none", "generic"].includes(role))
        continue;
      if (node.frameId && node.frameId !== after.frame.id) {
        truncated = true;
        continue;
      }
      // AX name/value may expose passwords. Password controls have a protected property.
      const protectedValue = node.properties?.some(
        (p) => p.name === "protected" && value(p.value),
      );
      const currentValue = protectedValue ? undefined : value(node.value);
      const line = `${role} ${name}${currentValue === undefined ? "" : ` = ${currentValue}`}`;
      if (size + line.length < 14000) {
        text.push(line);
        size += line.length;
      } else truncated = true;
      if (node.backendDOMNodeId && roles.has(role) && !protectedValue) {
        if (elements.length >= 180) {
          truncated = true;
          continue;
        }
        const ref = `${id}:${elements.length}`;
        const enabled = !node.properties?.some(
          (p) => p.name === "disabled" && value(p.value),
        );
        refs.set(ref, { backend: node.backendDOMNodeId, role, name });
        elements.push({
          ref,
          role,
          name,
          value: currentValue == null ? null : String(currentValue),
          enabled,
        });
      }
    }
    const current = await chrome.tabs.get(tab);
    if (origin(current.url) !== claim.origin)
      fail("Page left the authorized origin.");
    claim.snapshot = { id, loader: after.frame.loaderId, refs };
    return {
      id,
      title: current.title || "",
      url: current.url,
      text: text.join("\n"),
      elements,
      truncated,
    };
  }

  async function resolve(tab, claim, ref) {
    const saved = claim.snapshot?.refs.get(ref);
    if (!saved) fail("Unknown or stale element reference. Observe again.");
    const current = await frame(tab);
    if (current.frame.loaderId !== claim.snapshot.loader)
      fail("Document changed. Observe again.");
    const { nodes } = await cdp(tab, "Accessibility.getPartialAXTree", {
      backendNodeId: saved.backend,
      fetchRelatives: false,
    });
    const actual = nodes.find(
      (n) => n.backendDOMNodeId === saved.backend && !n.ignored,
    );
    if (
      !actual ||
      roleOf(actual) !== saved.role ||
      nameOf(actual) !== saved.name
    ) {
      fail("Element identity changed. Observe again.");
    }
    const { object } = await cdp(tab, "DOM.resolveNode", {
      backendNodeId: saved.backend,
      objectGroup: "dualgent-action",
    });
    if (!object.objectId) fail("Element no longer exists.");
    return object.objectId;
  }

  async function onNode(tab, objectId, functionDeclaration) {
    const result = await cdp(tab, "Runtime.callFunctionOn", {
      objectId,
      functionDeclaration,
      returnByValue: true,
    });
    if (result.exceptionDetails) fail("Element inspection failed.");
    return result.result.value;
  }

  async function act(tab, owner, observationId, action) {
    let dispatched = false;
    const send = async (method, params) => {
      await lease(tab, owner);
      dispatched = true;
      return cdp(tab, method, params);
    };
    try {
      const claim = await lease(tab, owner);
      if (!claim.snapshot || claim.snapshot.id !== observationId)
        fail("Observation expired. Observe again.");
      if (action.kind === "navigate") {
        if (origin(action.url) !== claim.origin)
          fail("Navigation outside the authorized origin is not allowed.");
        await send("Page.navigate", { url: action.url });
      } else {
        const objectId = await resolve(tab, claim, action.ref);
        const info = await onNode(
          tab,
          objectId,
          `function() {
          return {connected:this.isConnected, disabled:!!this.disabled,
            password:this.type === 'password', editable:this.isContentEditable ||
            this.tagName === 'INPUT' || this.tagName === 'TEXTAREA'};
        }`,
        );
        if (!info.connected || info.disabled || info.password)
          fail("Element is unavailable or protected.");
        if (action.kind === "click" || action.kind === "scroll") {
          await send("DOM.scrollIntoViewIfNeeded", { objectId });
          const point = await onNode(
            tab,
            objectId,
            `function() {
            const r=this.getBoundingClientRect();
            const x=(Math.max(0,r.left)+Math.min(innerWidth,r.right))/2;
            const y=(Math.max(0,r.top)+Math.min(innerHeight,r.bottom))/2;
            const hit=this.getRootNode().elementFromPoint(x,y);
            return {x,y,ok:r.width>0 && r.height>0 && x>=0 && y>=0 &&
              x<innerWidth && y<innerHeight && (hit===this || this.contains(hit))};
          }`,
          );
          if (!point?.ok) fail("Element is obscured or outside the viewport.");
          await lease(tab, owner);
          if (action.kind === "click") {
            await send("Input.dispatchMouseEvent", {
              type: "mousePressed",
              x: point.x,
              y: point.y,
              button: "left",
              clickCount: 1,
            });
            await send("Input.dispatchMouseEvent", {
              type: "mouseReleased",
              x: point.x,
              y: point.y,
              button: "left",
              clickCount: 1,
            });
          } else {
            if (
              !Number.isInteger(action.delta) ||
              Math.abs(action.delta) > 1200
            )
              fail("Invalid scroll delta.");
            await send("Input.dispatchMouseEvent", {
              type: "mouseWheel",
              x: point.x,
              y: point.y,
              deltaY: action.delta,
              deltaX: 0,
            });
          }
        } else if (action.kind === "fill" || action.kind === "press") {
          if (action.kind === "fill" && !info.editable)
            fail("Target is not editable.");
          await send("DOM.focus", { objectId });
          await lease(tab, owner);
          if (action.kind === "fill") {
            if (typeof action.text !== "string" || action.text.length > 12000)
              fail("Invalid input text.");
            const modifiers = /Mac/.test(navigator.platform) ? 4 : 2;
            await send("Input.dispatchKeyEvent", {
              type: "rawKeyDown",
              key: "a",
              code: "KeyA",
              windowsVirtualKeyCode: 65,
              modifiers,
            });
            await send("Input.dispatchKeyEvent", {
              type: "keyUp",
              key: "a",
              code: "KeyA",
              windowsVirtualKeyCode: 65,
              modifiers,
            });
            await send("Input.insertText", { text: action.text });
          } else {
            const keys = {
              Enter: [13, "Enter", "\r"],
              Tab: [9, "Tab", ""],
              Escape: [27, "Escape", ""],
              ArrowDown: [40, "ArrowDown", ""],
              ArrowUp: [38, "ArrowUp", ""],
              Space: [32, "Space", " "],
            };
            const key = keys[action.key];
            if (!key) fail("Unsupported key.");
            await send("Input.dispatchKeyEvent", {
              type: "keyDown",
              key: action.key === "Space" ? " " : action.key,
              code: key[1],
              windowsVirtualKeyCode: key[0],
              text: key[2],
            });
            await send("Input.dispatchKeyEvent", {
              type: "keyUp",
              key: action.key === "Space" ? " " : action.key,
              code: key[1],
              windowsVirtualKeyCode: key[0],
            });
          }
        } else fail("Unsupported action.");
      }
      return {
        status: "dispatched",
        detail:
          "CDP input dispatched; business outcome requires fresh verification.",
      };
    } catch (error) {
      return {
        status: dispatched ? "unknown" : "rejected",
        detail: error.message,
      };
    } finally {
      const claim = claims.get(tab);
      if (claim) claim.snapshot = null;
      await cdp(tab, "Runtime.releaseObjectGroup", {
        objectGroup: "dualgent-action",
      }).catch(() => {});
    }
  }

  async function release(tab, owner) {
    const claim = claims.get(tab);
    if (!claim) return { released: true };
    if (claim.owner !== owner) fail("Tab belongs to another task.");
    await chrome.debugger.detach({ tabId: tab });
    claims.delete(tab);
    return { released: true };
  }

  async function execute(command, args) {
    if (command === "discover") {
      const tabs = (await chrome.tabs.query({})).filter((t) => {
        try {
          origin(t.url);
          return true;
        } catch {
          return false;
        }
      });
      seen.clear();
      for (const tab of tabs) seen.set(tab.id, tab.url);
      return tabs.map((t) => ({ id: t.id, title: t.title || "", url: t.url }));
    }
    return serial(args.tab, async () => {
      const { tab, owner } = args;
      if (command === "acquire") {
        if (claims.has(tab)) {
          if (claims.get(tab).owner !== owner)
            fail("Tab is already controlled by another task.");
          await lease(tab, owner);
          return { acquired: true };
        }
        const current = await chrome.tabs.get(tab);
        if (
          !seen.has(tab) ||
          current.url !== args.expected_url ||
          seen.get(tab) !== current.url
        ) {
          fail(
            "Tab identity changed since discovery. Refresh environments and select it again.",
          );
        }
        const claim = {
          owner,
          origin: origin(current.url),
          snapshot: null,
          invalid: false,
        };
        claims.set(tab, claim);
        let attached = false;
        try {
          await chrome.debugger.attach({ tabId: tab }, "1.3");
          attached = true;
          await cdp(tab, "Page.enable");
          await cdp(tab, "Accessibility.enable");
          await lease(tab, owner);
        } catch (error) {
          if (attached) {
            try {
              await release(tab, owner);
            } catch {
              claim.invalid = true;
            }
          } else if (claims.get(tab) === claim) claims.delete(tab);
          throw error;
        }
        return { acquired: true };
      }
      if (command === "observe") return observe(tab, owner);
      if (command === "act")
        return act(tab, owner, args.observation_id, args.action);
      if (command === "release") return release(tab, owner);
      fail("Unknown browser command.");
    });
  }

  async function cleanup() {
    for (const claim of claims.values()) claim.invalid = true;
    const results = await Promise.allSettled(
      [...claims].map(([tab, claim]) =>
        serial(tab, () => release(tab, claim.owner)),
      ),
    );
    if (results.some((r) => r.status === "rejected"))
      fail(
        "Cannot release a debugger session; close the affected tab before reconnecting.",
      );
    seen.clear();
  }
  chrome.debugger.onDetach.addListener((source) => {
    claims.delete(source.tabId);
  });
  chrome.tabs.onRemoved.addListener((tab) => {
    claims.delete(tab);
    seen.delete(tab);
  });
  chrome.debugger.onEvent.addListener((source, method) => {
    if (method === "Page.frameNavigated") {
      const claim = claims.get(source.tabId);
      if (claim) claim.snapshot = null;
    }
  });
  return { execute, cleanup };
}

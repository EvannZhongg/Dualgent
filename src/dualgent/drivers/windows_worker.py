"""Windows UIA adapter. This module is deliberately never imported by the agent core."""

from __future__ import annotations

import json
import sys
import time

import psutil
import uiautomation as auto
import win32con
import win32gui
import win32process

from ..contracts import Element, Observation, Receipt, Surface, identity

ROLES = {
    "ButtonControl": "button",
    "EditControl": "textbox",
    "TextControl": "text",
    "CheckBoxControl": "checkbox",
    "ComboBoxControl": "combobox",
    "ListItemControl": "option",
    "RadioButtonControl": "radio",
    "TabItemControl": "tab",
    "MenuItemControl": "menuitem",
    "HyperlinkControl": "link",
    "DocumentControl": "document",
    "WindowControl": "window",
    "ScrollBarControl": "scrollbar",
    "SliderControl": "slider",
}
BLOCKED = {
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "windowsterminal.exe",
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "lockapp.exe",
    "credentialuibroker.exe",
}


class WindowsAdapter:
    def __init__(self):
        self.seen: dict[str, Surface] = {}
        self.claims: dict[str, dict] = {}

    @staticmethod
    def surface(hwnd: int) -> Surface:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process = psutil.Process(pid)
        name = process.name()
        if name.lower() in BLOCKED:
            raise ValueError(
                "Use the browser extension for web pages; this app cannot be controlled."
            )
        return Surface(
            id=f"desktop:{hwnd}:{pid}:{int(process.create_time() * 1000)}",
            kind="desktop",
            title=win32gui.GetWindowText(hwnd),
            application=name,
            actions=["click", "fill", "press", "scroll"],
        )

    def discover(self):
        surfaces = []

        def visit(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd):
                return
            try:
                surface = self.surface(hwnd)
                surfaces.append(surface)
            except (ValueError, psutil.Error, OSError):
                pass

        win32gui.EnumWindows(visit, None)
        self.seen = {s.id: s for s in surfaces}
        return [s.model_dump() for s in surfaces]

    def acquire(self, surface: dict, owner: str):
        target = Surface.model_validate(surface)
        hwnd = int(target.id.split(":")[1])
        current = self.surface(hwnd)
        if target.id not in self.seen or current.id != target.id or current.title != target.title:
            raise ValueError(
                "Window changed since discovery. Refresh environments and select it again."
            )
        existing = self.claims.get(target.id)
        if existing and existing["owner"] != owner:
            raise ValueError("Window is owned by another task.")
        self.claims[target.id] = {"owner": owner, "surface": current, "snapshot": None}
        return {"acquired": True}

    def lease(self, surface_id: str, owner: str):
        claim = self.claims.get(surface_id)
        if not claim or claim["owner"] != owner:
            raise ValueError("Window ownership was lost.")
        hwnd = int(surface_id.split(":")[1])
        if self.surface(hwnd).id != surface_id:
            raise ValueError("Window process identity changed.")
        return claim, hwnd

    @staticmethod
    def info(control):
        role = ROLES.get(
            control.ControlTypeName, control.ControlTypeName.removesuffix("Control").lower()
        )
        name = control.Name or ""
        value = None
        if not control.IsPassword:
            pattern = control.GetPattern(auto.PatternId.ValuePattern)
            if pattern:
                value = str(pattern.Value)
        return role, name, value

    def observe(self, id: str, owner: str):
        claim, hwnd = self.lease(id, owner)
        root = auto.ControlFromHandle(hwnd)
        observation_id = identity()
        refs, elements, text = {}, [], []
        stack = [(root, 0)]
        visited, size, truncated = 0, 0, False
        started = time.monotonic()
        while stack:
            control, depth = stack.pop()
            visited += 1
            if visited > 600 or time.monotonic() - started > 6:
                truncated = True
                break
            if control.IsPassword:
                continue
            role, name, value = self.info(control)
            if name or value is not None:
                line = f"{role} {name}" + (f" = {value}" if value is not None else "")
                if size + len(line) <= 14000:
                    text.append(line)
                    size += len(line)
                else:
                    truncated = True
            if role in set(ROLES.values()) and role != "window" and not control.IsOffscreen:
                if len(elements) < 180:
                    ref = f"{observation_id}:{len(elements)}"
                    refs[ref] = (control, tuple(control.GetRuntimeId()), role, name)
                    elements.append(
                        Element(
                            ref=ref,
                            role=role,
                            name=name,
                            value=value,
                            enabled=bool(control.IsEnabled),
                        )
                    )
                else:
                    truncated = True
            children = control.GetChildren() if depth < 12 else []
            if depth == 12:
                truncated = True
            stack.extend((child, depth + 1) for child in reversed(children))
        claim["snapshot"] = {"id": observation_id, "refs": refs}
        return Observation(
            id=observation_id,
            surface=self.surface(hwnd),
            text="\n".join(text),
            elements=elements,
            truncated=truncated,
        ).model_dump()

    def act(self, id: str, owner: str, observation_id: str, action: dict):
        dispatched = False
        claim = None
        try:
            claim, hwnd = self.lease(id, owner)
            snapshot = claim["snapshot"]
            if not snapshot or snapshot["id"] != observation_id:
                raise ValueError("Observation expired. Observe again.")
            saved = snapshot["refs"].get(action.get("ref"))
            if saved is None:
                raise ValueError("Unknown element reference.")
            control, runtime_id, role, name = saved
            current_role, current_name, _ = self.info(control)
            if (tuple(control.GetRuntimeId()), current_role, current_name) != (
                runtime_id,
                role,
                name,
            ):
                raise ValueError("Element identity changed. Observe again.")
            if not control.IsEnabled or control.IsPassword or control.IsOffscreen:
                raise ValueError("Target is unavailable or protected.")
            top = control.GetTopLevelControl()
            if not top or top.NativeWindowHandle != hwnd:
                raise ValueError("Target moved outside the authorized window.")
            # A modal can take control of a window. Do not dispatch input through a disabled owner.
            if not win32gui.IsWindowEnabled(hwnd):
                raise ValueError(
                    "A modal dialog is blocking this window. Select the dialog explicitly."
                )
            if action["kind"] == "fill":
                pattern = control.GetPattern(auto.PatternId.ValuePattern)
                if not pattern or pattern.IsReadOnly:
                    raise ValueError("Target does not expose a writable UIA value.")
                dispatched = True
                pattern.SetValue(action["text"], waitTime=0.05)
                detail = "UIA ValuePattern.SetValue dispatched; requires fresh verification."
            elif action["kind"] == "click":
                pattern = control.GetPattern(auto.PatternId.InvokePattern)
                if pattern:
                    dispatched = True
                    pattern.Invoke(waitTime=0.05)
                    detail = "UIA InvokePattern invoked; requires fresh verification."
                else:
                    dispatched = True
                    self.foreground(hwnd)
                    rect = control.BoundingRectangle
                    x, y = int((rect.left + rect.right) / 2), int((rect.top + rect.bottom) / 2)
                    hit = win32gui.WindowFromPoint((x, y))
                    if win32gui.GetAncestor(hit, win32con.GA_ROOT) != hwnd:
                        raise ValueError("Target is obscured by another window.")
                    dispatched = True
                    auto.Click(x, y, waitTime=0.05)
                    detail = "Native pointer input dispatched; requires fresh verification."
            elif action["kind"] == "press":
                keys = {
                    "Enter": "{Enter}",
                    "Tab": "{Tab}",
                    "Escape": "{Esc}",
                    "ArrowDown": "{Down}",
                    "ArrowUp": "{Up}",
                    "Space": "{Space}",
                }
                key = keys[action["key"]]
                dispatched = True
                self.foreground(hwnd)
                control.SetFocus()
                if tuple(auto.GetFocusedControl().GetRuntimeId()) != runtime_id:
                    raise ValueError("Target did not receive focus.")
                dispatched = True
                auto.SendKeys(key, charMode=False, waitTime=0.05)
                detail = "Native keyboard input dispatched; requires fresh verification."
            elif action["kind"] == "scroll":
                pattern = control.GetPattern(auto.PatternId.ScrollPattern)
                if not pattern:
                    raise ValueError("Target does not expose UIA scrolling.")
                amount = (
                    auto.ScrollAmount.LargeIncrement
                    if action["delta"] > 0
                    else auto.ScrollAmount.LargeDecrement
                )
                dispatched = True
                pattern.Scroll(auto.ScrollAmount.NoAmount, amount, waitTime=0.05)
                detail = "UIA ScrollPattern dispatched; requires fresh verification."
            else:
                raise ValueError("Unsupported desktop action.")
            return Receipt(status="dispatched", detail=detail).model_dump()
        except Exception as error:
            return Receipt(
                status="unknown" if dispatched else "rejected", detail=str(error)
            ).model_dump()
        finally:
            if claim:
                claim["snapshot"] = None

    @staticmethod
    def foreground(hwnd):
        if win32gui.IsIconic(hwnd):
            raise ValueError("Window is minimized. Restore it before continuing.")
        if win32gui.GetForegroundWindow() != hwnd:
            win32gui.SetForegroundWindow(hwnd)
        if win32gui.GetForegroundWindow() != hwnd:
            raise ValueError("Windows did not grant foreground focus.")

    def release(self, id: str, owner: str):
        claim = self.claims.get(id)
        if claim and claim["owner"] != owner:
            raise ValueError("Window belongs to another task.")
        self.claims.pop(id, None)
        return {"released": True}


def main():
    adapter = WindowsAdapter()
    for line in sys.stdin.buffer:
        try:
            request = json.loads(line)
            command = request["command"]
            if command not in {"discover", "acquire", "observe", "act", "release"}:
                raise ValueError("Unknown desktop command")
            result = getattr(adapter, command)(**request["arguments"])
            response = {"result": result}
        except Exception as error:
            response = {"error": str(error), "unknown": False}
        sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()

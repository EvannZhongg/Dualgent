"""The interaction loop owns task state, bounded execution, evidence, and recovery."""

from __future__ import annotations

import asyncio
import json
import time

from pydantic import ValidationError

from .contracts import Decision, InteractionError, Model, Observation, Surface
from .registry import Environments
from .store import TERMINAL, TaskStore
from .verification import evaluate


class InteractAgent:
    def __init__(
        self,
        store: TaskStore,
        environments: Environments,
        model: Model,
        *,
        verify_seconds: float = 3,
    ):
        self.store, self.environments, self.model = store, environments, model
        self.verify_seconds = verify_seconds
        self.worker: asyncio.Task | None = None
        self.wakeup = asyncio.Event()
        self.run_start: dict[str, int] = {}

    def start(self):
        self.store.recover()
        self.worker = asyncio.create_task(self._work())
        self.notify()

    def notify(self):
        self.wakeup.set()

    async def close(self):
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass

    async def _work(self):
        # Desktop input is global. A single execution lane also prevents browser/UI races.
        while True:
            queued = [t for t in reversed(self.store.list()) if t["state"] == "queued"]
            if not queued:
                self.wakeup.clear()
                await self.wakeup.wait()
                continue
            await self.run(queued[0]["id"])

    def _cancelled(self, task_id: str) -> bool:
        if self.store.get(task_id)["cancel_requested"]:
            if self.store.get(task_id)["state"] not in TERMINAL:
                self.store.transition(task_id, "cancelled", "已取消；已派发的操作不会撤销。")
            return True
        return False

    def _record_observation(self, task_id: str, observation: Observation):
        self.store.event(task_id, "observation", observation.model_dump())

    async def _observe(self, task_id: str, surfaces: dict[str, Surface]) -> dict[str, Observation]:
        observations = {}
        for surface_id, surface in surfaces.items():
            observation = await self.environments.driver(surface).observe(surface, task_id)
            self._record_observation(task_id, observation)
            observations[surface_id] = observation
        return observations

    def _context(self, task_id: str, observations: dict[str, Observation]) -> dict:
        task = self.store.get(task_id)
        events = self.store.events(task_id)
        # Preserve the complete decision/receipt/check ledger on disk, send a bounded recent view.
        history = [
            e
            for e in events
            if e["kind"]
            in {
                "decision",
                "receipt",
                "verification",
                "feedback",
                "user_note",
                "recovery",
            }
        ]
        return {
            "goal": task["goal"],
            "steps_remaining": task["max_steps"]
            - sum(
                e["kind"] == "decision" and e["seq"] > self.run_start.get(task_id, 0)
                for e in events
            ),
            "user_notes": [e["data"] for e in events if e["kind"] == "user_note"],
            "history": [{"kind": e["kind"], "data": e["data"]} for e in history[-16:]],
            "observations": [o.model_dump() for o in observations.values()],
        }

    @staticmethod
    def _validate_scope(decision: Decision, surfaces: dict[str, Surface]):
        ids = {check.surface_id for check in decision.checks}
        if decision.surface_id:
            ids.add(decision.surface_id)
        if not ids.issubset(surfaces):
            raise ValueError("decision targets a surface outside this task")
        if decision.kind == "act":
            surface = surfaces[decision.surface_id]
            if decision.action.kind not in surface.actions:
                raise ValueError("action is not supported by this surface")

    async def _verify(self, task_id: str, decision: Decision, surfaces: dict[str, Surface]):
        deadline = time.monotonic() + self.verify_seconds
        while True:
            # Every check in a verification round must hold together on fresh observations.
            observations = await self._observe(
                task_id,
                {
                    sid: surfaces[sid]
                    for sid in dict.fromkeys(c.surface_id for c in decision.checks)
                },
            )
            results = [evaluate(c, observations[c.surface_id]) for c in decision.checks]
            if all(r["passed"] for r in results) or time.monotonic() >= deadline:
                self.store.event(
                    task_id,
                    "verification",
                    {"passed": all(r["passed"] for r in results), "results": results},
                )
                return all(r["passed"] for r in results)
            if self._cancelled(task_id):
                return False
            await asyncio.sleep(0.25)

    async def run(self, task_id: str):
        task = self.store.get(task_id)
        surfaces = {s["id"]: Surface.model_validate(s) for s in task["surfaces"]}
        acquired = []
        try:
            if self._cancelled(task_id):
                return
            self.store.transition(task_id, "observing")
            if any(e["kind"] == "user_note" for e in self.store.events(task_id)):
                surfaces = await self.environments.refresh_bindings(surfaces)
            self.store.event(task_id, "run_started", {})
            self.run_start[task_id] = self.store.events(task_id)[-1]["seq"]
            for surface in surfaces.values():
                await self.environments.driver(surface).acquire(surface, task_id)
                acquired.append(surface)
            count = 0
            invalid = 0
            previous_fingerprint, repeated = None, 0
            while count < task["max_steps"]:
                if self._cancelled(task_id):
                    return
                observations = await self._observe(task_id, surfaces)
                self.store.transition(task_id, "thinking")
                try:
                    decision, usage = await self.model.decide(self._context(task_id, observations))
                    self._validate_scope(decision, surfaces)
                    if decision.kind == "finish":
                        touched = {
                            e["data"]["surface_id"]
                            for e in self.store.events(task_id)
                            if e["kind"] == "dispatch_intent"
                        }
                        if not touched.issubset({c.surface_id for c in decision.checks}):
                            raise ValueError("final checks must cover every interacted surface")
                except (ValidationError, ValueError) as error:
                    invalid += 1
                    # Retain field paths and schema errors, without echoing the model's raw input.
                    detail = (
                        error.errors(include_input=False, include_context=False, include_url=False)
                        if isinstance(error, ValidationError)
                        else str(error)
                    )
                    self.store.event(
                        task_id,
                        "feedback",
                        {
                            "detail": "Invalid decision. Correct the "
                            f"schema or scope errors: {json.dumps(detail, ensure_ascii=False)}"
                        },
                    )
                    if invalid >= 3:
                        raise InteractionError(
                            "模型连续返回无效决策；请调整任务描述后继续。"
                        ) from error
                    self.store.transition(task_id, "observing")
                    continue
                invalid = 0
                self.store.event(task_id, "decision", decision.model_dump())
                self.store.event(task_id, "usage", usage)
                count += 1
                if self._cancelled(task_id):
                    return
                if decision.kind == "ask":
                    self.store.transition(task_id, "needs_attention", decision.summary)
                    return
                if decision.kind == "observe":
                    self.store.transition(task_id, "observing")
                    await asyncio.sleep(0.3)
                    continue
                if decision.kind == "act":
                    surface = surfaces[decision.surface_id]
                    action_data = decision.action.model_dump()
                    ref = action_data.pop("ref", None)
                    target = next(
                        (e for e in observations[surface.id].elements if e.ref == ref), None
                    )
                    fingerprint = json.dumps(
                        {
                            "surface": surface.id,
                            "action": action_data,
                            "target": [target.role, target.name] if target else ref,
                            "text": observations[surface.id].text,
                        },
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                    repeated = repeated + 1 if fingerprint == previous_fingerprint else 1
                    previous_fingerprint = fingerprint
                    if repeated >= 3:
                        raise InteractionError(
                            "相同操作未带来可观察变化；请检查环境或调整任务后继续。"
                        )
                    self.store.transition(task_id, "acting")
                    # Durable decision and dispatch intent precede every external side effect.
                    self.store.event(
                        task_id,
                        "dispatch_intent",
                        {
                            "surface_id": surface.id,
                            "observation_id": observations[surface.id].id,
                            "action": decision.action.model_dump(),
                        },
                    )
                    receipt = await self.environments.driver(surface).act(
                        surface, task_id, observations[surface.id].id, decision.action
                    )
                    self.store.event(task_id, "receipt", receipt.model_dump())
                    if receipt.status == "unknown":
                        # Read-only observation is safe; never automatically dispatch again.
                        try:
                            await self._observe(task_id, {surface.id: surface})
                        except Exception as error:
                            self.store.event(
                                task_id, "recovery", {"detail": str(error), "unknown": True}
                            )
                        raise InteractionError(receipt.detail, unknown=True)
                    if receipt.status == "rejected":
                        self.store.event(task_id, "feedback", {"detail": receipt.detail})
                        self.store.transition(task_id, "verifying")
                        self.store.transition(task_id, "observing")
                        continue
                self.store.transition(task_id, "verifying")
                passed = await self._verify(task_id, decision, surfaces)
                if self._cancelled(task_id):
                    return
                if decision.kind == "finish" and passed:
                    self.store.transition(task_id, "succeeded", decision.summary)
                    return
                self.store.transition(task_id, "observing")
            self.store.transition(
                task_id, "needs_attention", "已达到本轮步骤上限；检查执行记录后可继续。"
            )
        except asyncio.CancelledError:
            if self.store.get(task_id)["state"] not in TERMINAL:
                self.store.transition(task_id, "needs_attention", "执行中断；请检查环境后继续。")
            raise
        except InteractionError as error:
            self.store.event(task_id, "recovery", {"unknown": error.unknown, "detail": str(error)})
            if self.store.get(task_id)["state"] not in TERMINAL:
                self.store.transition(task_id, "needs_attention", str(error))
        except Exception as error:
            if self.store.get(task_id)["state"] not in TERMINAL:
                self.store.transition(
                    task_id, "failed", f"执行失败：{type(error).__name__}: {error}"
                )
        finally:
            for surface in reversed(acquired):
                try:
                    await self.environments.driver(surface).release(surface, task_id)
                except Exception as error:
                    self.store.event(
                        task_id, "cleanup_error", {"surface_id": surface.id, "detail": str(error)}
                    )

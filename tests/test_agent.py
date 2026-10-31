from __future__ import annotations

import asyncio

import pytest
from conftest import MemoryDriver

from dualgent.agent import InteractAgent
from dualgent.contracts import Check, Decision, Fill
from dualgent.registry import Environments


def check(surface_id="web:test:1"):
    return Check(
        surface_id=surface_id, kind="element", role="textbox", expected="Name", value="Ada"
    )


class FormModel:
    async def decide(self, context):
        observation = context["observations"][0]
        field = observation["elements"][0]
        if field["value"] == "Ada":
            return Decision(kind="finish", summary="Name is Ada", checks=[check()]), {}
        return Decision(
            kind="act",
            summary="Fill name",
            surface_id=observation["surface"]["id"],
            action=Fill(kind="fill", ref=field["ref"], text="Ada"),
            checks=[check()],
        ), {}


def make_agent(store, driver, model=None):
    return InteractAgent(
        store, Environments({"web": driver}), model or FormModel(), verify_seconds=0
    )


async def test_complete_only_after_fresh_evidence_and_durable_intent(store):
    driver = MemoryDriver()
    task = store.create("Enter Ada", [driver.surface], 8)
    original_act = driver.act

    async def act(*args):
        assert store.events(task["id"])[-1]["kind"] == "dispatch_intent"
        return await original_act(*args)

    driver.act = act
    await make_agent(store, driver).run(task["id"])
    assert store.get(task["id"])["state"] == "succeeded"
    assert len(driver.actions) == 1
    events = store.events(task["id"])
    observed = {e["data"]["id"] for e in events if e["kind"] == "observation"}
    verifications = [e for e in events if e["kind"] == "verification"]
    assert len(verifications) == 2
    assert all(e["data"]["passed"] for e in verifications)
    assert all(r["observation_id"] in observed for e in verifications for r in e["data"]["results"])
    assert driver.releases == 1


async def test_dispatch_acknowledgement_never_proves_success(store):
    driver = MemoryDriver(mutate=False)
    task = store.create("Enter Ada", [driver.surface], 8)
    await make_agent(store, driver).run(task["id"])
    assert store.get(task["id"])["state"] == "needs_attention"
    assert len(driver.actions) == 2  # Third identical action is stopped before dispatch.
    assert not any(
        e["data"].get("passed") for e in store.events(task["id"]) if e["kind"] == "verification"
    )


async def test_false_finish_cannot_mark_task_succeeded(store):
    class FalseFinish:
        async def decide(self, context):
            return Decision(kind="finish", summary="Done!", checks=[check()]), {}

    driver = MemoryDriver()
    task = store.create("Enter Ada", [driver.surface], 2)
    await make_agent(store, driver, FalseFinish()).run(task["id"])
    assert store.get(task["id"])["state"] == "needs_attention"
    assert driver.actions == []


async def test_unknown_result_is_observed_and_never_replayed(store):
    driver = MemoryDriver(receipt="unknown")
    task = store.create("Enter Ada", [driver.surface], 8)
    await make_agent(store, driver).run(task["id"])
    assert store.get(task["id"])["state"] == "needs_attention"
    assert len(driver.actions) == 1
    events = store.events(task["id"])
    receipt_index = next(i for i, e in enumerate(events) if e["kind"] == "receipt")
    assert any(e["kind"] == "observation" for e in events[receipt_index + 1 :])
    assert driver.releases == 1


async def test_cancel_while_model_is_thinking_does_not_dispatch(store):
    entered, finish = asyncio.Event(), asyncio.Event()

    class SlowModel(FormModel):
        async def decide(self, context):
            entered.set()
            await finish.wait()
            return await super().decide(context)

    driver = MemoryDriver()
    task = store.create("Enter Ada", [driver.surface], 8)
    running = asyncio.create_task(make_agent(store, driver, SlowModel()).run(task["id"]))
    await entered.wait()
    store.cancel(task["id"])
    finish.set()
    await running
    assert store.get(task["id"])["state"] == "cancelled"
    assert not driver.actions
    assert driver.releases == 1


async def test_scope_violations_never_reach_driver(store):
    class WrongScope:
        async def decide(self, context):
            return Decision(
                kind="act",
                summary="Wrong surface",
                surface_id="web:other:2",
                action=Fill(kind="fill", ref="invented", text="Ada"),
                checks=[check("web:other:2")],
            ), {}

    driver = MemoryDriver()
    task = store.create("Enter Ada", [driver.surface], 8)
    await make_agent(store, driver, WrongScope()).run(task["id"])
    assert store.get(task["id"])["state"] == "needs_attention"
    assert not driver.actions


async def test_resume_gets_new_step_budget_but_preserves_history(store):
    driver = MemoryDriver()
    agent = make_agent(store, driver)
    task = store.create("Enter Ada", [driver.surface], 1)
    await agent.run(task["id"])
    assert store.get(task["id"])["state"] == "needs_attention"
    store.resume(task["id"], "I checked: Ada is present. Verify and finish.")
    await agent.run(task["id"])
    assert store.get(task["id"])["state"] == "succeeded"
    assert len(driver.actions) == 1
    assert sum(e["kind"] == "run_started" for e in store.events(task["id"])) == 2


async def test_serial_worker_runs_queued_tasks(store):
    driver = MemoryDriver()
    tasks = [store.create("Enter Ada", [driver.surface], 8) for _ in range(2)]
    agent = make_agent(store, driver)
    agent.start()
    try:
        async with asyncio.timeout(3):
            while any(store.get(t["id"])["state"] != "succeeded" for t in tasks):
                await asyncio.sleep(0.01)
        assert driver.releases == 2
    finally:
        await agent.close()


def test_actions_and_finish_require_postconditions():
    with pytest.raises(ValueError):
        Decision(kind="finish", summary="Done")
    with pytest.raises(ValueError):
        Decision(
            kind="act",
            summary="Fill",
            surface_id="web:test:1",
            action=Fill(kind="fill", ref="ref", text="Ada"),
        )

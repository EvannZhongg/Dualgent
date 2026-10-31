import pytest
from conftest import MemoryDriver

from dualgent.contracts import Check, Element, Observation
from dualgent.store import TaskStore
from dualgent.verification import evaluate


def test_restart_interrupts_active_task_without_replay(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    store = TaskStore(path)
    task = store.create("Save form", [MemoryDriver().surface], 8)
    store.transition(task["id"], "observing")
    store.transition(task["id"], "thinking")
    store.transition(task["id"], "acting")
    store.event(task["id"], "dispatch_intent", {"action": {"kind": "click"}})
    store.close()
    recovered = TaskStore(path)
    try:
        recovered.recover()
        assert recovered.get(task["id"])["state"] == "needs_attention"
        assert sum(e["kind"] == "dispatch_intent" for e in recovered.events(task["id"])) == 1
        assert not any(e["kind"] == "receipt" for e in recovered.events(task["id"]))
    finally:
        recovered.close()


def test_one_executor_owns_data_directory(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    try:
        with pytest.raises(RuntimeError, match="Dualgent"):
            TaskStore(tmp_path / "tasks.sqlite3")
    finally:
        store.close()


def test_terminal_tasks_cannot_resume_or_transition(store):
    task = store.create("Save", [MemoryDriver().surface], 8)
    store.cancel(task["id"])
    with pytest.raises(ValueError):
        store.transition(task["id"], "observing")
    with pytest.raises(ValueError):
        store.resume(task["id"], "continue")


@pytest.mark.parametrize(
    "kind,expected,passed",
    [
        ("text", "Saved", True),
        ("text", "saved", False),
        ("title", "form", True),
        ("url", "/form", True),
    ],
)
def test_literal_environment_checks(kind, expected, passed):
    surface = MemoryDriver().surface
    observation = Observation(surface=surface, text="Saved", elements=[])
    assert (
        evaluate(Check(surface_id=surface.id, kind=kind, expected=expected), observation)["passed"]
        is passed
    )


def test_ambiguous_element_and_truncated_absence_cannot_verify():
    surface = MemoryDriver().surface
    field = Element(ref="a", role="textbox", name="Name", value="Ada")
    observation = Observation(
        surface=surface, text="Saved", elements=[field, field.model_copy(update={"ref": "b"})]
    )
    check = Check(
        surface_id=surface.id, kind="element", expected="Name", role="textbox", value="Ada"
    )
    assert not evaluate(check, observation)["passed"]
    observation.elements = []
    observation.truncated = True
    assert not evaluate(check.model_copy(update={"present": False}), observation)["passed"]
    assert not evaluate(
        Check(surface_id=surface.id, kind="text", expected="Error", present=False), observation
    )["passed"]

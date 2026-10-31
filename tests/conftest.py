from __future__ import annotations

import pytest

from dualgent.contracts import Element, Observation, Receipt, Surface
from dualgent.store import TaskStore


@pytest.fixture
def store(tmp_path):
    instance = TaskStore(tmp_path / "tasks.sqlite3")
    yield instance
    instance.close()


class MemoryDriver:
    def __init__(self, *, receipt="dispatched", mutate=True):
        self.surface = Surface(
            id="web:test:1",
            kind="web",
            title="Test form",
            url="http://localhost/form",
            application="Fixture",
            actions=["fill", "click"],
        )
        self.value = ""
        self.receipt = receipt
        self.mutate = mutate
        self.actions = []
        self.owner = None
        self.observation = None
        self.releases = 0

    async def discover(self):
        return [self.surface]

    async def acquire(self, surface, owner):
        assert self.owner in (None, owner)
        self.owner = owner

    async def observe(self, surface, owner):
        assert owner == self.owner
        self.observation = Observation(
            surface=self.surface,
            text=f"Name: {self.value}",
            elements=[
                Element(ref="temporary", role="textbox", name="Name", value=self.value),
            ],
        )
        self.observation.elements[0].ref = self.observation.id + ":0"
        return self.observation

    async def act(self, surface, owner, observation_id, action):
        assert owner == self.owner
        assert observation_id == self.observation.id
        assert action.ref == self.observation.elements[0].ref
        self.actions.append(action)
        if self.mutate:
            self.value = action.text
        return Receipt(status=self.receipt, detail="fixture receipt")

    async def release(self, surface, owner):
        assert owner == self.owner
        self.owner = None
        self.releases += 1

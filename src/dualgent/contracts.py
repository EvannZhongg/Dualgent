"""Platform-neutral interaction contracts. No transport or operating-system imports."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def identity() -> str:
    return uuid4().hex


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Surface(Record):
    id: str
    kind: Literal["web", "desktop"]
    title: str
    url: str | None = None
    application: str
    actions: list[str]


class Element(Record):
    ref: str
    role: str
    name: str = ""
    value: str | None = None
    enabled: bool = True


class Observation(Record):
    id: str = Field(default_factory=identity)
    surface: Surface
    text: str
    elements: list[Element]
    truncated: bool = False


class Click(Record):
    kind: Literal["click"]
    ref: str


class Fill(Record):
    kind: Literal["fill"]
    ref: str
    text: str = Field(max_length=12000)


class Press(Record):
    kind: Literal["press"]
    ref: str
    key: Literal["Enter", "Tab", "Escape", "ArrowDown", "ArrowUp", "Space"]


class Scroll(Record):
    kind: Literal["scroll"]
    ref: str
    delta: int = Field(ge=-1200, le=1200)


class Navigate(Record):
    kind: Literal["navigate"]
    url: str


Action = Annotated[Click | Fill | Press | Scroll | Navigate, Field(discriminator="kind")]


class Check(Record):
    surface_id: str
    kind: Literal["text", "title", "url", "element"]
    expected: str = Field(min_length=1, max_length=2000)
    role: str | None = None
    value: str | None = None
    present: bool = True

    @model_validator(mode="after")
    def validate_element(self):
        if self.kind == "element" and not self.role:
            raise ValueError("element checks require an exact role and name (expected)")
        if self.kind != "element" and (self.role is not None or self.value is not None):
            raise ValueError("role and value apply only to element checks")
        return self


class Decision(Record):
    kind: Literal["act", "observe", "finish", "ask"]
    summary: str = Field(min_length=1, max_length=2000)
    plan: list[str] = Field(default_factory=list, max_length=8)
    surface_id: str | None = None
    action: Action | None = None
    checks: list[Check] = Field(
        max_length=12, description="Required. Postconditions for act/finish; [] for ask/observe."
    )

    @model_validator(mode="after")
    def validate_decision(self):
        if self.kind == "act":
            if not self.surface_id or self.action is None or not self.checks:
                raise ValueError("act requires surface_id, action, and postcondition checks")
            if not any(c.surface_id == self.surface_id for c in self.checks):
                raise ValueError("act must check its own surface")
        elif self.action is not None:
            raise ValueError("only act may contain an action")
        if self.kind == "finish" and not self.checks:
            raise ValueError("finish requires observable success checks")
        return self


class Receipt(Record):
    status: Literal["dispatched", "rejected", "unknown"]
    detail: str


class InteractionError(Exception):
    def __init__(self, detail: str, *, unknown: bool = False):
        super().__init__(detail)
        self.unknown = unknown


class Driver(Protocol):
    async def discover(self) -> list[Surface]: ...
    async def acquire(self, surface: Surface, owner: str) -> None: ...
    async def observe(self, surface: Surface, owner: str) -> Observation: ...
    async def act(
        self, surface: Surface, owner: str, observation_id: str, action: Action
    ) -> Receipt: ...
    async def release(self, surface: Surface, owner: str) -> None: ...


class Model(Protocol):
    async def decide(self, context: dict) -> tuple[Decision, dict]: ...

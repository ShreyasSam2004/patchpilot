"""Tool interface.

A tool is the only way the agent affects the world. Every tool declares a
Pydantic argument model (which doubles as its JSON schema for the model API),
a timeout, and an async ``run``. Tools return a :class:`ToolResult` rather than
raising for expected failures, so a bad argument becomes an *observation* the
agent can react to instead of aborting the run.

The same interface is implemented by native tools and by MCP-discovered tools,
so the agent cannot tell — and does not need to know — where a tool came from.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from ..domain.enums import ToolOrigin
from ..errors import ToolError, ToolValidationError

ArgsT = TypeVar("ArgsT", bound=BaseModel)


@dataclass(slots=True)
class ToolResult:
    """What the agent sees after a tool runs."""

    ok: bool
    content: str
    """Text handed back to the model. Already truncated to the output budget."""

    summary: str = ""
    """One-line description for the UI timeline (never model reasoning)."""

    data: dict[str, Any] = field(default_factory=dict)
    """Structured payload for the API/UI — file lists, test counts, diffs."""

    error_code: str | None = None
    duration_ms: int = 0
    truncated: bool = False

    @classmethod
    def failure(cls, message: str, *, code: str = "tool_error", **data: Any) -> ToolResult:
        return cls(
            ok=False,
            content=f"ERROR: {message}",
            summary=message[:200],
            error_code=code,
            data=data,
        )

    @classmethod
    def success(cls, content: str, summary: str = "", **data: Any) -> ToolResult:
        return cls(ok=True, content=content, summary=summary or content[:200], data=data)


class ToolContext:
    """Everything a tool is allowed to touch.

    Passed explicitly rather than reached for via globals, so tools are trivially
    testable and cannot acquire capabilities they were not given.
    """

    def __init__(
        self,
        *,
        workspace: Any,
        sandbox: Any,
        settings: Any,
        emit: Any = None,
        state: Any = None,
    ) -> None:
        self.workspace = workspace
        self.sandbox = sandbox
        self.settings = settings
        self.emit = emit
        self.state = state if state is not None else {}
        self.iteration: int = 0


class Tool(ABC, Generic[ArgsT]):
    """Base class for every tool the agent can call."""

    name: ClassVar[str]
    description: ClassVar[str]
    args_model: ClassVar[type[BaseModel]]
    origin: ClassVar[ToolOrigin] = ToolOrigin.NATIVE
    timeout_s: ClassVar[float] = 60.0
    mutating: ClassVar[bool] = False
    """True if the tool can change the workspace — used by the anti-loop logic."""

    @abstractmethod
    async def execute(self, ctx: ToolContext, args: ArgsT) -> ToolResult:
        """Run the tool. Raise :class:`ToolError` for recoverable problems."""

    # -- plumbing ------------------------------------------------------------
    def parse_args(self, raw: dict[str, Any]) -> BaseModel:
        try:
            return self.args_model.model_validate(raw or {})
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
                for e in exc.errors()[:6]
            )
            raise ToolValidationError(
                f"invalid arguments for {self.name}: {problems}. "
                f"Expected schema: {self.json_schema()}"
            ) from exc

    async def run(self, ctx: ToolContext, raw_args: dict[str, Any]) -> ToolResult:
        """Validate, execute, time and normalise errors."""
        started = time.perf_counter()
        try:
            args = self.parse_args(raw_args)
            result = await self.execute(ctx, args)  # type: ignore[arg-type]
        except ToolError as exc:
            result = ToolResult.failure(str(exc), code=exc.code)
        except Exception as exc:
            result = ToolResult.failure(
                f"{type(exc).__name__}: {exc}", code="tool_exception"
            )
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result

    def json_schema(self) -> dict[str, Any]:
        """JSON schema for the model's tool-calling API."""
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return schema

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.json_schema(),
            origin=self.origin,
            mutating=self.mutating,
        )


@dataclass(slots=True)
class ToolSpec:
    """Serialisable description handed to a model provider and to the UI."""

    name: str
    description: str
    input_schema: dict[str, Any]
    origin: ToolOrigin = ToolOrigin.NATIVE
    mutating: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "origin": str(self.origin),
            "mutating": self.mutating,
        }


class EmptyArgs(BaseModel):
    """For tools that take no arguments."""

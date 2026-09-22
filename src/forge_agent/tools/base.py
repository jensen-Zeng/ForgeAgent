"""Tool protocol, common execution lifecycle, and registry."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import ClassVar, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from forge_agent.domain import ToolCall, ToolResult, ToolSchema


class ToolExecution(BaseModel):
    """Successful internal result before it is attached to a tool call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output: str = ""
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ToolError(RuntimeError):
    """Expected tool failure carrying a stable machine-readable code."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        metadata: dict[str, JsonValue] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.metadata = metadata or {}


class Tool(Protocol):
    """Interface consumed by the registry and future Agent Runtime."""

    @property
    def schema(self) -> ToolSchema:
        """Return the provider-independent tool description."""

    def execute(self, call: ToolCall) -> ToolResult:
        """Validate and execute one tool call without leaking exceptions."""


class BaseTool[ParametersT: BaseModel](ABC):
    """Reusable strict parameter validation and error conversion."""

    name: ClassVar[str]
    description: ClassVar[str]
    parameters_type: ClassVar[type[BaseModel]]

    @property
    def schema(self) -> ToolSchema:
        """Generate a JSON Schema directly from the parameter model."""
        parameters = cast(dict[str, JsonValue], self.parameters_type.model_json_schema())
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=parameters,
        )

    def execute(self, call: ToolCall) -> ToolResult:
        """Run a call and convert all failures to a structured result."""
        if call.name != self.name:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                error=f"Tool name mismatch: expected {self.name!r}, got {call.name!r}",
                metadata={"error_code": "tool_name_mismatch"},
            )
        try:
            parameters = self.parameters_type.model_validate(call.arguments)
        except ValidationError as error:
            details = error.errors(include_input=False, include_url=False)
            return ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                error=f"Invalid tool arguments: {json.dumps(details, ensure_ascii=False)}",
                metadata={"error_code": "invalid_arguments"},
            )
        try:
            execution = self.run(cast(ParametersT, parameters))
        except ToolError as error:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                error=str(error),
                metadata={"error_code": error.code, **error.metadata},
            )
        except Exception as error:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                error=f"Tool execution failed: {type(error).__name__}",
                metadata={"error_code": "tool_execution_error"},
            )
        return ToolResult(
            call_id=call.id,
            name=call.name,
            ok=True,
            output=execution.output,
            metadata=execution.metadata,
        )

    @abstractmethod
    def run(self, parameters: ParametersT) -> ToolExecution:
        """Execute already validated parameters."""


class ToolRegistry:
    """Discover and invoke tools by their stable schema names."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        """Register a tool and reject ambiguous duplicate names."""
        name = tool.schema.name
        if name in self._tools:
            raise ValueError(f"tool {name!r} is already registered")
        self._tools[name] = tool

    @property
    def schemas(self) -> tuple[ToolSchema, ...]:
        """Return schemas in deterministic name order."""
        return tuple(self._tools[name].schema for name in sorted(self._tools))

    def execute(self, call: ToolCall) -> ToolResult:
        """Dispatch one call or return a structured unknown-tool failure."""
        tool = self._tools.get(call.name)
        if tool is None:
            available = sorted(self._tools)
            available_json = cast(list[JsonValue], available)
            return ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                error=f"Unknown tool {call.name!r}",
                metadata={
                    "error_code": "unknown_tool",
                    "available_tools": available_json,
                },
            )
        return tool.execute(call)

    async def execute_async(self, call: ToolCall) -> ToolResult:
        """Dispatch async-capable RLM tools while preserving all sync tools."""
        tool = self._tools.get(call.name)
        if tool is None:
            return self.execute(call)
        async_execute = getattr(tool, "execute_async", None)
        if async_execute is not None:
            result = await async_execute(call)
            if not isinstance(result, ToolResult):
                raise TypeError("async tool must return ToolResult")
            return result
        return tool.execute(call)

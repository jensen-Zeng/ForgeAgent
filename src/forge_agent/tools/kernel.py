"""Asynchronous, persistent Python execution tool for trusted RLM sessions."""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from forge_agent.domain import ToolCall, ToolResult, ToolSchema
from forge_agent.kernel import KernelManager


class ExecutePythonParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=1_000_000)
    timeout_seconds: float = Field(default=120.0, gt=0, le=900)


class ExecutePythonTool:
    """Expose one session's trusted IPython namespace to the model."""

    def __init__(self, manager: KernelManager) -> None:
        self.manager = manager

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="execute_python",
            description=(
                "Execute Python in the persistent trusted IPython kernel. The namespace "
                "persists across calls. This has the current user's workspace permissions."
            ),
            parameters=ExecutePythonParameters.model_json_schema(),
        )

    def execute(self, call: ToolCall) -> ToolResult:
        return ToolResult(
            call_id=call.id,
            name=call.name,
            ok=False,
            error="execute_python requires the asynchronous RLM runtime",
            metadata={"error_code": "async_tool_required"},
        )

    async def execute_async(self, call: ToolCall) -> ToolResult:
        if call.name != self.schema.name:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                error="Tool name mismatch",
                metadata={"error_code": "tool_name_mismatch"},
            )
        try:
            parameters = ExecutePythonParameters.model_validate(call.arguments)
        except ValidationError as error:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                error=json.dumps(error.errors(include_input=False), ensure_ascii=False),
                metadata={"error_code": "invalid_arguments"},
            )
        result = await self.manager.execute(
            parameters.code, timeout_seconds=parameters.timeout_seconds
        )
        output_parts = []
        if result.stdout:
            output_parts.append("stdout:\n" + result.stdout)
        if result.stderr:
            output_parts.append("stderr:\n" + result.stderr)
        if result.result is not None:
            output_parts.append("result:\n" + result.result)
        if result.traceback:
            output_parts.append("traceback:\n" + "\n".join(result.traceback))
        if result.artifact:
            output_parts.append("artifact: " + result.artifact)
        return ToolResult(
            call_id=call.id,
            name=call.name,
            ok=result.ok,
            output="\n".join(output_parts) if result.ok else "",
            error=None if result.ok else "Kernel execution failed",
            metadata={
                "timed_out": result.timed_out,
                "truncated": result.truncated,
                "artifact": result.artifact,
                "restarted": result.restarted,
                **({} if result.ok else {"error_code": "kernel_execution_failed"}),
            },
        )

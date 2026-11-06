"""Failure evidence must survive the kernel and provider context boundaries."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from forge_agent.domain import Message, Role, ToolCall, ToolResult, ToolResultPart
from forge_agent.kernel import KernelManager
from forge_agent.models.wire import anthropic_messages, openai_messages
from forge_agent.tools import ExecutePythonTool


@pytest.mark.parametrize(
    "output", ["", "stdout:\ncheckpoint reached\ntraceback:\nNameError: missing"]
)
def test_failed_tool_evidence_reaches_both_provider_formats(output: str) -> None:
    result = ToolResult(
        call_id="python-1",
        name="execute_python",
        ok=False,
        output=output,
        error="Kernel execution failed",
    )
    messages = (Message(role=Role.TOOL, content=(ToolResultPart(tool_result=result),)),)
    openai = openai_messages(messages)
    _, anthropic = anthropic_messages(messages)
    openai_content = openai[0]["content"]
    anthropic_blocks = anthropic[0]["content"]
    assert isinstance(anthropic_blocks, list)
    block = anthropic_blocks[0]
    assert isinstance(block, dict)
    assert block["is_error"] is True
    assert openai[0]["tool_call_id"] == block["tool_use_id"] == "python-1"
    for content in (openai_content, block["content"]):
        assert isinstance(content, str)
        assert "Kernel execution failed" in content
        if output:
            assert output in content
        else:
            assert content == "Kernel execution failed"


async def test_failed_python_tool_retains_stdout_and_traceback(tmp_path: Path) -> None:
    session_id = uuid4()
    manager = KernelManager(
        workspace=tmp_path,
        session_id=session_id,
        root_session_id=session_id,
        artifacts=tmp_path / "artifacts",
    )
    tool = ExecutePythonTool(manager)
    try:
        result = await tool.execute_async(
            ToolCall(
                id="python-1",
                name="execute_python",
                arguments={"code": "print('checkpoint reached')\nmissing_value"},
            )
        )
    finally:
        await manager.shutdown()

    assert result.ok is False
    assert result.error == "Kernel execution failed"
    assert "checkpoint reached" in result.output
    assert "NameError" in result.output
    assert "missing_value" in result.output

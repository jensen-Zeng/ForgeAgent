"""Failure evidence must survive the kernel and provider context boundaries."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from forge_agent.compaction import CompactionPolicy, ContextBuilder
from forge_agent.domain import (
    Message,
    ModelResponse,
    Role,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResult,
    ToolResultPart,
)
from forge_agent.kernel import KernelManager
from forge_agent.models import FakeModelProvider
from forge_agent.models.wire import anthropic_messages, openai_messages
from forge_agent.session import SessionStore
from forge_agent.tools import ExecutePythonTool


def test_compaction_can_be_imported_before_session_runtime() -> None:
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", "from forge_agent.compaction import ContextBuilder"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


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


async def test_compaction_receives_error_details_alongside_failed_output(tmp_path: Path) -> None:
    provider = FakeModelProvider([ModelResponse(content=(TextPart(text="summary"),))])
    store = SessionStore.create(tmp_path)
    builder = ContextBuilder(
        store=store,
        provider=provider,
        policy=CompactionPolicy(window_tokens=100, reserve_tokens=20, keep_recent_tokens=10),
    )
    messages = (
        Message(role=Role.SYSTEM, content=(TextPart(text="rules"),)),
        Message(role=Role.USER, content=(TextPart(text="old context " * 100),)),
        Message(
            role=Role.ASSISTANT,
            content=(ToolCallPart(tool_call=ToolCall(id="c1", name="execute_python")),),
        ),
        Message(
            role=Role.TOOL,
            content=(
                ToolResultPart(
                    tool_result=ToolResult(
                        call_id="c1",
                        name="execute_python",
                        ok=False,
                        output="checkpoint reached",
                        error="NameError: missing_value is not defined",
                    )
                ),
            ),
        ),
        Message(role=Role.USER, content=(TextPart(text="continue"),)),
    )

    await builder.prepare(messages)

    transcript = provider.requests[0].messages[1].content[0]
    assert isinstance(transcript, TextPart)
    assert "checkpoint reached" in transcript.text
    assert "NameError: missing_value is not defined" in transcript.text

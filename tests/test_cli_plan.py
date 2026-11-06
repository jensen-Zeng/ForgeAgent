"""End-to-end tests for the read-only planning CLI mode."""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from forge_agent.cli import app
from forge_agent.domain import (
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResultPart,
)
from forge_agent.models import FakeModelProvider, ProviderResponseError

runner = CliRunner()


class CountingFakeProvider(FakeModelProvider):
    def __init__(self) -> None:
        super().__init__((ModelResponse(content=(TextPart(text="Done"),)),))
        self.generate_calls = 0
        self.stream_calls = 0

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.generate_calls += 1
        return await super().generate(request)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        self.stream_calls += 1
        async for event in super().stream(request):
            yield event


class FailingFakeProvider(FakeModelProvider):
    def __init__(self) -> None:
        super().__init__(())

    async def generate(self, request: ModelRequest) -> ModelResponse:
        raise ProviderResponseError("scripted provider failure")


def _call(name: str, call_id: str) -> ToolCallPart:
    return ToolCallPart(tool_call=ToolCall(id=call_id, name=name, arguments={}))


def test_plan_is_read_only_and_uses_config_trace_and_planning_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "src.py"
    original = "print('unchanged')\n"
    source.write_text(original, encoding="utf-8")
    config_path = tmp_path / "custom.toml"
    config_path.write_text('[model]\nname = "from-config"\n', encoding="utf-8")
    trace_path = tmp_path / "outside-trace.jsonl"
    provider = FakeModelProvider(
        (
            ModelResponse(content=(_call("apply_patch", "write-attempt"),)),
            ModelResponse(content=(_call("run_command", "command-attempt"),)),
            ModelResponse(content=(TextPart(text="Plan: inspect src.py, then add tests."),)),
        )
    )
    monkeypatch.setattr("forge_agent.cli._create_live_provider", lambda settings: provider)
    monkeypatch.setattr(
        "forge_agent.cli._select_run_workspace",
        lambda *args, **kwargs: pytest.fail("plan must not create or select a worktree"),
    )

    result = runner.invoke(
        app,
        [
            "plan",
            "Review the project",
            "--workspace",
            str(tmp_path),
            "--config",
            str(config_path),
            "--trace",
            str(trace_path),
            "--stream",
        ],
        env={
            "FORGE_MODEL_PROVIDER": "openai",
            "FORGE_MODEL_BASE_URL": "https://api.example.test/v1",
            "FORGE_MODEL_API_KEY": "test-key",
        },
    )

    assert result.exit_code == 0, result.stdout
    assert "read-only planning mode" in result.stdout
    assert "Plan: inspect src.py" in result.stdout
    assert provider.closed is True
    assert len(provider.requests) == 3
    first_request = provider.requests[0]
    system_content = first_request.messages[0].content[0]
    assert isinstance(system_content, TextPart)
    system_prompt = system_content.text
    assert "read-only planning mode" in system_prompt
    assert "Do not edit files" in system_prompt
    assert {schema.name for schema in first_request.tools} == {
        "list_files",
        "read_file",
        "search_code",
        "git_diff",
        "git_status",
    }
    second_request_results = [
        part.tool_result
        for message in provider.requests[1].messages
        if message.role.value == "tool"
        for part in message.content
        if isinstance(part, ToolResultPart)
    ]
    assert second_request_results[0].metadata["error_code"] == "unknown_tool"
    third_request_results = [
        part.tool_result
        for message in provider.requests[2].messages
        if message.role.value == "tool"
        for part in message.content
        if isinstance(part, ToolResultPart)
    ]
    assert third_request_results[0].metadata["error_code"] == "unknown_tool"
    assert source.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".git").exists()
    assert trace_path.is_file()
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["kind"] == "run_completed"


def test_run_defaults_to_generate_and_stream_is_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_workspace = tmp_path / "default"
    streaming_workspace = tmp_path / "streaming"
    default_workspace.mkdir()
    streaming_workspace.mkdir()
    default_provider = CountingFakeProvider()
    streaming_provider = CountingFakeProvider()
    providers = iter((default_provider, streaming_provider))
    monkeypatch.setattr("forge_agent.cli._create_live_provider", lambda settings: next(providers))
    model_env = {
        "FORGE_MODEL_PROVIDER": "openai",
        "FORGE_MODEL_BASE_URL": "https://api.example.test/v1",
        "FORGE_MODEL_API_KEY": "test-key",
    }

    default_result = runner.invoke(
        app,
        ["run", "Inspect", "--in-place", "--workspace", str(default_workspace)],
        env=model_env,
    )
    streaming_result = runner.invoke(
        app,
        [
            "run",
            "Inspect",
            "--in-place",
            "--workspace",
            str(streaming_workspace),
            "--stream",
        ],
        env=model_env,
    )

    assert default_result.exit_code == 0, default_result.stdout
    assert streaming_result.exit_code == 0, streaming_result.stdout
    assert (default_provider.generate_calls, default_provider.stream_calls) == (1, 0)
    assert (streaming_provider.generate_calls, streaming_provider.stream_calls) == (0, 1)
    assert default_provider.closed is True
    assert streaming_provider.closed is True


def test_provider_is_closed_after_default_generate_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FailingFakeProvider()
    monkeypatch.setattr("forge_agent.cli._create_live_provider", lambda settings: provider)
    result = runner.invoke(
        app,
        ["run", "Inspect", "--in-place", "--workspace", str(tmp_path)],
        env={
            "FORGE_MODEL_PROVIDER": "openai",
            "FORGE_MODEL_BASE_URL": "https://api.example.test/v1",
            "FORGE_MODEL_API_KEY": "test-key",
        },
    )

    assert result.exit_code == 1
    assert "provider_error" in result.stdout
    assert provider.closed is True

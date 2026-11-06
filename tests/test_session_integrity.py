"""Recovery must preserve journals until an explicit tail-only repair."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from forge_agent.cli import app
from forge_agent.domain import RunConfig
from forge_agent.engine import LoopAgentEngine
from forge_agent.models import FakeModelProvider
from forge_agent.session import InputEnvelope, SessionRuntime, SessionStore
from forge_agent.session.store import SessionCorruptError
from forge_agent.tools import ToolRegistry


@pytest.mark.parametrize("started", [False, True], ids=["pending", "uncertain"])
def test_recovery_requires_tail_repair_before_appending(tmp_path: Path, started: bool) -> None:
    store = SessionStore.create(tmp_path)
    envelope = InputEnvelope(text="preserve this task")
    store.append(
        "input",
        {"envelope": envelope.model_dump(mode="json"), "receipt_sequence": 1},
        entry_id=envelope.id,
    )
    if started:
        store.append("input_started", {"input_id": str(envelope.id)})
    with store.path.open("ab") as stream:
        stream.write(b'{"partial":')
    original = store.path.read_bytes()

    def recover() -> SessionRuntime:
        return SessionRuntime(
            store=store,
            engine=LoopAgentEngine(FakeModelProvider([])),
            tools=ToolRegistry(),
            base_config=RunConfig(task="resume", workspace=tmp_path),
        )

    with pytest.raises(SessionCorruptError, match="repair"):
        recover()
    assert store.path.read_bytes() == original

    assert store.repair_tail(truncate_tail=True).valid
    recover()
    entries = store.replay()
    assert entries[0].id == envelope.id
    assert entries[-1].type == "recovery_marker"
    assert entries[-1].payload["uncertain_inputs"] == int(started)
    assert entries[-1].payload["unconsumed_inputs"] == int(not started)


@pytest.mark.parametrize("bad_line", [b'{"broken":\n', b"\xff\n"])
def test_interior_corruption_cannot_be_truncated_as_a_tail(tmp_path: Path, bad_line: bytes) -> None:
    store = SessionStore.create(tmp_path)
    store.append("before", {"text": "保留"})
    with store.path.open("ab") as stream:
        stream.write(bad_line)
    store.append("after", {"text": "also preserve"})
    original = store.path.read_bytes()

    with pytest.raises(SessionCorruptError, match=r"before.*final"):
        store.replay()
    with pytest.raises(SessionCorruptError, match=r"before.*final"):
        store.repair_tail(truncate_tail=True)
    assert store.path.read_bytes() == original

    result = CliRunner().invoke(
        app,
        [
            "session",
            "repair",
            str(store.header.session_id),
            "--workspace",
            str(tmp_path),
            "--truncate-tail",
        ],
    )
    assert result.exit_code == 2
    assert "Unable to repair session" in result.stdout
    assert store.path.read_bytes() == original


def test_invalid_final_line_remains_explicitly_repairable(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path)
    entry = store.append("retained", {"text": "中文🙂"})
    with store.path.open("ab") as stream:
        stream.write(b'{"broken":\n')
    assert not store.inspect_tail().valid
    assert store.repair_tail(truncate_tail=True).valid
    assert store.replay() == (entry,)

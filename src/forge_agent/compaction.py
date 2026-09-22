"""Persistent structured context compaction at safe message boundaries."""

from __future__ import annotations

from dataclasses import dataclass

from forge_agent.domain import Message, ModelRequest, Role, TextPart, ToolCallPart, ToolResultPart
from forge_agent.models import ModelProvider
from forge_agent.session.store import SessionStore

_SECTIONS = (
    "Goal",
    "Constraints",
    "Completed",
    "In Progress",
    "Key Decisions",
    "Failures and Evidence",
    "Next Steps",
    "Critical Files and Commands",
)


class CompactionFailedError(RuntimeError):
    """Compaction failed after the context had already overflowed."""


@dataclass(frozen=True)
class CompactionPolicy:
    enabled: bool = True
    window_tokens: int = 32_000
    reserve_tokens: int = 8_192
    keep_recent_tokens: int = 12_000
    max_tool_result_chars: int = 2_000


class ContextBuilder:
    """Build model context and persist summaries without splitting tool pairs."""

    def __init__(
        self,
        *,
        store: SessionStore,
        provider: ModelProvider,
        policy: CompactionPolicy | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.policy = policy or CompactionPolicy()

    async def prepare(self, messages: tuple[Message, ...]) -> tuple[Message, ...]:
        if not self.policy.enabled:
            return messages
        estimated = estimate_messages(messages)
        threshold = self.policy.window_tokens - self.policy.reserve_tokens
        if estimated < threshold:
            return messages
        recent = _recent_messages(messages, self.policy.keep_recent_tokens)
        old = messages[: len(messages) - len(recent)]
        if not old:
            return messages
        self.store.append(
            "compaction_started",
            {"before_tokens": estimated, "kept_messages": len(recent)},
        )
        try:
            summary = await self._summarize(old)
        except Exception as error:
            self.store.append(
                "compaction_failed",
                {"before_tokens": estimated, "error_type": type(error).__name__},
            )
            if estimated >= self.policy.window_tokens:
                raise CompactionFailedError(
                    "context overflow and summary generation failed"
                ) from error
            return messages
        first_kept = _first_message_entry_id(self.store, recent[0] if recent else None)
        self.store.append(
            "compaction",
            {
                "summary": summary,
                "before_tokens": estimated,
                "first_kept_entry_id": first_kept,
                "config": {
                    "window_tokens": self.policy.window_tokens,
                    "reserve_tokens": self.policy.reserve_tokens,
                    "keep_recent_tokens": self.policy.keep_recent_tokens,
                    "max_tool_result_chars": self.policy.max_tool_result_chars,
                },
            },
        )
        system = messages[0] if messages and messages[0].role is Role.SYSTEM else None
        summary_message = Message(
            role=Role.SYSTEM,
            content=(TextPart(text="Persistent session summary:\n" + summary),),
        )
        prefix = (system, summary_message) if system is not None else (summary_message,)
        return (*prefix, *recent)

    async def _summarize(self, messages: tuple[Message, ...]) -> str:
        headings = "\n".join(f"## {section}" for section in _SECTIONS)
        transcript = "\n\n".join(
            _render_message(message, max_tool_result_chars=self.policy.max_tool_result_chars)
            for message in messages
        )
        request = ModelRequest(
            messages=(
                Message(
                    role=Role.SYSTEM,
                    content=(
                        TextPart(
                            text=(
                                "Summarize the prior coding-agent transcript using every exact "
                                "heading below. Preserve facts, file paths, commands, failures, "
                                "constraints and unfinished work. Do not claim completion "
                                "without evidence.\n" + headings
                            )
                        ),
                    ),
                ),
                Message(role=Role.USER, content=(TextPart(text=transcript),)),
            )
        )
        response = await self.provider.generate(request)
        summary = "\n".join(part.text for part in response.text_parts).strip()
        if not summary:
            raise ValueError("empty compaction summary")
        return summary


def estimate_messages(messages: tuple[Message, ...]) -> int:
    chars = sum(len(_render_message(message)) for message in messages)
    return max((chars + 3) // 4, len(messages))


def _recent_messages(messages: tuple[Message, ...], token_budget: int) -> tuple[Message, ...]:
    selected: list[Message] = []
    used = 0
    for message in reversed(messages):
        cost = max((len(_render_message(message)) + 3) // 4, 1)
        if selected and used + cost > token_budget:
            break
        selected.append(message)
        used += cost
    selected.reverse()
    start = len(messages) - len(selected)
    if selected and selected[0].role is Role.TOOL and start > 0:
        previous = messages[start - 1]
        if any(isinstance(part, ToolCallPart) for part in previous.content):
            selected.insert(0, previous)
    return tuple(selected)


def _render_message(message: Message, *, max_tool_result_chars: int | None = None) -> str:
    parts: list[str] = []
    for part in message.content:
        if isinstance(part, TextPart):
            parts.append(part.text)
        elif isinstance(part, ToolCallPart):
            parts.append(
                f"TOOL_CALL {part.tool_call.name} {part.tool_call.id} {part.tool_call.arguments}"
            )
        elif isinstance(part, ToolResultPart):
            result = part.tool_result
            output = result.output
            if max_tool_result_chars is not None and len(output) > max_tool_result_chars:
                output = output[:max_tool_result_chars] + "...[truncated for compaction]"
            parts.append(f"TOOL_RESULT {result.name} {result.call_id} ok={result.ok} {output}")
    return f"[{message.role.value}] " + "\n".join(parts)


def _first_message_entry_id(store: SessionStore, message: Message | None) -> str | None:
    if message is None:
        return None
    dump = message.model_dump(mode="json")
    for entry in reversed(store.replay()):
        if entry.type == "message" and entry.payload.get("message") == dump:
            return str(entry.id)
    return None

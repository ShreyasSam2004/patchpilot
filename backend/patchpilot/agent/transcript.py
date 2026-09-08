"""Conversation transcript with compaction.

A 30-iteration run with full file reads and test output would blow past any
context window. The transcript keeps recent turns verbatim — that is where the
agent's working memory lives — and replaces older *tool results* with one-line
summaries, preserving the shape of the conversation (every ``tool_use`` still
has a matching ``tool_result``) while shedding the bulk.

Assistant text and tool *calls* are never compacted: the record of what the
agent decided stays intact for the whole run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..providers.base import Message, ToolCall, ToolCallResult


@dataclass
class Transcript:
    """The message list handed to the provider each turn."""

    keep_verbatim_turns: int = 6
    """How many of the most recent tool-result messages stay in full."""
    max_result_chars: int = 24_000
    """Hard cap on any single tool result, applied on the way in."""
    summary_chars: int = 220
    """Length of the replacement text for a compacted result."""

    messages: list[Message] = field(default_factory=list)

    # ------------------------------------------------------------- building
    def add_user(self, content: str) -> None:
        if content.strip():
            self.messages.append(Message(role="user", content=content))

    def add_assistant(self, content: str, tool_calls: list[ToolCall]) -> None:
        message = Message(role="assistant", content=content, tool_calls=list(tool_calls))
        if not message.is_empty():
            self.messages.append(message)

    def add_tool_results(self, results: list[ToolCallResult]) -> None:
        if not results:
            return
        capped = [
            ToolCallResult(
                id=result.id,
                name=result.name,
                content=_cap(result.content, self.max_result_chars),
                is_error=result.is_error,
            )
            for result in results
        ]
        self.messages.append(Message(role="tool", tool_results=capped))

    # ------------------------------------------------------------ compaction
    def render(self) -> list[Message]:
        """The message list to send, with older tool results compacted."""
        tool_indices = [i for i, m in enumerate(self.messages) if m.role == "tool"]
        if len(tool_indices) <= self.keep_verbatim_turns:
            return self.messages

        compact_before = tool_indices[-self.keep_verbatim_turns]
        rendered: list[Message] = []
        for index, message in enumerate(self.messages):
            if message.role == "tool" and index < compact_before:
                rendered.append(
                    Message(
                        role="tool",
                        tool_results=[
                            ToolCallResult(
                                id=result.id,
                                name=result.name,
                                content=_summarize(result.content, self.summary_chars),
                                is_error=result.is_error,
                            )
                            for result in message.tool_results
                        ],
                    )
                )
            else:
                rendered.append(message)
        return rendered

    # -------------------------------------------------------------- metrics
    def approx_tokens(self) -> int:
        """Rough size estimate (~4 chars/token) for logging and budgeting."""
        total = 0
        for message in self.render():
            total += len(message.content)
            for call in message.tool_calls:
                total += len(str(call.arguments)) + len(call.name)
            for result in message.tool_results:
                total += len(result.content)
        return total // 4

    def __len__(self) -> int:
        return len(self.messages)


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return f"{text[:head]}\n...[{len(text) - limit} characters omitted]...\n{text[-tail:]}"


def _summarize(text: str, limit: int) -> str:
    """One-line stand-in for an older tool result."""
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    first_line = stripped.splitlines()[0][:limit]
    return f"{first_line}\n...[earlier result compacted; {len(stripped)} characters]"

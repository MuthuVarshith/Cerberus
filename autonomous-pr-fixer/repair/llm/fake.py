"""
Deterministic fake model for tests and the scripted demo. It costs nothing and never
touches the network.

A script is a list of steps. Each step is either a `ModelResponse` or a callable
`(messages) -> ModelResponse`, so a step can react to the previous tool results or to
Cerberus feedback. A step that is an Exception instance is raised as an LLMError.
"""
from __future__ import annotations

import itertools
from typing import Callable, List, Sequence, Union

from repair.llm.provider import LLMError, Message, ModelResponse, ToolCall, ToolSpec, Usage

Step = Union[ModelResponse, Callable[[List[Message]], ModelResponse], Exception]

_ids = itertools.count(1)


def call(name: str, **arguments) -> ToolCall:
    return ToolCall(id=f"fake_{next(_ids)}", name=name, arguments=arguments)


def respond(*calls: ToolCall, text: str = "", input_tokens: int = 1000, output_tokens: int = 200) -> ModelResponse:
    return ModelResponse(text=text, tool_calls=list(calls), usage=Usage(input_tokens, output_tokens),
                         stop_reason="tool_use" if calls else "end_turn", model="")


def last_tool_results(messages: List[Message]) -> List[str]:
    for m in reversed(messages):
        if m.tool_results:
            return [r.content for r in m.tool_results]
    return []


class FakeChatModel:
    def __init__(self, steps: Sequence[Step], model: str = "fake-model"):
        self.model = model
        self.steps = list(steps)
        self.calls = 0
        self.seen_tools: List[str] = []

    def complete(self, system: str, messages: List[Message], tools: List[ToolSpec],
                 max_tokens: int) -> ModelResponse:
        self.seen_tools = [t.name for t in tools]
        if self.calls >= len(self.steps):
            raise LLMError("FakeChatModel script exhausted")
        step = self.steps[self.calls]
        self.calls += 1
        if isinstance(step, Exception):
            raise LLMError(str(step))
        resp = step(messages) if callable(step) else step
        resp.model = resp.model or self.model
        return resp

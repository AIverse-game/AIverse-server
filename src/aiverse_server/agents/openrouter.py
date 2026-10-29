"""A brain that asks a language model through OpenRouter, see decision 0003 in the client
repository.

Each round is a conversation of its own: the system prompt gives the rules (`rules.py`), the
first message the observation and a summary of the previous round, and the model then calls
tools, sees their results and calls more, until it replies without a tool call or
`MAX_STEPS` requests have gone by. The model's own notes carry anything further between
rounds.

The API key comes from the caller, read from the environment by `__main__.py`; it never goes
into a record or a log. Every request counts towards `request_limit`; once it is reached the
brain stops asking and the resident just waits. A failed request raises `OpenRouterError`,
which the runner records before asking again later.
"""

from __future__ import annotations

from typing import Any

import httpx

from .observation import Observation, to_json
from .rules import system_prompt
from .tools import ResidentTools, ToolCall, tool_specs

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DEFAULT_MODEL = "nvidia/nemotron-3.5-lightning"

MAX_STEPS = 8
"""Model requests in one round, each of which may call tools."""

REQUEST_LIMIT = 400
"""Model requests one resident may make in a game."""

MAX_TOKENS = 2048
"""Tokens a model may write per request, so one answer cannot run up the bill."""

TIMEOUT = 60.0
"""Seconds to wait for one request."""


class OpenRouterError(Exception):
    pass


class OpenRouterBrain:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        strategy: str | None = None,
        client: httpx.AsyncClient | None = None,
        request_limit: int = REQUEST_LIMIT,
        max_steps: int = MAX_STEPS,
    ) -> None:
        self.model = model
        self.strategy = strategy
        self.requests = 0
        self.request_limit = request_limit
        self.max_steps = max_steps
        self._api_key = api_key
        self._client = client if client is not None else httpx.AsyncClient(timeout=TIMEOUT)
        self._last_round = "This is your first round."

    async def take_turn(self, observation: Observation, tools: ResidentTools) -> str | None:
        if self.requests >= self.request_limit:
            return "request limit reached; waiting"
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": system_prompt(observation.me.name, observation.me.id, self.strategy),
            },
            {
                "role": "user",
                "content": f"{self._last_round}\n\nObservation:\n{to_json(observation)}",
            },
        ]
        remark: str | None = None
        try:
            for _ in range(self.max_steps):
                if self.requests >= self.request_limit:
                    break
                message = await self._complete(messages)
                content = message.get("content")
                remark = content.strip() if isinstance(content, str) and content.strip() else remark
                calls = message.get("tool_calls")
                if not isinstance(calls, list) or not calls:
                    break
                messages.append(
                    {"role": "assistant", "content": content or "", "tool_calls": calls}
                )
                for call in calls:
                    function = call.get("function") if isinstance(call, dict) else None
                    if not isinstance(function, dict):
                        raise OpenRouterError("the model sent a malformed tool call")
                    name = function.get("name")
                    arguments = function.get("arguments")
                    result = tools.call(
                        name if isinstance(name, str) else "",
                        arguments if isinstance(arguments, str | dict) else "",
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(call.get("id", "")),
                            "content": result.to_json(),
                        }
                    )
        finally:
            self._last_round = _summary(tools.calls, remark)
        return remark

    async def _complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """One chat completion; returns the model's message."""
        self.requests += 1
        try:
            response = await self._client.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {self._api_key}", "X-Title": "AIverse"},
                json={
                    "model": self.model,
                    "messages": messages,
                    "tools": tool_specs(),
                    "tool_choice": "auto",
                    "max_tokens": MAX_TOKENS,
                },
            )
        except httpx.HTTPError as error:
            raise OpenRouterError(f"request failed: {type(error).__name__}") from error
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code != 200 or not isinstance(body, dict) or "error" in body:
            detail = body.get("error") if isinstance(body, dict) else None
            message = detail.get("message") if isinstance(detail, dict) else None
            raise OpenRouterError(
                f"HTTP {response.status_code}: {str(message or 'unexpected response')[:300]}"
            )
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise OpenRouterError("the response has no choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise OpenRouterError("the response has no message")
        return message


def _summary(calls: list[ToolCall], remark: str | None) -> str:
    """What the brain did last round, for the next one."""
    if not calls and remark is None:
        return "Last round you did nothing."
    lines = ["Last round:"]
    for call in calls:
        result = call.result
        outcome = "ok" if result.ok else f"refused ({result.code}: {result.message})"
        lines.append(f"- {call.name}({_arguments(call)}) -> {outcome}")
    if remark is not None:
        lines.append(f"Your plan then: {remark[:500]}")
    return "\n".join(lines)


def _arguments(call: ToolCall) -> str:
    return ", ".join(f"{key}={value!r}" for key, value in call.arguments.items())[:300]

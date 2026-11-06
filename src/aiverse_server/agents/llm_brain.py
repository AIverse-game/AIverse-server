"""A brain that asks a language model through an OpenAI-compatible chat completions API, see
decision 0004 in the client repository.

Any provider that speaks that API will do; none is built in. The player gives its base URL,
an API key and a model on the page, and the server's config file may override any of them,
see `server/config.py`; `LLMSettings` carries them and `over` lays one on another.

Each round is a conversation of its own: the system prompt gives the rules (`system_prompt.py`), the
first message the observation and a summary of the previous round, and the model then calls
tools, sees their results and calls more, until it replies without a tool call, its tools
leave the resident walking or in a job, or `MAX_STEPS` requests have gone by. A busy resident
can only talk and take notes, which the model does in the same reply; asking again would
only have it wait out the job in notes, and the runner wakes it when the job ends. When a
free resident's model has called no tool yet and replies with nothing to act on, empty or
cut off at its configured token limit, the round asks once more and raises `LLMError` if
the second reply is no better, so the runner records why and retries soon. The model's notes carry
anything further between rounds.

The API key never goes into a record, a log or an error message, and is left out of the
settings' repr; it must be printable ASCII without spaces, as an HTTP header allows, or it
would be refused by the HTTP client in an error naming it. Every request counts towards
`request_limit`; once it is reached the brain stops asking and the resident just waits. The
game carries a resident's count over to the next brain it hands them to, see `Game`, so
restarting an agent does not reset it. A failed request raises `LLMError`, which the
runner records before asking again later.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from .observation import Observation, to_json
from .system_prompt import system_prompt
from .tool_selection import (
    discover_tools,
    last_request_tool_names,
    selected_tool_names,
    selected_tool_specs,
)
from .tools import ResidentTools, ToolCall

MAX_STEPS = 8
"""Model requests in one round, each of which may call tools."""

REQUEST_LIMIT = 400
"""Model requests one resident may make in a game."""

MAX_TOKENS = 8192
"""Tokens a model may write per request, so one answer cannot run up the bill. A reasoning
model spends most of them thinking before it calls a tool; at 2048 more than half the
rounds of a logged game ended cut off with nothing done."""

ENDGAME_MAX_TOKENS = 8192
"""Per-request output ceiling after the configured early-warning threshold."""

ENDGAME_INSTRUCTION = (
    "Keep reasoning brief and call the next useful tool promptly. Only coins count at the end: "
    "allow time to reach the market and sell carried energy before the clock runs out, and "
    "keep selling while the price is good rather than waiting for the last moment."
)

TIMEOUT = 180.0
"""Seconds to wait for one request."""

LLMSetting = Literal["base_url", "api_key", "model"]


@dataclass(frozen=True)
class LLMSettings:
    """Where and what to ask; any of them may be missing until settings are laid together.
    `base_url` is the API root, such as `https://openrouter.ai/api/v1`, to which
    `/chat/completions` is added."""

    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    model: str | None = None

    def over(self, chosen: LLMSettings) -> LLMSettings:
        """These settings where given, `chosen` for the rest."""
        return LLMSettings(
            base_url=self.base_url if self.base_url is not None else chosen.base_url,
            api_key=self.api_key if self.api_key is not None else chosen.api_key,
            model=self.model if self.model is not None else chosen.model,
        )

    def given(self) -> tuple[LLMSetting, ...]:
        """The settings present, in a fixed order."""
        present: tuple[tuple[LLMSetting, str | None], ...] = (
            ("base_url", self.base_url),
            ("api_key", self.api_key),
            ("model", self.model),
        )
        return tuple(name for name, value in present if value is not None)


NO_SETTINGS = LLMSettings()


class SettingsError(ValueError):
    def __init__(
        self, code: Literal["missing_llm_setting", "invalid_llm_setting"], message: str
    ) -> None:
        super().__init__(message)
        self.code: Literal["missing_llm_setting", "invalid_llm_setting"] = code
        self.message = message


def complete(settings: LLMSettings) -> tuple[str, str, str]:
    """The base URL, API key and model, once settings are laid together. Raises
    `SettingsError` when one is missing or the base URL is not an http(s) URL."""
    base_url, api_key, model = settings.base_url, settings.api_key, settings.model
    if base_url is None or api_key is None or model is None:
        given = settings.given()
        missing = [name for name in ("base_url", "api_key", "model") if name not in given]
        raise SettingsError("missing_llm_setting", f"missing {', '.join(missing)}")
    if not all("!" <= character <= "~" for character in api_key):
        raise SettingsError("invalid_llm_setting", "api_key must be printable ASCII without spaces")
    try:
        parts = urlsplit(base_url)
    except ValueError:
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not parts.netloc:
        raise SettingsError("invalid_llm_setting", "base_url must be an http(s) URL")
    return base_url, api_key, model


class LLMError(Exception):
    pass


class ChatCompletionsBrain:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        strategy: str | None = None,
        client: httpx.AsyncClient | None = None,
        request_limit: int = REQUEST_LIMIT,
        max_steps: int = MAX_STEPS,
        max_tokens: int = MAX_TOKENS,
    ) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.strategy = strategy
        self.requests = 0
        self.request_limit = request_limit
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self._api_key = api_key
        # Only a client made here is closed here; one passed in belongs to the caller.
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient(timeout=TIMEOUT)
        self._last_round = "This is your first round."
        self.outputs: list[str] = []
        self.completions: list[dict[str, Any]] = []
        """One entry per request of the last round: why it stopped and the tokens the API
        counted, so a log shows what a round cost and whether the model ran out of room."""

    async def take_turn(self, observation: Observation, tools: ResidentTools) -> str | None:
        self.outputs = []
        self.completions = []
        if self.requests >= self.request_limit:
            return "request limit reached; waiting"
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": system_prompt(
                    observation.me.name,
                    observation.me.id,
                    self.strategy,
                    tools.game.early_notice_seconds,
                    tools.game.final_notice_seconds,
                ),
            },
            {
                "role": "user",
                "content": f"{self._last_round}\n\nObservation:\n{to_json(observation)}",
            },
        ]
        remark: str | None = None
        retried = False
        endgame_instruction_sent = False
        advertised = selected_tool_names(observation)
        try:
            for step in range(self.max_steps):
                if self.requests >= self.request_limit:
                    break
                remaining = tools.game.remaining()
                if remaining <= tools.game.early_notice_seconds and not endgame_instruction_sent:
                    messages[0]["content"] += (
                        f"\n\nThe game ends within {tools.game.early_notice_seconds / 60:g} "
                        f"minutes. {ENDGAME_INSTRUCTION}"
                    )
                    endgame_instruction_sent = True
                max_tokens = (
                    min(self.max_tokens, ENDGAME_MAX_TOKENS)
                    if remaining <= tools.game.early_notice_seconds
                    else self.max_tokens
                )
                # Discovery needs another model request. When none remains, show all
                # actions usable in this state directly so they stay reachable.
                last_request = step == self.max_steps - 1 or self.requests == self.request_limit - 1
                if last_request:
                    schemas = selected_tool_specs(
                        last_request_tool_names(tools),
                        include_discovery=False,
                    )
                else:
                    schemas = selected_tool_specs(advertised)
                message, finish_reason = await self._complete(messages, max_tokens, schemas)
                content = message.get("content")
                if isinstance(content, str) and content:
                    self.outputs.append(content)
                calls = message.get("tool_calls")
                if not isinstance(calls, list) or not calls:
                    text = content.strip() if isinstance(content, str) else ""
                    # A free resident with nothing done and nothing to act on: an empty reply,
                    # or one cut off. A busy one may well choose to let what they heard pass.
                    if (
                        not tools.calls
                        and not _busy(tools.status())
                        and (not text or finish_reason == "length")
                    ):
                        if retried:
                            raise LLMError(
                                f"the model replied without acting (finish_reason: "
                                f"{finish_reason or 'none'})"
                            )
                        retried = True
                        continue
                    remark = text or None
                    break
                messages.append(
                    {"role": "assistant", "content": content or "", "tool_calls": calls}
                )
                for call in calls:
                    function = call.get("function") if isinstance(call, dict) else None
                    if not isinstance(function, dict):
                        raise LLMError("the model sent a malformed tool call")
                    name = function.get("name")
                    arguments = function.get("arguments")
                    if name == "discover_tools":
                        result_text = discover_tools(
                            arguments if isinstance(arguments, str | dict) else "", advertised
                        )
                    else:
                        result = tools.call(
                            name if isinstance(name, str) else "",
                            arguments if isinstance(arguments, str | dict) else "",
                        )
                        result_text = result.to_json()
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(call.get("id", "")),
                            "content": result_text,
                        }
                    )
                if _busy(tools.status()):
                    remark = (
                        content.strip() if isinstance(content, str) and content.strip() else None
                    )
                    break
            if not tools.calls and remark is None and not _busy(tools.status()):
                raise LLMError("the model used all requests without acting")
        finally:
            self._last_round = _summary(tools.calls, remark)
        return remark

    def credential_redactor(self) -> tuple[int, Callable[[str], str]]:
        """A game can keep filtering this key after the brain and its HTTP client close."""
        api_key = self._api_key
        return len(api_key), lambda text: text.replace(api_key, "***") if api_key else text

    def redact(self, text: str) -> str:
        """Remove a provider-echoed credential before exposing a turn on the page."""
        return text.replace(self._api_key, "***") if self._api_key else text

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _complete(
        self, messages: list[dict[str, Any]], max_tokens: int, schemas: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], str | None]:
        """One chat completion; returns the model's message and why it stopped writing, such
        as "length" when it ran out of tokens."""
        self.requests += 1
        try:
            response = await self._client.post(
                self.url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self.model,
                    "messages": messages,
                    "tools": schemas,
                    "tool_choice": "auto",
                    "max_tokens": max_tokens,
                },
            )
        except httpx.HTTPError as error:
            # Not chained: the HTTP client's message may quote the request, headers included.
            raise LLMError(f"request failed: {type(error).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code != 200 or not isinstance(body, dict) or "error" in body:
            detail = body.get("error") if isinstance(body, dict) else None
            message = detail.get("message") if isinstance(detail, dict) else None
            # A provider may quote the key back; it never goes further.
            detail_text = str(message or "unexpected response")
            if self._api_key:
                detail_text = detail_text.replace(self._api_key, "***")
            raise LLMError(f"HTTP {response.status_code}: {detail_text[:300]}")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise LLMError("the response has no choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise LLMError("the response has no message")
        finish_reason = choices[0].get("finish_reason")
        self.completions.append(_completion_record(finish_reason, body.get("usage")))
        return message, finish_reason if isinstance(finish_reason, str) else None


def _completion_record(finish_reason: object, usage: object) -> dict[str, Any]:
    """What a log keeps of one completion: why it stopped and the token counts the provider
    gives: tokens read (`prompt_tokens`, of which `cached_tokens` came from its cache), tokens
    written (`completion_tokens`, of which `reasoning_tokens` went on reasoning) and the
    `total_tokens`. A count that is missing or not a number is left out."""
    record: dict[str, Any] = {
        "finish_reason": finish_reason if isinstance(finish_reason, str) else None
    }
    if isinstance(usage, dict):
        counts = (
            ("prompt_tokens", usage.get("prompt_tokens")),
            ("cached_tokens", _detail(usage.get("prompt_tokens_details"), "cached_tokens")),
            ("completion_tokens", usage.get("completion_tokens")),
            (
                "reasoning_tokens",
                _detail(usage.get("completion_tokens_details"), "reasoning_tokens"),
            ),
            ("total_tokens", usage.get("total_tokens")),
        )
        for name, count in counts:
            if isinstance(count, int) and not isinstance(count, bool):
                record[name] = count
    return record


def _detail(details: object, key: str) -> object:
    return details.get(key) if isinstance(details, dict) else None


def _busy(status: dict[str, Any]) -> bool:
    """Whether a tool's status shows the resident walking or in a job."""
    busy = ("walking_to", "harvesting_spring", "digging_quarry", "refining_crystal")
    return any(key in status for key in busy)


def _summary(calls: list[ToolCall], remark: str | None) -> str:
    """What the brain did last round, for the next one."""
    if not calls and remark is None:
        return "Last round you did nothing."
    lines = ["Last round:"]
    for call in calls:
        result = call.result
        if result.ok:
            outcome = f"ok ({result.message})" if result.message else "ok"
        else:
            outcome = f"refused ({result.code}: {result.message})"
        lines.append(f"- {call.name}({_arguments(call)}) -> {outcome}")
    if remark is not None:
        lines.append(f"Your plan then: {remark[:500]}")
    return "\n".join(lines)


def _arguments(call: ToolCall) -> str:
    return ", ".join(f"{key}={value!r}" for key, value in call.arguments.items())[:300]

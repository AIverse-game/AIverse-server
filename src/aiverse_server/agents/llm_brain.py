"""A brain that asks a language model through an OpenAI-compatible chat completions API, see
decision 0004 in the client repository.

Any provider that speaks that API will do; none is built in. The player gives its base URL,
an API key and a model on the page, and the server's config file may override any of them,
see `server/config.py`; `LLMSettings` carries them and `over` lays one on another.

Each round is a conversation of its own: the system prompt gives the rules (`system_prompt.py`), the
first message the observation and a summary of the previous round, and the model then calls
tools, sees their results and calls more, until it replies without a tool call or
`MAX_STEPS` requests have gone by. The model's own notes carry anything further between
rounds.

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
from .tools import ResidentTools, ToolCall, tool_specs

MAX_STEPS = 8
"""Model requests in one round, each of which may call tools."""

REQUEST_LIMIT = 400
"""Model requests one resident may make in a game."""

MAX_TOKENS = 2048
"""Tokens a model may write per request, so one answer cannot run up the bill."""

TIMEOUT = 60.0
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
    ) -> None:
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.strategy = strategy
        self.requests = 0
        self.request_limit = request_limit
        self.max_steps = max_steps
        self._api_key = api_key
        # Only a client made here is closed here; one passed in belongs to the caller.
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient(timeout=TIMEOUT)
        self._last_round = "This is your first round."
        self.outputs: list[str] = []

    async def take_turn(self, observation: Observation, tools: ResidentTools) -> str | None:
        self.outputs = []
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
                if isinstance(content, str) and content:
                    self.outputs.append(content)
                calls = message.get("tool_calls")
                if not isinstance(calls, list) or not calls:
                    remark = (
                        content.strip() if isinstance(content, str) and content.strip() else None
                    )
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

    async def _complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """One chat completion; returns the model's message."""
        self.requests += 1
        try:
            response = await self._client.post(
                self.url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self.model,
                    "messages": messages,
                    "tools": tool_specs(),
                    "tool_choice": "auto",
                    "max_tokens": MAX_TOKENS,
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

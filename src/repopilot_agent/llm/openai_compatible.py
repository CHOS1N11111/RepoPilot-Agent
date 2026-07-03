"""OpenAI-compatible chat completions client."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .base import LLMClient, LLMError, LLMMessage

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DISABLE_JSON_MODE_ENV = "REPOPILOT_DISABLE_JSON_MODE"


class OpenAICompatibleClient(LLMClient):
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        json_mode: bool | None = None,
        timeout_seconds: int = 60,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.getenv("REPOPILOT_MODEL") or DEFAULT_MODEL
        self.json_mode = _resolve_json_mode(json_mode)
        self.timeout_seconds = timeout_seconds
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY is not configured.")

    def complete(self, messages: list[LLMMessage]) -> str:
        if not self.json_mode:
            return self._complete_once(messages, json_mode=False)
        try:
            return self._complete_once(messages, json_mode=True)
        except LLMError as exc:
            if not _should_retry_without_json_mode(exc):
                raise
            try:
                return self._complete_once(messages, json_mode=False)
            except LLMError as retry_exc:
                raise LLMError(
                    f"{exc} Retried without response_format and failed: {retry_exc}"
                ) from retry_exc

    def _complete_once(self, messages: list[LLMMessage], json_mode: bool) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
            "temperature": 0.1,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ProviderHTTPError(exc.code, body) from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"LLM request failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise LLMError("LLM request timed out.") from exc
        except json.JSONDecodeError as exc:
            raise ProviderJSONDecodeError(f"LLM response was not valid JSON: {exc}") from exc

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("LLM response did not contain chat completion content.") from exc


class ProviderHTTPError(LLMError):
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"LLM request failed with HTTP {status_code}: {body}")


class ProviderJSONDecodeError(LLMError):
    pass


def _resolve_json_mode(value: bool | None) -> bool:
    if value is not None:
        return value
    raw = os.getenv(DISABLE_JSON_MODE_ENV, "")
    return raw.strip().lower() not in {"1", "true", "yes", "on"}


def _should_retry_without_json_mode(exc: LLMError) -> bool:
    if isinstance(exc, ProviderJSONDecodeError):
        return True
    if not isinstance(exc, ProviderHTTPError):
        return False
    body = exc.body.lower()
    if exc.status_code in {401, 403}:
        return False
    if "response_format" in body or "json_object" in body:
        return True
    return exc.status_code in {400, 404, 422, 500, 502, 503}

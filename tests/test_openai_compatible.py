from __future__ import annotations

import json
import os
import sys
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.llm.base import LLMError, LLMMessage
from repopilot_agent.llm.openai_compatible import OpenAICompatibleClient


class FakeResponse:
    def __init__(self, body: bytes | None = None) -> None:
        self.body = body or json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class OpenAICompatibleClientTests(unittest.TestCase):
    def test_configured_endpoint_url_is_used_exactly(self) -> None:
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            return FakeResponse()

        endpoint = "https://sub2api.example/v1/chat/completions/"
        with patch("urllib.request.urlopen", fake_urlopen):
            client = OpenAICompatibleClient(api_key="test-key", base_url=endpoint, model="test-model")
            client.complete([LLMMessage(role="user", content="Return JSON.")])

        self.assertEqual(captured["url"], endpoint)

    def test_json_mode_adds_response_format(self) -> None:
        captured = {}

        def fake_urlopen(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            client = OpenAICompatibleClient(api_key="test-key", model="test-model", json_mode=True)
            client.complete([LLMMessage(role="user", content="Return JSON.")])

        self.assertEqual(captured["payload"]["response_format"], {"type": "json_object"})

    def test_json_mode_can_be_disabled_for_compatible_providers(self) -> None:
        captured = {}

        def fake_urlopen(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            client = OpenAICompatibleClient(api_key="test-key", model="test-model", json_mode=False)
            client.complete([LLMMessage(role="user", content="Return JSON.")])

        self.assertNotIn("response_format", captured["payload"])

    def test_json_mode_can_be_disabled_by_environment(self) -> None:
        captured = {}

        def fake_urlopen(request, timeout):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        env = {**os.environ, "REPOPILOT_DISABLE_JSON_MODE": "1"}
        with patch.dict(os.environ, env, clear=True), patch("urllib.request.urlopen", fake_urlopen):
            client = OpenAICompatibleClient(api_key="test-key", model="test-model")
            client.complete([LLMMessage(role="user", content="Return JSON.")])

        self.assertNotIn("response_format", captured["payload"])

    def test_json_mode_retries_without_response_format_when_rejected(self) -> None:
        payloads = []

        def fake_urlopen(request, timeout):
            payload = json.loads(request.data.decode("utf-8"))
            payloads.append(payload)
            if len(payloads) == 1:
                raise urllib.error.HTTPError(
                    request.full_url,
                    400,
                    "Bad Request",
                    hdrs={},
                    fp=BytesIO(b'{"error":"response_format is not supported"}'),
                )
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            client = OpenAICompatibleClient(api_key="test-key", model="test-model")
            output = client.complete([LLMMessage(role="user", content="Return JSON.")])

        self.assertEqual(output, '{"ok": true}')
        self.assertEqual(len(payloads), 2)
        self.assertIn("response_format", payloads[0])
        self.assertNotIn("response_format", payloads[1])

    def test_json_mode_retries_without_response_format_for_non_json_gateway_response(self) -> None:
        payloads = []

        def fake_urlopen(request, timeout):
            payload = json.loads(request.data.decode("utf-8"))
            payloads.append(payload)
            if len(payloads) == 1:
                return FakeResponse(b"<html>gateway error</html>")
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            client = OpenAICompatibleClient(api_key="test-key", model="test-model")
            output = client.complete([LLMMessage(role="user", content="Return JSON.")])

        self.assertEqual(output, '{"ok": true}')
        self.assertEqual(len(payloads), 2)
        self.assertIn("response_format", payloads[0])
        self.assertNotIn("response_format", payloads[1])

    def test_non_json_response_error_includes_safe_preview(self) -> None:
        def fake_urlopen(request, timeout):
            return FakeResponse(b"<html>gateway rejected test-key</html>")

        with patch("urllib.request.urlopen", fake_urlopen):
            client = OpenAICompatibleClient(api_key="test-key", model="test-model", json_mode=False)
            with self.assertRaises(LLMError) as context:
                client.complete([LLMMessage(role="user", content="Return JSON.")])

        message = str(context.exception)
        self.assertIn("Body preview: <html>gateway rejected [REDACTED_API_KEY]</html>", message)
        self.assertIn("Content-Type: unknown", message)
        self.assertNotIn("test-key", message)


if __name__ == "__main__":
    unittest.main()

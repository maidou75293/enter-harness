"""Anthropic 原生 Messages API：消息/工具转换、流式聚合、错误映射。"""
import asyncio
import json
import os
import unittest
from unittest import mock

import httpx

from enter_harness.errors import ConfigError, ProviderError
from enter_harness.llm import AnthropicCompatModel
from enter_harness.llm.anthropic_compat import (to_anthropic_messages,
                                                to_anthropic_tools)
from enter_harness.llm.registry import create_provider, list_providers


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)
        self._raw = self.text.encode("utf-8")

    async def aread(self):
        return self._raw

    def json(self):
        return self._body


class FakeStreamResponse:
    def __init__(self, events, status_code=200, body=b"", fail_after=None):
        self.lines = [f"data: {json.dumps(e)}" for e in events]
        self.status_code = status_code
        self._body = body
        self.fail_after = fail_after

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self):
        return self._body

    async def aiter_lines(self):
        for i, line in enumerate(self.lines):
            if self.fail_after is not None and i >= self.fail_after:
                raise httpx.ConnectError("stream dropped")
            yield line


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []
        self.headers = []
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls += 1
        self.payloads.append(json)
        self.headers.append(dict(headers or {}))
        return self.responses.pop(0)

    def stream(self, method, url, headers=None, json=None):
        self.calls += 1
        self.payloads.append(json)
        self.headers.append(dict(headers or {}))
        return self.responses.pop(0)


def _model(client, **kw):
    m = AnthropicCompatModel(base_url="https://api.anthropic.com", api_key="sk-ant",
                             name="claude-sonnet-5-5", retry_backoff=0, **kw)
    m._client = lambda: client
    return m


async def _collect(stream):
    return [c async for c in stream]


class ConversionTests(unittest.TestCase):
    def test_messages_and_tools(self):
        system, messages = to_anthropic_messages([
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "echo",
                                          "arguments": "{\"x\": 1}"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "ok"},
        ])
        self.assertEqual(system, "你是助手")
        self.assertEqual(messages[0], {"role": "user",
                                       "content": [{"type": "text", "text": "hi"}]})
        self.assertEqual(messages[1]["content"][0]["type"], "tool_use")
        self.assertEqual(messages[1]["content"][0]["input"], {"x": 1})
        self.assertEqual(messages[2]["content"][0],
                         {"type": "tool_result", "tool_use_id": "t1",
                          "content": "ok"})
        tools = to_anthropic_tools([
            {"type": "function", "function": {
                "name": "echo", "description": "d",
                "parameters": {"type": "object", "properties": {}}}}])
        self.assertEqual(tools[0]["name"], "echo")
        self.assertIn("input_schema", tools[0])


class ChatTests(unittest.TestCase):
    def test_chat_maps_response(self):
        client = FakeClient([FakeResponse(200, {
            "content": [{"type": "text", "text": "hello"},
                        {"type": "tool_use", "id": "t1", "name": "echo",
                         "input": {"x": 1}}],
            "usage": {"input_tokens": 3, "output_tokens": 4},
            "stop_reason": "tool_use"})])
        msg = asyncio.run(_model(client).chat(
            [{"role": "user", "content": "hi"}],
            tools=[{"name": "echo", "description": "d",
                    "parameters": {"type": "object"}}]))
        self.assertEqual(msg["content"], "hello")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "echo")
        self.assertEqual(json.loads(msg["tool_calls"][0]["function"]["arguments"]),
                         {"x": 1})
        self.assertEqual(msg["usage"], {"prompt_tokens": 3, "completion_tokens": 4})
        self.assertEqual(msg["finish_reason"], "tool_calls")
        self.assertEqual(client.headers[0]["x-api-key"], "sk-ant")
        self.assertEqual(client.headers[0]["anthropic-version"], "2023-06-01")
        self.assertIn("tools", client.payloads[0])
        self.assertIn("input_schema", client.payloads[0]["tools"][0])

    def test_http_error(self):
        client = FakeClient([FakeResponse(400, {"error": {"message": "bad"}})])
        with self.assertRaises(ProviderError) as ctx:
            asyncio.run(_model(client).chat([{"role": "user", "content": "x"}]))
        self.assertEqual(ctx.exception.status, 400)

    def test_missing_key(self):
        with self.assertRaises(ConfigError):
            asyncio.run(AnthropicCompatModel(api_key="", name="m").chat(
                [{"role": "user", "content": "x"}]))

    def test_embed_not_supported(self):
        with self.assertRaises(ProviderError):
            _model(FakeClient([])).embed(["a"])


class StreamTests(unittest.TestCase):
    def test_stream_aggregates_text_and_tools(self):
        events = [
            {"type": "message_start",
             "message": {"usage": {"input_tokens": 3}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "he"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "llo"}},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "tool_use", "id": "t1", "name": "echo"}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta", "partial_json": "{\"x\":"}},
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "input_json_delta", "partial_json": " 1}"}},
            {"type": "message_delta",
             "delta": {"stop_reason": "tool_use"},
             "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]
        client = FakeClient([FakeStreamResponse(events)])
        chunks = asyncio.run(_collect(_model(client).chat_stream(
            [{"role": "user", "content": "hi"}])))
        self.assertEqual("".join(c["content"] for c in chunks), "hello")
        final = chunks[-1]
        self.assertTrue(final["done"])
        self.assertEqual(final["finish_reason"], "tool_calls")
        self.assertEqual(final["usage"], {"prompt_tokens": 3, "completion_tokens": 5})
        self.assertEqual(final["tool_calls"][0]["id"], "t1")
        self.assertEqual(json.loads(final["tool_calls"][0]["function"]["arguments"]),
                         {"x": 1})
        self.assertTrue(client.payloads[0]["stream"])


    def test_stream_http_error_maps_to_provider_error(self):
        client = FakeClient([FakeStreamResponse([], status_code=400, body=b"bad")])

        async def run():
            return await _collect(_model(client).chat_stream(
                [{"role": "user", "content": "hi"}]))

        with self.assertRaises(ProviderError) as ctx:
            asyncio.run(run())
        self.assertEqual(ctx.exception.status, 400)

    def test_retry_clears_stale_tool_use(self):
        # 第一次：发出 tool_use start 后断流（还没任何 delta）→ 可重试
        first = FakeStreamResponse([
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "toolu_stale",
                               "name": "dangerous"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "ignored"}},
        ], fail_after=1)
        second = FakeStreamResponse([
            {"type": "message_start", "message": {"usage": {"input_tokens": 2}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "hello"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 1}},
        ])
        client = FakeClient([first, second])
        chunks = asyncio.run(_collect(_model(client, retry_attempts=1).chat_stream(
            [{"role": "user", "content": "hi"}])))
        final = chunks[-1]
        self.assertEqual("".join(c["content"] for c in chunks), "hello")
        self.assertIsNone(final["tool_calls"])
        self.assertEqual(client.calls, 2)


class RegistryTests(unittest.TestCase):
    def test_builtin_registered(self):
        names = list_providers()
        self.assertIn("anthropic", names)
        self.assertIn("claude", names)

    def test_env_fallback(self):
        env = {"ANTHROPIC_BASE_URL": "https://proxy.test",
               "ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_MODEL": "claude-test"}
        with mock.patch.dict(os.environ, env, clear=False):
            provider = create_provider("anthropic")
        self.assertEqual(provider.base_url, "https://proxy.test")
        self.assertEqual(provider.api_key, "sk-test")
        self.assertEqual(provider.name, "claude-test")


if __name__ == "__main__":
    unittest.main()

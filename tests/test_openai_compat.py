"""OpenAI 兼容 provider：流式 usage / finish_reason、重试、错误映射。"""
import asyncio
import json
import unittest

import httpx

from enter_harness.errors import ProviderError
from enter_harness.llm import OpenAICompatModel


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
    def __init__(self, lines, status_code=200, body=b"", fail_after=None):
        self.lines = lines
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


class _RaisingStream:
    def __init__(self, exc):
        self.exc = exc

    async def __aenter__(self):
        raise self.exc

    async def __aexit__(self, *exc):
        return False


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.payloads = []
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls += 1
        self.payloads.append(json)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def stream(self, method, url, headers=None, json=None):
        self.calls += 1
        self.payloads.append(json)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            return _RaisingStream(item)
        return item


def _lines(*objs):
    out = [f"data: {json.dumps(o)}" for o in objs]
    out.append("data: [DONE]")
    return out


def _model(client, **kw):
    m = OpenAICompatModel(base_url="https://x.test/v1", api_key="k", name="m",
                          retry_backoff=0, **kw)
    m._client = lambda: client
    return m


async def _collect(stream):
    return [c async for c in stream]


class StreamTests(unittest.TestCase):
    def test_stream_requests_usage_and_reports_it(self):
        client = FakeClient([FakeStreamResponse(_lines(
            {"choices": [{"delta": {"content": "he"}}]},
            {"choices": [{"delta": {"content": "llo"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
        ))])
        model = _model(client)
        chunks = asyncio.run(_collect(model.chat_stream(
            [{"role": "user", "content": "hi"}])))
        self.assertEqual(client.payloads[0]["stream_options"],
                         {"include_usage": True})
        self.assertEqual("".join(c["content"] for c in chunks), "hello")
        final = chunks[-1]
        self.assertTrue(final["done"])
        self.assertEqual(final["usage"], {"prompt_tokens": 5, "completion_tokens": 2})
        self.assertEqual(final["finish_reason"], "stop")

    def test_stream_usage_can_be_disabled(self):
        client = FakeClient([FakeStreamResponse(_lines(
            {"choices": [{"delta": {"content": "x"}}]}))])
        model = _model(client, stream_usage=False)
        asyncio.run(_collect(model.chat_stream([{"role": "user", "content": "hi"}])))
        self.assertNotIn("stream_options", client.payloads[0])

    def test_stream_retries_transport_error_before_content(self):
        client = FakeClient([
            httpx.ConnectError("boom"),
            FakeStreamResponse(_lines({"choices": [{"delta": {"content": "ok"}}]})),
        ])
        model = _model(client, retry_attempts=1)
        chunks = asyncio.run(_collect(model.chat_stream(
            [{"role": "user", "content": "hi"}])))
        self.assertEqual(client.calls, 2)
        self.assertEqual("".join(c["content"] for c in chunks), "ok")

    def test_stream_does_not_retry_after_content(self):
        client = FakeClient([FakeStreamResponse(
            _lines({"choices": [{"delta": {"content": "partial"}}]}),
            fail_after=1)])
        model = _model(client, retry_attempts=2)

        async def run():
            return await _collect(model.chat_stream([{"role": "user", "content": "hi"}]))

        with self.assertRaises(ProviderError):
            asyncio.run(run())
        self.assertEqual(client.calls, 1)

    def test_stream_http_error_maps_to_provider_error(self):
        client = FakeClient([FakeStreamResponse([], status_code=400, body=b"bad")])
        model = _model(client)

        async def run():
            return await _collect(model.chat_stream([{"role": "user", "content": "hi"}]))

        with self.assertRaises(ProviderError) as ctx:
            asyncio.run(run())
        self.assertEqual(ctx.exception.status, 400)


class ChatTests(unittest.TestCase):
    def test_chat_success(self):
        client = FakeClient([FakeResponse(200, {
            "choices": [{"message": {"content": "hi", "tool_calls": []}}]})])
        msg = asyncio.run(_model(client).chat([{"role": "user", "content": "x"}]))
        self.assertEqual(msg["content"], "hi")

    def test_chat_retries_429(self):
        client = FakeClient([
            FakeResponse(429, {"error": "slow down"}),
            FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]}),
        ])
        msg = asyncio.run(_model(client, retry_attempts=1).chat(
            [{"role": "user", "content": "x"}]))
        self.assertEqual(msg["content"], "ok")
        self.assertEqual(client.calls, 2)

    def test_chat_400_not_retried(self):
        client = FakeClient([FakeResponse(400, {"error": "bad request"})])
        with self.assertRaises(ProviderError) as ctx:
            asyncio.run(_model(client, retry_attempts=3).chat(
                [{"role": "user", "content": "x"}]))
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()

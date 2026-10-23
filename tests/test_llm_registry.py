"""Provider 注册表与同步 ModelPort 适配。"""
import asyncio
import os
import unittest
from unittest import mock

from enter_harness import (AgentConfig, ProviderModelPort, Scope, create_model,
                        create_provider, list_providers, register_provider,
                        unregister_provider)
from enter_harness.domain import Message
from enter_harness.errors import ConfigError
from enter_harness.llm import build_model, build_provider
from enter_harness.settings import DictSettings


class FakeProvider:
    def __init__(self, **options):
        self.options = options
        self.requests = []

    async def chat(self, messages, tools=None, tool_choice=None,
                   response_format=None, temperature=None):
        self.requests.append((messages, tools))
        return {
            "content": "hello",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "echo",
                                         "arguments": "{\"text\": \"x\"}"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            "finish_reason": "stop",
        }

    async def complete(self, messages, kind="chat"):
        return "hello"

    def embed(self, texts):
        return [[0.1, 0.2] for _ in texts]


class FakeStreamingProvider(FakeProvider):
    """把正文与 tool_calls 拆成多个 chunk，模拟真实 SSE。"""

    def __init__(self, **options):
        super().__init__(**options)
        self.stream_requests = []

    async def chat_stream(self, messages, tools=None, tool_choice=None, temperature=None):
        self.stream_requests.append((messages, tools))
        yield {"content": "he", "tool_calls": None, "done": False}
        yield {"content": "llo", "tool_calls": None, "done": False}
        yield {"content": "", "tool_calls": [
            {"index": 0, "id": "c1", "type": "function",
             "function": {"name": "ec", "arguments": "{\"te"}},
        ], "done": False}
        yield {"content": "", "tool_calls": [
            {"index": 0, "function": {"name": "ho", "arguments": "xt\": 1}"}},
        ], "done": False}
        yield {"content": "", "tool_calls": None,
               "usage": {"prompt_tokens": 7, "completion_tokens": 5},
               "finish_reason": "tool_calls", "done": True}


class SimpleTool:
    name = "other"
    description = "o"
    parameters = {"type": "object"}


class ProviderRegistryTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: unregister_provider("fake"))

    def test_builtin_providers_registered(self):
        names = list_providers()
        for n in ("openai", "openai-compatible", "vllm", "ollama"):
            self.assertIn(n, names)

    def test_unknown_provider_raises_config_error(self):
        with self.assertRaises(ConfigError):
            create_provider("no-such-provider")

    def test_register_duplicate_requires_replace(self):
        register_provider("fake", lambda **kw: FakeProvider(**kw))
        with self.assertRaises(ValueError):
            register_provider("fake", lambda **kw: FakeProvider(**kw))
        register_provider("fake", lambda **kw: FakeProvider(**kw), replace=True)
        self.assertIsInstance(create_provider("fake"), FakeProvider)

    def test_unregister(self):
        register_provider("fake", lambda **kw: FakeProvider(**kw))
        self.assertTrue(unregister_provider("fake"))
        self.assertFalse(unregister_provider("fake"))
        with self.assertRaises(ConfigError):
            create_provider("fake")

    def test_invalid_provider_name(self):
        with self.assertRaises(ValueError):
            register_provider("Bad Name", lambda **kw: FakeProvider(**kw))

    def test_create_model_returns_sync_port(self):
        register_provider("fake", lambda **kw: FakeProvider(**kw))
        model = create_model("fake", name="m1")
        self.assertIsInstance(model, ProviderModelPort)
        resp = model.complete([Message(role="user", content="hi")])
        self.assertEqual(resp.content, "hello")
        self.assertEqual(resp.tool_calls[0].name, "echo")
        self.assertEqual(resp.tool_calls[0].arguments, {"text": "x"})
        self.assertEqual(resp.usage.input_tokens, 3)
        self.assertEqual(resp.usage.output_tokens, 4)
        self.assertEqual(model.embed(["a", "b"]), [[0.1, 0.2], [0.1, 0.2]])
        self.assertGreater(model.count_tokens([Message(role="user",
                                                       content="12345678")]), 0)
        deltas = list(model.stream([Message(role="user", content="hi")]))
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0].content, "hello")

    def test_tool_and_message_normalization(self):
        provider = FakeProvider()
        port = ProviderModelPort(provider)
        tools = [
            {"name": "echo", "description": "d", "parameters": {"type": "object"}},
            SimpleTool(),
        ]
        port.complete([Message(role="user", content="hi"),
                       Message(role="tool", content="ok", tool_call_id="c0")],
                      tools=tools)
        messages, sent_tools = provider.requests[0]
        self.assertEqual(messages[0], {"role": "user", "content": "hi"})
        self.assertEqual(messages[1]["tool_call_id"], "c0")
        self.assertEqual([t["function"]["name"] for t in sent_tools], ["echo", "other"])
        self.assertTrue(all(t["type"] == "function" for t in sent_tools))

    def test_on_delta_streams_and_aggregates(self):
        register_provider("fake-stream", lambda **kw: FakeStreamingProvider(**kw))
        self.addCleanup(lambda: unregister_provider("fake-stream"))
        seen: list[str] = []
        model = create_model("fake-stream", on_delta=seen.append)
        resp = model.complete([Message(role="user", content="hi")],
                              tools=[SimpleTool()])
        self.assertEqual(seen, ["he", "llo"])
        self.assertEqual(resp.content, "hello")
        self.assertEqual(len(resp.tool_calls), 1)
        self.assertEqual(resp.tool_calls[0].id, "c1")
        self.assertEqual(resp.tool_calls[0].name, "echo")
        self.assertEqual(resp.tool_calls[0].arguments, {"text": 1})
        self.assertEqual(resp.usage.input_tokens, 7)
        self.assertEqual(resp.usage.output_tokens, 5)
        self.assertEqual(resp.finish_reason, "tool_calls")
        _, sent_tools = model.provider.stream_requests[0]
        self.assertEqual(sent_tools[0]["function"]["name"], "other")

    def test_stream_yields_incremental_deltas(self):
        register_provider("fake-stream", lambda **kw: FakeStreamingProvider(**kw))
        self.addCleanup(lambda: unregister_provider("fake-stream"))
        model = create_model("fake-stream")
        deltas = list(model.stream([Message(role="user", content="hi")]))
        self.assertEqual([d.content for d in deltas], ["he", "llo", "", "", ""])
        self.assertEqual(deltas[-1].finish_reason, "tool_calls")
        self.assertEqual(deltas[-1].usage.input_tokens, 7)
        # 工具调用随流累计给出，最后一个 delta 是完整列表
        self.assertEqual(deltas[-1].tool_calls[0].name, "echo")
        self.assertEqual(deltas[-1].tool_calls[0].arguments, {"text": 1})
        self.assertEqual(len(deltas[0].tool_calls), 0)

    def test_complete_inside_running_loop(self):
        register_provider("fake", lambda **kw: FakeProvider(**kw))
        model = create_model("fake")

        async def caller():
            return model.complete([Message(role="user", content="hi")])

        self.assertEqual(asyncio.run(caller()).content, "hello")

    def test_openai_env_fallback(self):
        env = {"OPENAI_BASE_URL": "https://example.test/v1",
               "OPENAI_API_KEY": "sk-test", "OPENAI_MODEL": "m-test"}
        with mock.patch.dict(os.environ, env, clear=False):
            provider = create_provider("openai")
        self.assertEqual(provider.base_url, "https://example.test/v1")
        self.assertEqual(provider.api_key, "sk-test")
        self.assertEqual(provider.name, "m-test")

    def test_explicit_options_beat_env(self):
        env = {"OPENAI_BASE_URL": "https://env.test/v1", "OPENAI_API_KEY": "env",
               "OPENAI_MODEL": "env-model"}
        with mock.patch.dict(os.environ, env, clear=False):
            provider = create_provider("openai", base_url="https://arg.test/v1",
                                       api_key="arg", name="arg-model")
        self.assertEqual(provider.base_url, "https://arg.test/v1")
        self.assertEqual(provider.name, "arg-model")


class SettingsProviderTests(unittest.TestCase):
    def setUp(self):
        register_provider("fake", lambda **kw: FakeProvider(**kw))
        self.addCleanup(lambda: unregister_provider("fake"))

    def test_profile_selects_provider(self):
        settings = DictSettings({
            "models.profiles": ('[{"id": "p1", "provider": "fake",'
                                ' "base_url": "https://x/v1", "api_key": "k",'
                                ' "model": "m1", "temperature": 0.5}]'),
            "models.default": "p1",
        })
        provider = build_provider(settings)
        self.assertIsInstance(provider, FakeProvider)
        self.assertEqual(provider.options["base_url"], "https://x/v1")
        self.assertEqual(provider.options["name"], "m1")
        self.assertEqual(provider.options["temperature"], 0.5)

    def test_build_model_returns_port(self):
        settings = DictSettings({
            "models.profiles": ('[{"id": "p1", "provider": "fake",'
                                ' "base_url": "https://x/v1", "api_key": "k",'
                                ' "model": "m1"}]'),
            "models.default": "p1",
        })
        model = build_model(settings)
        self.assertIsInstance(model, ProviderModelPort)
        self.assertEqual(model.complete([Message(role="user", content="hi")]).content,
                         "hello")

    def test_agent_config_accepts_created_model(self):
        model = create_model("fake")
        cfg = AgentConfig(scope=Scope("t1", "a1", "u1", ()), model=model,
                          store=None, tools=None)
        self.assertIs(cfg.model, model)


if __name__ == "__main__":
    unittest.main()

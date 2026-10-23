"""Anthropic 原生 Messages API provider（不走 OpenAI 兼容层）。

与 `OpenAICompatModel` 暴露同一套接口，`ProviderModelPort` 无需区分：

    await provider.chat(messages, tools=..., tool_choice=...) -> dict
    async for chunk in provider.chat_stream(messages, tools=...): ...

协议差异都在这里吸收：
- system 消息合并成顶层 `system`；
- 工具调用是 `tool_use` / `tool_result` 内容块；
- 工具 schema 是 `input_schema`，不是 `parameters`；
- 流式是 `content_block_delta` 事件。

Anthropic 不提供 embedding 接口，`embed()` 会显式报错；需要时用
`enter_harness.llm.local_embed` 或另接 OpenAI 兼容的嵌入服务。
"""
from __future__ import annotations

import json

from ..errors import ConfigError, ProviderError

DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
_STOP_REASONS = {"end_turn": "stop", "stop_sequence": "stop",
                 "tool_use": "tool_calls", "max_tokens": "length"}


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(b.get("text") or "") for b in content
                       if isinstance(b, dict))
    return "" if content is None else str(content)


def _content_blocks(message: dict) -> list:
    """OpenAI 消息 → Anthropic content blocks。"""
    role = str(message.get("role") or "user")
    if role == "tool":
        return [{"type": "tool_result",
                 "tool_use_id": str(message.get("tool_call_id") or ""),
                 "content": _text_of(message.get("content"))}]
    blocks: list = []
    text = _text_of(message.get("content"))
    if text:
        blocks.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                args = {}
        blocks.append({"type": "tool_use", "id": str(call.get("id") or ""),
                       "name": str(fn.get("name") or ""),
                       "input": args if isinstance(args, dict) else {}})
    return blocks


def to_anthropic_messages(messages: list) -> tuple[str, list]:
    """返回 (system, messages)。连续同角色合并，tool_result 并入 user。"""
    system_parts: list[str] = []
    out: list[dict] = []
    for message in messages or []:
        role = str(message.get("role") or "user")
        if role == "system":
            text = _text_of(message.get("content"))
            if text:
                system_parts.append(text)
            continue
        target = "assistant" if role == "assistant" else "user"
        blocks = _content_blocks(message)
        if not blocks:
            continue
        if out and out[-1]["role"] == target:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": target, "content": blocks})
    return "\n\n".join(system_parts), out


def to_anthropic_tools(tools) -> list:
    out = []
    for tool in tools or []:
        if isinstance(tool, dict) and "function" in tool:
            fn = tool["function"] or {}
            out.append({"name": str(fn.get("name") or ""),
                        "description": str(fn.get("description") or ""),
                        "input_schema": fn.get("parameters")
                        or {"type": "object", "properties": {}}})
        elif isinstance(tool, dict):
            out.append({"name": str(tool.get("name") or ""),
                        "description": str(tool.get("description") or ""),
                        "input_schema": tool.get("parameters") or tool.get("input_schema")
                        or {"type": "object", "properties": {}}})
    return out


def to_anthropic_tool_choice(choice):
    if not choice:
        return None
    if isinstance(choice, str):
        return {"type": {"auto": "auto", "required": "any", "none": "none"}.get(choice, "auto")}
    fn = (choice.get("function") or {}) if isinstance(choice, dict) else {}
    name = fn.get("name") or (choice.get("name") if isinstance(choice, dict) else "")
    return {"type": "tool", "name": str(name)} if name else {"type": "auto"}


class AnthropicCompatModel:
    """Anthropic Messages API provider。"""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, api_key: str = "",
                 name: str = "", temperature: float = 0.2, max_tokens: int = 4096,
                 timeout: float = 120, proxy_url: str = "",
                 anthropic_version: str = ANTHROPIC_VERSION,
                 retry_attempts: int = 2, retry_backoff: float = 0.5):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key
        self.name = name
        self.temperature = temperature
        self.max_tokens = int(max_tokens)
        self.timeout = timeout
        self.proxy_url = (proxy_url or "").strip()
        self.anthropic_version = anthropic_version
        self.retry_attempts = max(0, min(int(retry_attempts), 5))
        self.retry_backoff = max(0.0, min(float(retry_backoff), 10.0))

    # ---------------- 内部 ----------------
    def _check(self) -> None:
        if not self.name:
            raise ConfigError("未设置 Anthropic 模型名（如 claude-sonnet-5-5）")
        if not self.api_key:
            raise ConfigError("未设置 ANTHROPIC_API_KEY")

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key,
                "anthropic-version": self.anthropic_version,
                "content-type": "application/json"}

    def _client(self):
        import httpx
        return httpx.AsyncClient(timeout=self.timeout, proxy=(self.proxy_url or None))

    @staticmethod
    def _retryable(status: int) -> bool:
        return status in (408, 409, 429) or status >= 500

    async def _sleep(self, attempt: int) -> None:
        import asyncio
        await asyncio.sleep(self.retry_backoff * (2 ** attempt))

    async def _post(self, client, payload: dict):
        attempt = 0
        while True:
            try:
                resp = await client.post(self.base_url + "/v1/messages",
                                         headers=self._headers(), json=payload)
            except Exception as e:  # noqa: BLE001
                import httpx
                if not isinstance(e, httpx.TransportError) or attempt >= self.retry_attempts:
                    raise ProviderError(f"Anthropic 连接失败：{type(e).__name__}: {e}") from e
                await self._sleep(attempt)
                attempt += 1
                continue
            if self._retryable(resp.status_code) and attempt < self.retry_attempts:
                await self._sleep(attempt)
                attempt += 1
                continue
            return resp

    def _payload(self, messages, tools=None, tool_choice=None,
                 temperature=None, stream: bool = False) -> dict:
        system, converted = to_anthropic_messages(messages)
        payload = {"model": self.name, "messages": converted,
                   "max_tokens": self.max_tokens,
                   "temperature": self.temperature if temperature is None else temperature}
        if system:
            payload["system"] = system
        tool_defs = to_anthropic_tools(tools)
        if tool_defs:
            payload["tools"] = tool_defs
            choice = to_anthropic_tool_choice(tool_choice)
            if choice:
                payload["tool_choice"] = choice
        if stream:
            payload["stream"] = True
        return payload

    @staticmethod
    def _message_of(data: dict) -> dict:
        text_parts, calls = [], []
        for block in data.get("content") or []:
            kind = block.get("type")
            if kind == "text":
                text_parts.append(str(block.get("text") or ""))
            elif kind == "tool_use":
                calls.append({"id": str(block.get("id") or ""), "type": "function",
                              "function": {
                                  "name": str(block.get("name") or ""),
                                  "arguments": json.dumps(block.get("input") or {},
                                                          ensure_ascii=False)}})
        usage = data.get("usage") or {}
        return {"content": "".join(text_parts),
                "tool_calls": calls,
                "usage": {"prompt_tokens": int(usage.get("input_tokens") or 0),
                          "completion_tokens": int(usage.get("output_tokens") or 0)},
                "finish_reason": _STOP_REASONS.get(str(data.get("stop_reason") or ""),
                                                   "stop")}

    # ---------------- 接口 ----------------
    async def chat(self, messages: list, tools: list | None = None, tool_choice=None,
                   response_format=None, temperature=None) -> dict:
        self._check()
        payload = self._payload(messages, tools, tool_choice, temperature)
        async with self._client() as client:
            resp = await self._post(client, payload)
        if resp.status_code >= 400:
            raise ProviderError(f"Anthropic 接口返回 {resp.status_code}：{resp.text[:300]}",
                                status=resp.status_code, body=resp.text[:2000])
        try:
            return self._message_of(resp.json())
        except (KeyError, TypeError, ValueError) as e:
            raise ProviderError(f"Anthropic 响应格式异常：{e}") from e

    async def complete(self, messages: list, kind: str = "chat") -> str:
        msg = await self.chat(messages)
        return msg.get("content") or ""

    async def chat_stream(self, messages: list, tools: list | None = None,
                          tool_choice=None, temperature=None):
        """逐块产出与 OpenAI 兼容 provider 相同的 chunk 结构。"""
        self._check()
        payload = self._payload(messages, tools, tool_choice, temperature, stream=True)
        calls: dict[int, dict] = {}
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        finish = ""

        for attempt in range(self.retry_attempts + 1):
            # 每次尝试重新聚合，避免失败尝试的 tool_use / usage 混进成功结果
            calls.clear()
            usage = {"prompt_tokens": 0, "completion_tokens": 0}
            finish = ""
            emitted = False
            try:
                async with self._client() as client:
                    async with client.stream("POST", self.base_url + "/v1/messages",
                                             headers=self._headers(),
                                             json=payload) as resp:
                        if resp.status_code >= 400:
                            body = await resp.aread()
                            if self._retryable(resp.status_code) and attempt < self.retry_attempts:
                                await self._sleep(attempt)
                                continue
                            raise ProviderError(
                                f"Anthropic 接口返回 {resp.status_code}："
                                f"{body[:300].decode('utf-8', 'replace')}",
                                status=resp.status_code,
                                body=body[:2000].decode("utf-8", "replace"))
                        async for line in resp.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            try:
                                event = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            kind = str(event.get("type") or "")
                            if kind == "message_start":
                                u = ((event.get("message") or {}).get("usage") or {})
                                usage["prompt_tokens"] = int(u.get("input_tokens") or 0)
                            elif kind == "content_block_start":
                                block = event.get("content_block") or {}
                                if block.get("type") == "tool_use":
                                    calls[int(event.get("index") or 0)] = {
                                        "id": str(block.get("id") or ""),
                                        "type": "function",
                                        "function": {"name": str(block.get("name") or ""),
                                                     "arguments": ""}}
                            elif kind == "content_block_delta":
                                delta = event.get("delta") or {}
                                if delta.get("type") == "text_delta":
                                    text = str(delta.get("text") or "")
                                    if text:
                                        emitted = True
                                    yield {"content": text, "tool_calls": None,
                                           "usage": None, "finish_reason": finish,
                                           "done": False}
                                elif delta.get("type") == "input_json_delta":
                                    idx = int(event.get("index") or 0)
                                    cur = calls.setdefault(idx, {
                                        "id": "", "type": "function",
                                        "function": {"name": "", "arguments": ""}})
                                    cur["function"]["arguments"] += str(
                                        delta.get("partial_json") or "")
                                    emitted = True
                            elif kind == "message_delta":
                                u = event.get("usage") or {}
                                if u.get("output_tokens") is not None:
                                    usage["completion_tokens"] = int(u["output_tokens"])
                                finish = _STOP_REASONS.get(
                                    str((event.get("delta") or {}).get("stop_reason") or ""),
                                    finish)
                            elif kind == "error":
                                err = event.get("error") or {}
                                raise ProviderError(
                                    f"Anthropic 流式错误：{err.get('type') or ''}: "
                                    f"{err.get('message') or ''}")
                break
            except ProviderError:
                raise
            except Exception as e:  # noqa: BLE001
                import httpx
                if (emitted or not isinstance(e, httpx.TransportError)
                        or attempt >= self.retry_attempts):
                    raise ProviderError(
                        f"Anthropic 连接失败：{type(e).__name__}: {e}") from e
                await self._sleep(attempt)

        yield {"content": "", "tool_calls": [calls[k] for k in sorted(calls)] or None,
               "usage": usage, "finish_reason": finish or "stop", "done": True}

    def embed(self, texts: list) -> list:
        raise ProviderError("Anthropic 不提供 embedding；请使用 local_embed 或 OpenAI 兼容嵌入服务")

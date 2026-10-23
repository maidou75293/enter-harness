"""OpenAI 兼容 provider（原生 tool calling + 可选流式）。

统一接口：
    await provider.chat(messages, tools=..., tool_choice=..., response_format=...) -> dict
        → assistant 消息（content / tool_calls）
    async for delta in provider.chat_stream(messages, tools=...) -> dict
        → {"content": 增量文本, "tool_calls": [...], "done": bool}
    await provider.complete(messages) -> str
    provider.embed(texts) -> list[list[float]]

不读环境变量、不依赖业务配置；由 `profiles.build_provider(settings)` 组装。
"""
from __future__ import annotations

import json

from ..errors import ConfigError, ProviderError
from . import embedding


class OpenAICompatModel:
    """OpenAI 兼容 provider（本地 vLLM / 云端均可）。"""

    def __init__(self, base_url: str, api_key: str, name: str, temperature: float = 0.2,
                 embed_model: str | None = None, reasoning_effort: str = "", proxy_url: str = "",
                 timeout: float = 120):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.name = name
        self.temperature = temperature
        self.embed_model = embed_model or name
        self.reasoning_effort = (reasoning_effort or "").strip()
        self.proxy_url = (proxy_url or "").strip()
        self.timeout = timeout

    # ---------------- 内部 ----------------
    def _payload(self, messages: list, tools=None, tool_choice=None,
                 response_format=None, temperature=None, stream: bool = False) -> dict:
        payload = {
            "model": self.name,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "stream": bool(stream),
        }
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
        if response_format:
            payload["response_format"] = response_format
        if self.reasoning_effort in ("minimal", "low", "medium", "high"):
            payload["reasoning_effort"] = self.reasoning_effort
        return payload

    def _client(self):
        import httpx
        return httpx.AsyncClient(timeout=self.timeout, proxy=(self.proxy_url or None))

    def _check(self) -> None:
        if not self.name:
            raise ConfigError("未设置模型名：请到「设置 → 模型名」刷新并选择一个模型")
        if not self.base_url:
            raise ConfigError("未设置模型 base_url")

    # ---------------- 非流式 ----------------
    async def chat(self, messages: list, tools: list | None = None, tool_choice=None,
                   response_format=None, temperature=None) -> dict:
        self._check()
        payload = self._payload(messages, tools, tool_choice, response_format, temperature)
        async with self._client() as client:
            async def _post(p):
                return await client.post(
                    self.base_url + "/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"}, json=p)

            resp = await _post(payload)
            # 思考模式不支持“强制指定工具”→ 退回 auto 再试一次
            if resp.status_code >= 400 and tool_choice and "tool_choice" in resp.text:
                payload.pop("tool_choice", None)
                resp = await _post(payload)
        if resp.status_code >= 400:
            raise ProviderError(f"模型接口返回 {resp.status_code}：{resp.text[:300]}",
                                status=resp.status_code, body=resp.text[:2000])
        try:
            return resp.json()["choices"][0]["message"]
        except (KeyError, IndexError, ValueError) as e:
            raise ProviderError(f"模型响应格式异常：{e}") from e

    async def complete(self, messages: list, kind: str = "chat") -> str:
        msg = await self.chat(messages)
        return msg.get("content") or ""

    # ---------------- 流式 ----------------
    async def chat_stream(self, messages: list, tools: list | None = None,
                          tool_choice=None, temperature=None):
        """逐块产出 {"content": str, "tool_calls": list|None, "done": bool}。

        tool_calls 按 index 聚合后，在 done=True 的块里给出完整列表。
        """
        self._check()
        payload = self._payload(messages, tools, tool_choice, None, temperature, stream=True)
        calls: dict[int, dict] = {}
        async with self._client() as client:
            async with client.stream("POST", self.base_url + "/chat/completions",
                                     headers={"Authorization": f"Bearer {self.api_key}"},
                                     json=payload) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise ProviderError(f"模型接口返回 {resp.status_code}："
                                        f"{body[:300].decode('utf-8', 'replace')}",
                                        status=resp.status_code)
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    for tc in (delta.get("tool_calls") or []):
                        idx = int(tc.get("index") or 0)
                        cur = calls.setdefault(idx, {"id": "", "type": "function",
                                                     "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            cur["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            cur["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            cur["function"]["arguments"] += fn["arguments"]
                    yield {"content": delta.get("content") or "",
                           "tool_calls": None, "done": False}
        yield {"content": "", "tool_calls": [calls[k] for k in sorted(calls)] or None, "done": True}

    def embed(self, texts: list) -> list:
        return embedding.openai_embed(
            texts, self.base_url, self.api_key, self.embed_model, self.proxy_url)


# ---------------------------------------------------------------- JSON 工具


def _content(m) -> str:
    return str(m.get("content", "")) if isinstance(m, dict) else str(m)


def _first_user(messages) -> str:
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            return _content(m)
    return ""


def _first_json_obj(t: str):
    """扫描第一个括号平衡的 JSON 对象（跳过字符串与转义）。"""
    start = t.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for k in range(start, len(t)):
            ch = t[k]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return t[start:k + 1]
        start = t.find("{", start + 1)
    return None


def extract_json(text: str) -> dict:
    """兜底：从纯文本里抠 JSON（正常路径走 tool calling）。"""
    if not text:
        return {}
    obj = _first_json_obj(text.strip())
    if obj is None:
        return {}
    try:
        parsed = json.loads(obj)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def tool_result(msg: dict, name: str | None = None) -> dict:
    """从 assistant 消息取工具调用参数（按 name 匹配）；无则解析 content。"""
    for c in (msg.get("tool_calls") or []):
        fn = c.get("function") or {}
        if name is None or fn.get("name") == name:
            try:
                return json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                return extract_json(fn.get("arguments") or "")
    return extract_json(msg.get("content") or "")

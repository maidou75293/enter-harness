"""Provider 注册表：按名字注册/选择模型 provider。

内核只认 `ModelPort`（同步）；真实大模型通过 provider 接入：

- 内置 `openai` / `openai-compatible`：任何 OpenAI 兼容接口
  （OpenAI、DeepSeek、通义、vLLM、Ollama、one-api 网关等）；
- 内置 `anthropic` / `claude`：Anthropic 原生 Messages API；
- 其他厂商/私有网关：`register_provider("my", factory)` 注册一个工厂，
  工厂返回实现 `contracts.Provider`（`async chat / complete`）的对象即可。

```python
from enter_harness.llm import register_provider, create_model
from my_gateway import MyProvider

register_provider("my", lambda **kw: MyProvider(**kw))
model = create_model("my", base_url=..., api_key=..., name="my-model")
agent = Agent(AgentConfig(..., model=model))     # 已经是 ModelPort
```

环境变量只在内置工厂里作为缺省值：
`OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL`，
`ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL`。
"""
from __future__ import annotations

import os
import re
import threading
from typing import Any, Callable

from ..errors import ConfigError
from .anthropic_compat import AnthropicCompatModel
from .anthropic_compat import DEFAULT_BASE_URL as ANTHROPIC_BASE_URL
from .openai_compat import OpenAICompatModel
from .sync_model import ProviderModelPort

Factory = Callable[..., Any]

_FACTORIES: dict[str, Factory] = {}
_LOCK = threading.RLock()
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def register_provider(name: str, factory: Factory, *, replace: bool = False,
                      aliases: tuple[str, ...] = ()) -> None:
    """注册 provider 工厂；名字冲突时默认报错，`replace=True` 覆盖。"""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"provider 名不合法：{name!r}（小写字母/数字/._-）")
    if not callable(factory):
        raise TypeError("provider 工厂必须可调用")
    names = (name,) + tuple(aliases)
    with _LOCK:
        for n in names:
            if not isinstance(n, str) or not _NAME_RE.match(n):
                raise ValueError(f"provider 别名不合法：{n!r}")
            if n in _FACTORIES and not replace:
                raise ValueError(f"provider 已存在：{n}（如需覆盖传 replace=True）")
        for n in names:
            _FACTORIES[n] = factory


def unregister_provider(name: str) -> bool:
    with _LOCK:
        return _FACTORIES.pop(name, None) is not None


def list_providers() -> list[str]:
    with _LOCK:
        return sorted(_FACTORIES)


def get_provider_factory(name: str) -> Factory:
    with _LOCK:
        factory = _FACTORIES.get(name)
    if factory is None:
        raise ConfigError(
            f"未注册的模型 provider：{name}；已注册：{', '.join(list_providers())}")
    return factory


def create_provider(provider: str = "openai", **options: Any) -> Any:
    """按名字创建 provider（async 接口，可直接 `await provider.chat(...)`）。"""
    return get_provider_factory(provider)(**options)


def create_model(provider: str = "openai", *, on_delta=None,
                 **options: Any) -> ProviderModelPort:
    """按名字创建**同步 ModelPort**，可直接传给 `AgentConfig(model=...)`。

    `on_delta(text)` 用于流式回调；不传则为一次性返回。
    """
    return ProviderModelPort(create_provider(provider, **options), on_delta=on_delta)


# ---------------------------------------------------------------- 内置工厂


def _openai_compatible(**options: Any) -> OpenAICompatModel:
    """OpenAI 兼容 provider；缺省值可来自环境变量。"""
    base_url = options.pop("base_url", None) or os.environ.get("OPENAI_BASE_URL") \
        or "https://api.openai.com/v1"
    api_key = options.pop("api_key", None) or os.environ.get("OPENAI_API_KEY", "")
    name = options.pop("name", None) or os.environ.get("OPENAI_MODEL", "")
    return OpenAICompatModel(base_url=base_url, api_key=api_key, name=name, **options)


def _anthropic(**options: Any) -> AnthropicCompatModel:
    """Anthropic 原生协议；缺省值可来自环境变量。"""
    base_url = options.pop("base_url", None) or os.environ.get("ANTHROPIC_BASE_URL") \
        or ANTHROPIC_BASE_URL
    api_key = options.pop("api_key", None) or os.environ.get("ANTHROPIC_API_KEY", "")
    name = options.pop("name", None) or os.environ.get("ANTHROPIC_MODEL", "")
    # 这些是 OpenAI 兼容层的概念，Anthropic 不需要
    options.pop("reasoning_effort", None)
    return AnthropicCompatModel(base_url=base_url, api_key=api_key,
                                name=name, **options)


register_provider("openai", _openai_compatible,
                  aliases=("openai-compatible", "openai_compatible", "vllm", "ollama"))
register_provider("anthropic", _anthropic, aliases=("claude",))

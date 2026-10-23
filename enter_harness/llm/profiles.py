"""多模型 profile 解析与 provider 构建。

配置键（与既有系统兼容，可由业务层替换/预置）：
    models.profiles  JSON: [{id,name,base_url,api_key,model,reasoning_effort,temperature,proxy}]
    models.default   默认 profile id
    model.base_url / model.api_key / model.name / model.temperature / model.reasoning_effort
    proxy.list       JSON: [{id,name,url}]
"""
from __future__ import annotations

import json
from typing import Any

from ..settings import as_settings
from .openai_compat import OpenAICompatModel


def list_proxies(settings: Any) -> list:
    raw = as_settings(settings).get_config("proxy.list", "") or ""
    if not str(raw).strip():
        return []
    try:
        v = json.loads(raw)
        return [x for x in v if isinstance(x, dict) and x.get("id")] if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


def proxy_url_of(settings: Any, pid) -> str:
    if not pid:
        return ""
    for p in list_proxies(settings):
        if str(p.get("id")) == str(pid):
            return str(p.get("url") or "")
    return ""


def _legacy_profile(s: Any) -> dict:
    return {"id": "default", "name": "默认模型",
            "base_url": s.get_config("model.base_url"),
            "api_key": s.get_config("model.api_key"),
            "model": s.get_config("model.name"),
            "reasoning_effort": s.get_config("model.reasoning_effort", "") or "",
            "temperature": s.get_float("model.temperature", 0.2)}


def list_profiles(settings: Any) -> list:
    s = as_settings(settings)
    raw = s.get_config("models.profiles", "") or ""
    items: list = []
    if str(raw).strip():
        try:
            v = json.loads(raw)
            if isinstance(v, list):
                items = [x for x in v if isinstance(x, dict) and x.get("id")]
        except (TypeError, ValueError):
            items = []
    return items or [_legacy_profile(s)]


def get_profile(settings: Any, pid=None) -> dict:
    s = as_settings(settings)
    items = list_profiles(s)
    if pid:
        for it in items:
            if str(it.get("id")) == str(pid):
                return it
    default = str(s.get_config("models.default", "") or "")
    for it in items:
        if str(it.get("id")) == default:
            return it
    return items[0] if items else {}


def _provider_options(s: Any, p: dict) -> dict:
    temp = p.get("temperature")
    try:
        temp = float(temp) if temp not in (None, "") else s.get_float("model.temperature", 0.2)
    except (TypeError, ValueError):
        temp = s.get_float("model.temperature", 0.2)
    return {
        "base_url": p.get("base_url") or s.get_config("model.base_url"),
        "api_key": p.get("api_key") or s.get_config("model.api_key"),
        "name": p.get("model") or s.get_config("model.name"),
        "temperature": temp,
        "reasoning_effort": (p.get("reasoning_effort")
                             or s.get_config("model.reasoning_effort", "") or ""),
        "proxy_url": proxy_url_of(s, p.get("proxy")),
    }


def build_provider(settings: Any, pid=None, model_cls=None):
    """按 Settings 中的配置构建 provider；缺省用默认 profile。

    profile 可用 `provider` 字段选择已注册的 provider（默认 `openai`）；
    显式传 `model_cls` 时按旧行为直接实例化该类。
    """
    s = as_settings(settings)
    p = get_profile(s, pid)
    options = _provider_options(s, p)
    if model_cls is not None:
        return model_cls(**options)
    from .registry import create_provider
    return create_provider(str(p.get("provider") or "openai"), **options)


def build_model(settings: Any, pid=None, on_delta=None):
    """按 Settings 构建**同步 ModelPort**，可直接传给 `AgentConfig(model=...)`。

    `on_delta(text)` 传入后，模型调用改为流式，逐块回调增量文本（SSE 场景）。
    """
    from .sync_model import ProviderModelPort

    return ProviderModelPort(build_provider(settings, pid), on_delta=on_delta)

"""LLM 层：provider 注册表 + OpenAI / Anthropic 实现 + 同步端口 + 嵌入/JSON 工具。"""
from .anthropic_compat import AnthropicCompatModel
from .embedding import cosine, local_embed, openai_embed, tokenize
from .openai_compat import OpenAICompatModel, extract_json, tool_result
from .profiles import build_model, build_provider, get_profile, list_profiles, proxy_url_of
from .registry import (create_model, create_provider, get_provider_factory,
                       list_providers, register_provider, unregister_provider)
from .sync_model import ProviderModelPort

__all__ = [
    "OpenAICompatModel", "AnthropicCompatModel", "extract_json", "tool_result",
    "build_model", "build_provider", "get_profile", "list_profiles", "proxy_url_of",
    "ProviderModelPort", "register_provider", "unregister_provider",
    "list_providers", "get_provider_factory", "create_provider", "create_model",
    "local_embed", "openai_embed", "cosine", "tokenize",
]

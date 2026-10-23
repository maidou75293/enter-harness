"""LLM 层：OpenAI 兼容 provider + 嵌入 + JSON 工具。"""
from .embedding import cosine, local_embed, openai_embed, tokenize
from .openai_compat import OpenAICompatModel, extract_json, tool_result
from .profiles import build_provider, get_profile, list_profiles, proxy_url_of

__all__ = [
    "OpenAICompatModel", "extract_json", "tool_result",
    "build_provider", "get_profile", "list_profiles", "proxy_url_of",
    "local_embed", "openai_embed", "cosine", "tokenize",
]

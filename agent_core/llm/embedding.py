"""嵌入（向量）实现：本地哈希词袋（默认，零第三方依赖）+ OpenAI 兼容嵌入。

local_embed 用 hashing trick：词/CJK 字/双字映射到固定维度并计数，再 L2 归一化。
不是语义级向量，但确定、可算余弦、能跑通混合检索；接真模型时注入 openai_embed。
"""
from __future__ import annotations

import math
import re

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_CJK_RE = re.compile(r"[一-鿿]")


def tokenize(text: str) -> list:
    text = (text or "").lower()
    words = _TOKEN_RE.findall(text)
    cjk = _CJK_RE.findall(text)
    bigrams = [a + b for a, b in zip(cjk, cjk[1:])]
    return words + cjk + bigrams


def _stable_hash(s: str) -> int:
    h = 2166136261
    for ch in s:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def local_embed(text: str, dim: int = 256) -> list:
    vec = [0.0] * dim
    for tok in tokenize(text):
        vec[_stable_hash(tok) % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def cosine(a: list, b: list) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def openai_embed(texts: list, base_url: str, api_key: str, model: str, proxy: str = "") -> list:
    """同步调用 OpenAI 兼容 /embeddings（httpx 延迟导入，纯本地场景不依赖）。"""
    import httpx

    url = base_url.rstrip("/") + "/embeddings"
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "input": texts},
        timeout=60,
        proxy=(proxy or None),
    )
    resp.raise_for_status()
    return [d["embedding"] for d in resp.json()["data"]]

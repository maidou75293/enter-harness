"""Markdown 本地存储的公共读写：frontmatter、路径安全、原子写。

文件形态：
    ---
    id: m_xxx
    tenant_id: t1
    kind: semantic
    ...
    ---
    正文（记忆内容 / 知识块正文）

只依赖标准库；不引入 YAML 依赖。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Iterator

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_DELIM = "---"


def safe_name(value: str) -> str:
    """文件名安全化；非 ASCII（如中文 id）会加 hash 后缀防碰撞。"""
    raw = str(value or "").strip()
    cleaned = _SAFE.sub("_", raw).strip("._")
    if not cleaned:
        cleaned = "item"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    if cleaned != raw or len(cleaned) > 80:
        return f"{cleaned[:80]}_{digest}"
    return cleaned


def _dump_value(value) -> str:
    if isinstance(value, str):
        # 空串、跨行、看起来像 JSON/数字/布尔的字符串用 JSON 引号包住
        if (not value or "\n" in value or value.strip() != value
                or value[:1] in ("{", "[", '"', "'")
                or value in ("true", "false", "null")
                or re.fullmatch(r"-?\d+(\.\d+)?", value)):
            return json.dumps(value, ensure_ascii=False)
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _parse_value(raw: str):
    text = raw.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001 —— 普通字符串
        return text


def dump(meta: dict, body: str) -> str:
    lines = [_DELIM]
    for key, value in meta.items():
        if value is None:
            continue
        lines.append(f"{key}: {_dump_value(value)}")
    lines.append(_DELIM)
    text = "\n".join(lines)
    if body:
        text += "\n\n" + str(body).strip("\n")
    return text + "\n"


def parse(text: str) -> tuple[dict, str]:
    lines = str(text or "").splitlines()
    if not lines or lines[0].strip() != _DELIM:
        return {}, str(text or "")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _DELIM:
            end = i
            break
    if end is None:
        return {}, str(text or "")
    meta: dict = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        if key:
            meta[key] = _parse_value(raw)
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, body


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def read(path: Path) -> tuple[dict, str]:
    with open(path, "r", encoding="utf-8") as fh:
        return parse(fh.read())


def iter_files(root: Path) -> Iterator[Path]:
    root = Path(root)
    if not root.exists():
        return
    for path in sorted(root.rglob("*.md")):
        if path.is_file():
            yield path

"""工具大输出治理：超阈值 → 全文落盘 + 头尾切片 + 路径。

模型上下文是工作集，不是日志：大输出不硬塞进上下文，给模型一个可恢复的指针。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class OutputLimiter:
    max_lines: int = 2000
    max_bytes: int = 51200
    keep_lines: int = 400
    keep_bytes: int = 8000
    dirname: str = ".outputs"

    def __call__(self, name: str, text, root: Path | None = None):
        return land_output(name, text, root, self)


def land_output(name: str, text, root: Path | None = None,
                limiter: OutputLimiter | None = None):
    """大输出落盘，返回「头 + 省略提示 + 尾」；小输出原样返回。"""
    if not isinstance(text, str):
        return text
    lim = limiter or OutputLimiter()
    nbytes = len(text.encode("utf-8"))
    nlines = text.count("\n") + 1
    if nlines <= lim.max_lines and nbytes <= lim.max_bytes:
        return text
    out_dir = (Path(root) / lim.dirname) if root else Path(lim.dirname)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", name)[:40] or "out"
    fp = out_dir / f"{int(time.time() * 1000)}_{safe}.txt"
    fp.write_text(text, encoding="utf-8")
    lines = text.splitlines()
    head = "\n".join(lines[:lim.keep_lines])
    tail = "\n".join(lines[-lim.keep_lines:])
    if len(head.encode("utf-8")) > lim.keep_bytes:
        head = head.encode("utf-8")[:lim.keep_bytes].decode("utf-8", "ignore")
    if len(tail.encode("utf-8")) > lim.keep_bytes:
        tail = tail.encode("utf-8")[-lim.keep_bytes:].decode("utf-8", "ignore")
    kept = len(head.encode("utf-8")) + len(tail.encode("utf-8"))
    elided = max(0, nbytes - kept)
    shown = str(fp.relative_to(root)) if root else str(fp)
    return (f"{head}\n\n…[省略 {elided} 字节（共 {nlines} 行 / {nbytes} 字节）；"
            f"完整输出已落盘：{shown}（工作区内），可用 read_file 读取]…\n\n{tail}")

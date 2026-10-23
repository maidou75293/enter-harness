"""工作区路径边界（沙箱的一半：文件工具）。"""
from __future__ import annotations

from pathlib import Path


def safe_path(p: str, root: Path) -> Path:
    """把路径限制在 root 内；resolve() 穿透 `..` 与软链，越界即拒。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    target = (root / (p or ".")).resolve()
    rr = root.resolve()
    if rr not in target.parents and target != rr:
        raise ValueError(f"路径越界：{p}（仅允许工作区 {rr} 内）")
    return target

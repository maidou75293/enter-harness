"""出口管制（DNS 白名单）。

三档：
- off        → --unshare-net，直接断网
- whitelist  → /etc/hosts 只解析白名单域名，DNS 置空
- all        → 不限制

注意：白名单挡不住「直接用 IP 直连」；要绝对强用 off。
"""
from __future__ import annotations

import hashlib
import os
import socket
from pathlib import Path

EGRESS_DIR = Path(os.environ.get("AGENT_CORE_EGRESS_DIR",
                                 str(Path.home() / ".agent-core-egress")))


def _resolve(domain: str) -> list:
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(domain, None,
                                                             proto=socket.IPPROTO_TCP)})
    except Exception:  # noqa: BLE001
        return []


def build_hosts(domains) -> Path:
    EGRESS_DIR.mkdir(parents=True, exist_ok=True)
    doms = sorted({str(d).strip().lower() for d in (domains or []) if str(d).strip()})
    h = hashlib.md5(("\n".join(doms)).encode("utf-8")).hexdigest()[:10]
    fp = EGRESS_DIR / f"hosts_{h}.txt"
    if fp.exists():
        return fp
    lines = ["127.0.0.1 localhost", "::1 localhost"]
    for d in doms:
        base = d[2:] if d.startswith("*.") else d
        for ip in _resolve(base):
            lines.append(f"{ip} {base}")
            if base.startswith("www."):
                lines.append(f"{ip} {base[4:]}")
    try:
        fp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    return fp


def empty_resolv() -> Path:
    EGRESS_DIR.mkdir(parents=True, exist_ok=True)
    fp = EGRESS_DIR / "resolv.empty"
    if not fp.exists():
        try:
            fp.write_text("# DNS 已禁用（只允许 /etc/hosts 中的域名）\n", encoding="utf-8")
        except OSError:
            pass
    return fp


def host_allowed(host: str, allow) -> bool:
    """域名是否在白名单里（example.com 覆盖其子域；*.x.com / * 通配）。"""
    h = (host or "").strip().lower().rstrip(".")
    if not h or not allow:
        return False
    for d in allow:
        d = str(d).strip().lower().rstrip(".")
        if not d:
            continue
        if d == "*":
            return True
        if d.startswith("*."):
            d = d[2:]
        if h == d or h.endswith("." + d):
            return True
    return False

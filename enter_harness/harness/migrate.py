"""三套本地存储后端之间的迁移（记忆 + 知识）。

范围
----
- 迁移对象：记忆（MemoryPort，active 记录）与知识块（KnowledgePort，含全部审核状态）。
- **不迁移**：审计链、运行状态 / 检查点、审批队列等。审计链是逐条追加的
  证据链，跨库拼接会破坏「同一库内单调递增」的前提，因此不能迁移；
  运行状态与具体进程 / 会话绑定，也没有跨库语义。
- 幂等：记忆按 upsert 的 R1 语义（同 tenant/scope/scope_id/agent/kind/key/
  condition 的 active 记录只保留一条），重复导入不会产生重复 active；
  知识块按 id 覆盖，重复导入不会翻倍。

用法
----
库内调用::

    from enter_harness.harness.factory import build_local_ports
    from enter_harness.harness.migrate import migrate_ports

    src = build_local_ports("A", "sqlite")
    dst = build_local_ports("B", "markdown")
    try:
        report = migrate_ports(src, dst, tenant_id="t1")
    finally:
        src.close()
        dst.close()

命令行::

    python -m enter_harness.harness.migrate \
      --from-backend sqlite --from-root A \
      --to-backend markdown --to-root B [--tenant T]
"""
from __future__ import annotations

import argparse
import inspect
import json
from typing import Any

from ..context.model import MemoryRecord
from ..errors import ConfigError
from .factory import BACKENDS, build_local_ports

_MEMORY_FIELDS = tuple(MemoryRecord.__dataclass_fields__)

# 与 KnowledgePort.add() 接受的 chunk dict 对齐
_CHUNK_FIELDS = (
    "id", "tenant_id", "scope", "scope_id", "doc_id", "title", "source",
    "content", "content_hash", "review_status", "version", "created_at",
    "updated_at", "reviewer",
)


def _port(ports: Any, name: str):
    """同时兼容对象属性（LocalPorts）与字典两种端口容器。"""
    port = getattr(ports, name, None)
    if port is None and isinstance(ports, dict):
        port = ports.get(name)
    if port is None:
        raise ConfigError(f"缺少 {name} 端口，无法迁移（检查 build_local_ports 的返回值）")
    return port


def _memory_dump(port: Any, tenant_id: str) -> list[dict]:
    dump = getattr(port, "dump", None)
    if not callable(dump):
        raise ConfigError(
            f"{type(port).__name__} 缺少 dump(tenant_id) 能力，无法导出记忆；"
            "请使用支持迁移的端口实现（StoreMemoryPort / MarkdownMemoryPort / "
            "PostgresMemoryPort）")
    rows = dump(str(tenant_id or ""))
    if not isinstance(rows, list):
        raise ConfigError(
            f"{type(port).__name__}.dump() 应返回 list[dict]，实际为 "
            f"{type(rows).__name__}")
    return rows


def _knowledge_dump(port: Any, tenant_id: str) -> list[dict]:
    dump = getattr(port, "dump", None)
    if not callable(dump):
        raise ConfigError(
            f"{type(port).__name__} 缺少 dump(tenant_id) 能力，无法导出知识；"
            "请使用支持迁移的端口实现（SQLiteKnowledgePort / "
            "MarkdownKnowledgePort / PostgresKnowledgePort）")
    rows = dump(str(tenant_id or ""))
    if not isinstance(rows, list):
        raise ConfigError(
            f"{type(port).__name__}.dump() 应返回 list[dict]，实际为 "
            f"{type(rows).__name__}")
    return rows


def dump_ports(ports: Any, tenant_id: str = "") -> dict:
    """导出记忆与知识为纯数据：``{"memory": [...], "knowledge": [...]}``。

    任一端口缺少 ``dump()`` 时抛 :class:`ConfigError` 并说明缺的是哪一端。
    返回内容可 JSON 序列化，不含连接对象或 ORM 句柄。
    """
    memory = _port(ports, "memory")
    knowledge = _port(ports, "knowledge")
    return {
        "memory": _memory_dump(memory, tenant_id),
        "knowledge": _knowledge_dump(knowledge, tenant_id),
    }


def _load_memory(port: Any, rows: list[dict], errors: list[str]) -> tuple[int, int, int]:
    upsert = getattr(port, "upsert", None)
    if not callable(upsert):
        raise ConfigError(f"{type(port).__name__} 缺少 upsert()，无法写入记忆")
    ok = skipped = failed = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            failed += 1
            errors.append(f"memory[{index}]: 记录不是 dict")
            continue
        try:
            payload = {k: v for k, v in row.items() if k in _MEMORY_FIELDS}
            record = MemoryRecord(**payload)
            result = upsert(record)
        except Exception as exc:  # noqa: BLE001 —— 单条失败不中断整体迁移
            failed += 1
            errors.append(f"memory[{index}] id={row.get('id', '')!r}: {exc}")
            continue
        if not result.get("ok", True):
            skipped += 1
            errors.append(
                f"memory[{index}] id={row.get('id', '')!r}: "
                f"invalid ({result.get('reason') or 'unknown'})")
            continue
        ok += 1
    return ok, skipped, failed


def _load_knowledge(port: Any, rows: list[dict],
                    errors: list[str]) -> tuple[int, int, int]:
    add = getattr(port, "add", None)
    if not callable(add):
        raise ConfigError(f"{type(port).__name__} 缺少 add()，无法写入知识")
    approve = getattr(port, "approve", None)
    archive = getattr(port, "archive", None)
    getter = getattr(port, "get_chunk", None)

    ok = skipped = failed = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            failed += 1
            errors.append(f"knowledge[{index}]: 记录不是 dict")
            continue
        chunk = {k: row[k] for k in _CHUNK_FIELDS if row.get(k) is not None}
        status = str(row.get("review_status") or "")
        chunk_id = str(row.get("id") or "")
        try:
            # 已存在且内容 / 审核状态都一致时不重写：
            # Markdown 后端对「同状态重复 add」会退回 pending，跳过可保持原状态。
            if chunk_id and callable(getter):
                existing = getter(chunk_id, tenant_id=str(row.get("tenant_id") or ""))
                if (existing is not None
                        and str(existing.get("content_hash") or "")
                        == str(row.get("content_hash") or "")
                        and str(existing.get("review_status") or "") == status):
                    ok += 1
                    continue

            # 迁移要保留原审核状态（含 rejected），不触发“内容变了退回 pending”
            if "preserve_status" in inspect.signature(add).parameters:
                new_id = str(add(chunk, preserve_status=True) or chunk_id)
            else:
                new_id = str(add(chunk) or chunk_id)
            if status == "approved" and callable(approve):
                approve(new_id, reviewer=str(row.get("reviewer") or ""))
            elif status == "archived" and callable(archive):
                archive(new_id)
        except Exception as exc:  # noqa: BLE001 —— 单条失败不中断整体迁移
            failed += 1
            errors.append(f"knowledge[{index}] id={chunk_id!r}: {exc}")
            continue
        ok += 1
    return ok, skipped, failed


def load_ports(ports: Any, data: dict) -> dict:
    """把 :func:`dump_ports` 的结果逐条写入目标端口。

    返回 ``{"memory": n, "knowledge": n, "skipped": n, "failed": n,
    "errors": [...]}``：``n`` 是成功处理的条数（含幂等 noop / 已一致跳过），
    ``skipped`` 是因数据不合法而跳过的条数，``failed`` 是写入抛异常的条数。
    """
    memory = _port(ports, "memory")
    knowledge = _port(ports, "knowledge")
    if not isinstance(data, dict):
        raise ValueError("data 必须是 dump_ports() 返回的 dict")

    mem_rows = data.get("memory") or []
    kb_rows = data.get("knowledge") or []
    if not isinstance(mem_rows, list) or not isinstance(kb_rows, list):
        raise ValueError("data['memory'] / data['knowledge'] 必须是 list")

    errors: list[str] = []
    mem_ok, mem_skipped, mem_failed = _load_memory(memory, mem_rows, errors)
    kb_ok, kb_skipped, kb_failed = _load_knowledge(knowledge, kb_rows, errors)
    return {
        "memory": mem_ok,
        "knowledge": kb_ok,
        "skipped": mem_skipped + kb_skipped,
        "failed": mem_failed + kb_failed,
        "errors": errors,
    }


def migrate_ports(source_ports: Any, target_ports: Any,
                  tenant_id: str = "") -> dict:
    """从一个后端整体迁移到另一个后端（记忆 + 知识）。

    返回 ``{"tenant_id", "memory", "knowledge", "skipped", "failed", "errors"}``。
    可重复执行：记忆依赖 upsert 幂等，知识按 id 覆盖。
    """
    data = dump_ports(source_ports, tenant_id)
    report = load_ports(target_ports, data)
    return {
        "tenant_id": str(tenant_id or ""),
        "memory": report["memory"],
        "knowledge": report["knowledge"],
        "skipped": report["skipped"],
        "failed": report["failed"],
        "errors": report["errors"],
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m enter_harness.harness.migrate",
        description="在 sqlite / markdown / postgres 之间迁移记忆与知识"
                    "（不迁移审计链与运行状态）")
    for side in ("from", "to"):
        parser.add_argument(f"--{side}-backend", required=True, choices=BACKENDS,
                            help="源/目标后端")
        parser.add_argument(f"--{side}-root", required=True,
                            help="源/目标根目录（postgres 下仅作占位）")
        parser.add_argument(f"--{side}-dsn", default="",
                            help="postgres DSN；缺省读 HARNESS_PG_DSN / DATABASE_URL")
    parser.add_argument("--tenant", default="", help="只迁移该租户（默认空租户）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：构建两端端口，迁移后关闭；有失败条数时返回 1。"""
    args = _build_arg_parser().parse_args(argv)
    source = build_local_ports(args.from_root, args.from_backend, args.from_dsn)
    target = build_local_ports(args.to_root, args.to_backend, args.to_dsn)
    try:
        report = migrate_ports(source, target, tenant_id=args.tenant)
    finally:
        source.close()
        target.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["failed"] else 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())

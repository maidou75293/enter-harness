"""Markdown 本地记忆：人可读写、可进版本库的 MemoryPort 实现。

布局：
    <root>/<tenant_id>/<scope>/<scope_id>/<id>.md

正文是记忆内容，frontmatter 是内核字段。写入语义与 SQLite 实现一致：
append / supersede / noop / invalid，租户与范围必须显式匹配。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from ..context.model import (MemoryRecord, WriteResult, content_hash,
                             is_active, normalize_scope, validate_record)
from . import markdown_io


class MarkdownMemoryPort:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------ MemoryPort
    def query(self, req=None) -> Sequence[MemoryRecord]:
        req = dict(req or {})
        tenant_id = str(req.get("tenant_id") or "")
        scope = normalize_scope(req.get("scope")) if req.get("scope") else ""
        scope_id = str(req.get("scope_id") or "")
        agent_id = req.get("agent_id")
        status = str(req.get("status") or "active")
        include_expired = bool(req.get("include_expired", False))
        text = str(req.get("query") or "").strip().lower()

        out = []
        for rec in self._scan(tenant_id=tenant_id, scope=scope, scope_id=scope_id):
            if str(rec.status) != status:
                continue
            if agent_id is not None and str(rec.agent_id or "") != str(agent_id):
                continue
            if status == "active" and not include_expired and not is_active(rec):
                continue
            if text:
                hay = f"{rec.content}\n{rec.summary}\n{rec.key}".lower()
                if text not in hay:
                    continue
            out.append(rec)
        out.sort(key=lambda r: float(r.updated_at or 0), reverse=True)
        return out

    def upsert(self, item: MemoryRecord) -> WriteResult:
        err = validate_record(item)
        if err:
            return WriteResult(action="invalid", id=getattr(item, "id", ""),
                               reason=err, ok=False)
        with self._lock:
            item.id = item.id or ("m_" + uuid.uuid4().hex[:12])
            item.content_hash = item.content_hash or content_hash(
                item.summary or item.content)
            existing = self._find_active(item)
            if existing is not None and existing.content_hash == item.content_hash:
                prov = list(existing.provenance or [])
                if item.source and item.source not in [p.get("source") for p in prov]:
                    prov.append({"source": item.source, "at": time.time()})
                existing.provenance = prov
                existing.updated_at = time.time()
                self._write(existing)
                return WriteResult(action="noop", id=existing.id)

            replaced_id = ""
            action = "append"
            if existing is not None:
                existing.status = "superseded"
                existing.status_reason = "superseded"
                existing.superseded_by = item.id
                existing.updated_at = time.time()
                self._write(existing)
                replaced_id = existing.id
                action = "supersede"
            self._write(item)
            return WriteResult(action=action, id=item.id,
                               replaced_id=replaced_id)

    def archive(self, ids: Sequence[str], reason: str = "policy") -> int:
        wanted = {str(x) for x in (ids or ()) if str(x)}
        if not wanted:
            return 0
        count = 0
        with self._lock:
            for rec in self._scan(tenant_id=None):
                if rec.id in wanted and rec.status == "active":
                    rec.status = "archived"
                    rec.status_reason = reason
                    rec.updated_at = time.time()
                    self._write(rec)
                    count += 1
        return count

    def dump(self, tenant_id: str = "") -> list[dict]:
        """导出某租户的 active 记忆为纯数据（可 JSON 序列化）。"""
        rows = self.query({"tenant_id": str(tenant_id or ""),
                           "status": "active", "include_expired": True})
        return [asdict(rec) for rec in rows]

    # ------------------------------------------------------------ 辅助
    def get(self, mid: str) -> MemoryRecord | None:
        for rec in self._scan(tenant_id=None):
            if rec.id == str(mid):
                return rec
        return None

    def _path(self, rec: MemoryRecord) -> Path:
        tenant = markdown_io.safe_name(rec.tenant_id) if rec.tenant_id else "_"
        scope = markdown_io.safe_name(rec.scope or "project")
        scope_id = markdown_io.safe_name(rec.scope_id) if rec.scope_id else "_"
        return self.root / tenant / scope / scope_id / (
            markdown_io.safe_name(rec.id) + ".md")

    def _write(self, rec: MemoryRecord) -> None:
        data = asdict(rec)
        content = str(data.pop("content", "") or "")
        meta = {k: v for k, v in data.items() if v not in (None, "")}
        markdown_io.atomic_write(self._path(rec),
                                 markdown_io.dump(meta, content))

    def _scan(self, *, tenant_id: str | None = None, scope: str = "",
              scope_id: str = ""):
        base = (self.root / (markdown_io.safe_name(tenant_id) if tenant_id else "_")
                if tenant_id is not None else self.root)
        if scope:
            base = base / markdown_io.safe_name(scope)
        if scope_id:
            base = base / markdown_io.safe_name(scope_id)
        for path in markdown_io.iter_files(base):
            meta, body = markdown_io.read(path)
            if tenant_id is not None and str(meta.get("tenant_id") or "") != tenant_id:
                continue
            fields = MemoryRecord.__dataclass_fields__
            kwargs = {k: v for k, v in meta.items() if k in fields}
            kwargs.pop("content", None)
            kwargs["content"] = body
            # Markdown 可被人直接编辑：读时按实际正文重算指纹
            kwargs["content_hash"] = content_hash(
                str(kwargs.get("summary") or body))
            if not kwargs.get("id"):
                continue
            try:
                yield MemoryRecord(**kwargs)
            except Exception:  # noqa: BLE001 —— 手改坏的文件跳过，不拖垮整体
                continue

    def _find_active(self, item: MemoryRecord) -> MemoryRecord | None:
        cond = json.dumps(item.condition or {}, sort_keys=True,
                          ensure_ascii=False, default=str)
        for rec in self._scan(tenant_id=item.tenant_id, scope=item.scope,
                              scope_id=item.scope_id):
            if rec.status != "active":
                continue
            if (str(rec.agent_id or "") == str(item.agent_id or "")
                    and rec.kind == item.kind and rec.key == item.key
                    and json.dumps(rec.condition or {}, sort_keys=True,
                                   ensure_ascii=False, default=str) == cond):
                return rec
        return None

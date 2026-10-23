"""Markdown 本地知识库：一块一个 .md，frontmatter 存来源与审核状态。

布局：
    <root>/<tenant_id>/<scope>/<scope_id>/<id>.md

只有 review_status=approved 的块参与检索；归档块不可再 approve。
"""
from __future__ import annotations

import hashlib
import threading
import time
import uuid
from pathlib import Path
from typing import Sequence

from . import markdown_io
from .knowledge import query_tokens


class MarkdownKnowledgePort:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 写
    def add(self, chunk: dict, preserve_status: bool = False) -> str:
        data = dict(chunk or {})
        content = str(data.get("content") or "").strip()
        source = str(data.get("source") or "").strip()
        if not content:
            raise ValueError("content 不能为空")
        if not source:
            raise ValueError("source 不能为空")
        cid = str(data.get("id") or ("kb_" + uuid.uuid4().hex[:12]))
        old = self.get_chunk(cid, tenant_id=str(data.get("tenant_id") or ""))
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        now = time.time()
        status = str(data.get("review_status") or "").strip().lower()
        if old is not None and not preserve_status:
            if digest == old.get("content_hash") and not status:
                status = str(old.get("review_status") or "pending")
            elif not status or status == str(old.get("review_status") or ""):
                status = "pending"
        if status not in ("pending", "approved", "rejected", "archived"):
            status = "pending"
        rec = {
            "id": cid,
            "tenant_id": str(data.get("tenant_id") or ""),
            "scope": str(data.get("scope") or "project"),
            "scope_id": str(data.get("scope_id") or ""),
            "doc_id": str(data.get("doc_id") or ""),
            "title": str(data.get("title") or ""),
            "source": source,
            "content_hash": digest,
            "review_status": status,
            "version": str(data.get("version") or ""),
            "created_at": float((old or {}).get("created_at")
                                or data.get("created_at") or now),
            "updated_at": float(data.get("updated_at") or now),
            "reviewer": str(data.get("reviewer")
                            or (old or {}).get("reviewer") or ""),
            "content": content,
        }
        with self._lock:
            self._write(rec)
        return cid

    def approve(self, chunk_id: str, reviewer: str = "") -> bool:
        with self._lock:
            rec = self.get_chunk(chunk_id)
            if rec is None or rec.get("review_status") == "archived":
                return False
            rec["review_status"] = "approved"
            rec["reviewer"] = str(reviewer or rec.get("reviewer") or "")
            rec["updated_at"] = time.time()
            self._write(rec)
            return True

    def archive(self, chunk_id: str) -> bool:
        with self._lock:
            rec = self.get_chunk(chunk_id)
            if rec is None:
                return False
            rec["review_status"] = "archived"
            rec["updated_at"] = time.time()
            self._write(rec)
            return True

    # ------------------------------------------------------------ 读
    def search(self, query: str, top_k: int = 4, scope: str = "",
               tenant_id: str = "", scope_id: str = "") -> Sequence[dict]:
        q = str(query or "").strip()
        tokens = query_tokens(q)
        rows = []
        for rec in self._scan(tenant_id=tenant_id or None, scope=scope,
                              scope_id=scope_id):
            if rec.get("review_status") != "approved":
                continue
            if not str(rec.get("source") or "").strip():
                continue
            title = str(rec.get("title") or "").lower()
            body = str(rec.get("content") or "").lower()
            score = 0
            title_hit = 0
            for token in tokens:
                if not token:
                    continue
                if token in title:
                    title_hit += 1
                    score += 3
                score += body.count(token)
            if q and score <= 0:
                continue
            item = {k: v for k, v in rec.items() if k != "content"}
            item["content"] = rec.get("content", "")
            item["score"] = score
            item["_title_hit"] = title_hit
            rows.append(item)
        rows.sort(key=lambda x: (-int(x.get("_title_hit") or 0),
                                 -int(x.get("score") or 0),
                                 -float(x.get("updated_at") or 0)))
        for row in rows:
            row.pop("_title_hit", None)
        return rows[:max(0, int(top_k or 0))] if top_k else rows

    def get_chunk(self, chunk_id: str, tenant_id: str = "") -> dict | None:
        for rec in self._scan(tenant_id=tenant_id or None):
            if str(rec.get("id") or "") == str(chunk_id):
                if rec.get("review_status") == "archived":
                    return None
                return rec
        return None

    def dump(self, tenant_id: str = "") -> list[dict]:
        """导出某租户的全部知识块（含 pending / rejected / archived）。

        字段与 add() 接受的 chunk dict 对齐；丢弃内部编辑标记，
        保证导出结果可 JSON 序列化、可回灌。
        """
        out = []
        for rec in self._scan(tenant_id=str(tenant_id or "")):
            out.append({
                "id": str(rec.get("id") or ""),
                "tenant_id": str(rec.get("tenant_id") or ""),
                "scope": str(rec.get("scope") or ""),
                "scope_id": str(rec.get("scope_id") or ""),
                "doc_id": str(rec.get("doc_id") or ""),
                "title": str(rec.get("title") or ""),
                "source": str(rec.get("source") or ""),
                "content": str(rec.get("content") or ""),
                "content_hash": str(rec.get("content_hash") or ""),
                "review_status": str(rec.get("review_status") or "pending"),
                "version": str(rec.get("version") or ""),
                "created_at": float(rec.get("created_at") or 0.0),
                "updated_at": float(rec.get("updated_at") or 0.0),
                "reviewer": str(rec.get("reviewer") or ""),
            })
        out.sort(key=lambda r: (r["created_at"], r["id"]))
        return out

    # ------------------------------------------------------------ 内部
    def _path(self, rec: dict) -> Path:
        tenant = markdown_io.safe_name(rec["tenant_id"]) if rec["tenant_id"] else "_"
        scope = markdown_io.safe_name(rec.get("scope") or "project")
        scope_id = (markdown_io.safe_name(rec["scope_id"])
                    if rec.get("scope_id") else "_")
        return self.root / tenant / scope / scope_id / (
            markdown_io.safe_name(rec["id"]) + ".md")

    def _write(self, rec: dict) -> None:
        data = dict(rec)
        content = str(data.pop("content", "") or "")
        markdown_io.atomic_write(self._path(rec), markdown_io.dump(data, content))

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
            meta["content"] = body
            actual = hashlib.sha256(body.encode("utf-8")).hexdigest()
            stored = str(meta.get("content_hash") or "")
            # 人改了正文但没重新审核：自动退回 pending，避免旧审核放行新内容
            if stored and stored != actual:
                meta["review_status"] = "pending"
                meta["edited_since_review"] = True
            meta["content_hash"] = actual
            meta.setdefault("created_at", 0.0)
            meta.setdefault("updated_at", 0.0)
            yield meta

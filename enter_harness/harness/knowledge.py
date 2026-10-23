"""KnowledgePort 的默认实现：SQLite 知识块存储。

口径（与 docs/核心模型-定稿-v1.md §3 对齐）：
- 文档 Document → 块 Chunk，入库粒度是块；
- 版本 = 来源时间 + 内容指纹；来源时间缺失时用入库时间兜底，
  内容指纹（content_hash）是版本的权威；
- 检索先按租户 / 可见范围 / 审核状态过滤，再做关键词匹配；
- 只有 review_status='approved' 的块参与检索；
- 无 source 的块不得入库；检索也会再兜底排除。

只依赖标准库 sqlite3，产品可替换为向量库或远端知识库适配器。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Sequence

# 允许的审核状态：pending（待审）/ approved（已审）/ rejected（拒绝）/ archived（归档）
_REVIEW_STATUSES = ("pending", "approved", "rejected", "archived")

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_chunk(
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT '',
  scope_id TEXT NOT NULL DEFAULT '',
  doc_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'pending',
  version TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  reviewer TEXT NOT NULL DEFAULT ''
);
-- 检索主路径：租户 + 范围 + 审核状态
CREATE INDEX IF NOT EXISTS idx_chunk_scope
  ON knowledge_chunk(tenant_id, scope, scope_id, review_status);
CREATE INDEX IF NOT EXISTS idx_chunk_doc
  ON knowledge_chunk(tenant_id, doc_id);
"""

# 英文取小写词（数字也算词），中文按连续汉字段取子串 / 二元组
_EN_WORD = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[一-鿿]+")


def _content_hash(content: str) -> str:
    """内容指纹：对实际入库文本算 sha256，用来判断内容有没有变。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def query_tokens(query: str) -> list[str]:
    """把查询拆成关键词。

    - 英文：小写词，大小写不敏感；
    - 中文：连续汉字段按子串匹配；超过 2 字时拆成二元组，
      避免整句中文因为不连续而完全召回不到。
    """
    text = (query or "").lower()
    found: set[str] = set(_EN_WORD.findall(text))
    for run in _CJK_RUN.findall(text):
        if len(run) <= 2:
            found.add(run)
        else:
            found.update(run[i:i + 2] for i in range(len(run) - 1))
    return sorted(found)


# 兼容旧私有名；新代码用 query_tokens
_tokens = query_tokens


class SQLiteKnowledgePort:
    """基于 SQLite 的默认 KnowledgePort 实现（单文件或内存库）。"""

    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 写入
    def add(self, chunk: dict, preserve_status: bool = False) -> str:
        """写入或更新一个知识块，返回块 id。

        必填 content、source；缺任一或为空白字符串时抛 ValueError。
        content_hash 未给则按内容计算；review_status 默认 'pending'。
        更新时若内容指纹未变且未显式传 review_status，则保留原审核状态，
        避免仅改标题就要求重新审核；内容变化则回到 'pending'。
        """
        if not isinstance(chunk, dict):
            raise TypeError("chunk 必须是 dict")
        content = str(chunk.get("content") or "")
        if not content.strip():
            raise ValueError("知识块缺少 content")
        source = str(chunk.get("source") or "")
        if not source.strip():
            raise ValueError("知识块缺少 source")

        # 未显式给指纹时，一律以实际内容为准
        content_hash = str(chunk.get("content_hash") or "") or _content_hash(content)
        chunk_id = str(chunk.get("id") or "") or uuid.uuid4().hex
        now = time.time()

        with self._lock, self.conn:
            existing = self.conn.execute(
                "SELECT * FROM knowledge_chunk WHERE id=?", (chunk_id,)).fetchone()

            def pick(name: str, default: str = "") -> str:
                """可选字段：显式传值优先，更新时缺省保留旧值。"""
                value = chunk.get(name)
                if value is not None:
                    return str(value)
                if existing is not None:
                    return str(existing[name])
                return default

            # 审核状态：显式值 > 内容未变时沿用旧状态 > 默认待审
            status_value = chunk.get("review_status")
            if status_value is not None:
                review_status = str(status_value)
            elif existing is not None and existing["content_hash"] == content_hash:
                review_status = str(existing["review_status"])
            else:
                review_status = "pending"
            if review_status not in _REVIEW_STATUSES:
                raise ValueError(f"非法 review_status: {review_status}")

            created_at = (float(existing["created_at"]) if existing is not None
                          else float(chunk.get("created_at") or now))
            updated_at = float(chunk.get("updated_at") or now)

            # 版本 = 来源时间 + 内容指纹；缺少来源时间用入库时间兜底
            source_time = chunk.get("source_time")
            version_value = chunk.get("version")
            if version_value is not None:
                version = str(version_value)
            elif (existing is not None
                  and existing["content_hash"] == content_hash
                  and source_time in (None, "")
                  and existing["version"]):
                version = str(existing["version"])
            else:
                stamp = now if source_time in (None, "") else source_time
                version = f"{stamp}:{content_hash}"

            self.conn.execute(
                "INSERT INTO knowledge_chunk("
                " id, tenant_id, scope, scope_id, doc_id, title, source, content,"
                " content_hash, review_status, version, created_at, updated_at, reviewer)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET"
                " tenant_id=excluded.tenant_id, scope=excluded.scope,"
                " scope_id=excluded.scope_id, doc_id=excluded.doc_id,"
                " title=excluded.title, source=excluded.source,"
                " content=excluded.content, content_hash=excluded.content_hash,"
                " review_status=excluded.review_status, version=excluded.version,"
                " created_at=excluded.created_at, updated_at=excluded.updated_at,"
                " reviewer=excluded.reviewer",
                (chunk_id, pick("tenant_id"), pick("scope"), pick("scope_id"),
                 pick("doc_id"), pick("title"), source, content, content_hash,
                 review_status, version, created_at, updated_at,
                 pick("reviewer")))
        return chunk_id

    def approve(self, chunk_id: str, reviewer: str = "") -> bool:
        """人工审核通过；块不存在或已归档时返回 False。"""
        now = time.time()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE knowledge_chunk SET review_status='approved', reviewer=?,"
                " updated_at=? WHERE id=? AND review_status<>'archived'",
                (str(reviewer or ""), now, str(chunk_id)))
            return cur.rowcount > 0

    def archive(self, chunk_id: str) -> bool:
        """归档知识块；块存在（含已归档）返回 True，不存在返回 False。"""
        now = time.time()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE knowledge_chunk SET review_status='archived', updated_at=?"
                " WHERE id=? AND review_status<>'archived'",
                (now, str(chunk_id)))
            if cur.rowcount > 0:
                return True
            row = self.conn.execute(
                "SELECT 1 FROM knowledge_chunk WHERE id=?", (str(chunk_id),)).fetchone()
            return row is not None

    # ------------------------------------------------------------ 检索
    def search(self, query: str, top_k: int = 4, scope: str = "",
               tenant_id: str = "", scope_id: str = "") -> Sequence[dict]:
        """按关键词检索已审核知识块。

        过滤顺序：租户 / scope / scope_id / review_status='approved' /
        排除无 source 的块；再按「标题命中 > 正文命中次数 > 新近度」排序。
        query 为空时直接按 updated_at 倒序返回。
        """
        limit = max(0, int(top_k))
        if limit == 0:
            return []

        sql = ["SELECT rowid AS _rowid, * FROM knowledge_chunk"
               " WHERE review_status='approved' AND source<>''"]
        args: list[Any] = []
        if tenant_id:
            sql.append("AND tenant_id=?")
            args.append(str(tenant_id))
        if scope:
            sql.append("AND scope=?")
            args.append(str(scope))
        if scope_id:
            sql.append("AND scope_id=?")
            args.append(str(scope_id))

        query_text = (query or "").strip()
        if not query_text:
            sql.append("ORDER BY updated_at DESC, rowid DESC LIMIT ?")
            args.append(limit)
            rows = self.conn.execute(" ".join(sql), args).fetchall()
            return [self._hit(row, 0.0) for row in rows]

        tokens = query_tokens(query_text)
        if not tokens:
            return []

        rows = self.conn.execute(" ".join(sql), args).fetchall()
        scored: list[tuple[int, int, float, int, sqlite3.Row]] = []
        for row in rows:
            title_text = str(row["title"] or "").lower()
            content_text = str(row["content"] or "").lower()
            title_hits = sum(title_text.count(token) for token in tokens)
            content_hits = sum(content_text.count(token) for token in tokens)
            if title_hits == 0 and content_hits == 0:
                continue
            # 标题命中权重远高于正文，保证标题相关的块排在前面
            score = title_hits * 10 + content_hits
            scored.append((title_hits, content_hits, score,
                           float(row["updated_at"]), row))

        # 命中次数用负号实现降序；新近度与 rowid 作为稳定兜底
        scored.sort(key=lambda item: (-item[0], -item[1], -item[3],
                                      -item[4]["_rowid"]))
        return [self._hit(item[4], float(item[2])) for item in scored[:limit]]

    def get_chunk(self, chunk_id: str, tenant_id: str = "") -> dict | None:
        """按 id 回取正文；archived 或不存在返回 None。

        与 search 不同，这里允许取回 pending / rejected 块，
        但必须带 review_status，供引用复核时判断可信度。
        """
        sql = "SELECT * FROM knowledge_chunk WHERE id=? AND review_status<>'archived'"
        args: list[Any] = [str(chunk_id)]
        if tenant_id:
            sql += " AND tenant_id=?"
            args.append(str(tenant_id))
        row = self.conn.execute(sql, args).fetchone()
        return self._dict(row) if row is not None else None

    def dump(self, tenant_id: str = "") -> list[dict]:
        """导出某租户的全部知识块（含 pending / rejected / archived）。

        字段与 add() 接受的 chunk dict 对齐，可直接回灌到其它后端；
        只含纯数据，不含连接对象。
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, tenant_id, scope, scope_id, doc_id, title, source,"
                " content, content_hash, review_status, version, created_at,"
                " updated_at, reviewer FROM knowledge_chunk"
                " WHERE tenant_id=? ORDER BY created_at, id",
                (str(tenant_id or ""),)).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _dict(row: sqlite3.Row) -> dict:
        return {key: row[key] for key in row.keys()}

    @classmethod
    def _hit(cls, row: sqlite3.Row, score: float) -> dict:
        """检索命中只暴露调用方需要的字段与得分。"""
        return {
            "id": str(row["id"]),
            "content": str(row["content"]),
            "title": str(row["title"]),
            "source": str(row["source"]),
            "content_hash": str(row["content_hash"]),
            "review_status": str(row["review_status"]),
            "score": score,
            "doc_id": str(row["doc_id"]),
            "tenant_id": str(row["tenant_id"]),
            "scope": str(row["scope"]),
            "scope_id": str(row["scope_id"]),
            "version": str(row["version"]),
            "updated_at": float(row["updated_at"]),
        }

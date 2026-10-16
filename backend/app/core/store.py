"""LanceDB 的读写封装：两张表（分块 + 数据源），够用就好。

刻意不做的事（依据官方文档，2026-10 核对）：
- **不建向量索引**：文档写明「暴力检索适合几十万向量以内，需要 100% 召回时就用它」，
  我们只有几百块，暴力检索又快又准；将来真到了几十万向量，再上 `IVF_PQ` 且度量必须与索引一致。
- **不建标量索引**：`merge_insert` 的 join 列在「未索引行数 > 10000」时才会报 400，本项目远低于此。

关键调用（与文档一致）：
- 建表：`db.create_table(name, schema=..., exist_ok=True)`（幂等打开 + 校验 schema）
- 写入：`table.add(rows)`
- 原子替换：`table.delete("source_id = '...'")`
- upsert：`table.merge_insert("source_id").when_matched_update_all().when_not_matched_insert_all().execute([row])`
- 检索：`table.search(vector).metric("cosine").select([...]).limit(k).to_list()`
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

import lancedb
from lancedb.table import Table

from app.core.schema import CHUNKS_TABLE, SOURCES_TABLE, chunks_schema, sources_schema

# 检索时回传的列：**故意不含 vector**，2048 维的向量没必要来回搬。
# `_distance` 要显式列出来：LanceDB 现在会自动带上并在缺省时打弃用警告，将来不会。
SEARCH_COLUMNS = [
    "chunk_id",
    "source_id",
    "heading",
    "text",
    "kind",
    "rel_path",
    "start",
    "end",
    "images",
    "_distance",
]


class StoreError(RuntimeError):
    """存储层出错（schema/维度不匹配、目录不可用等）。"""


def _existing_dim(db: "lancedb.DBConnection") -> int | None:
    """已存在的 chunks 表当前用了几维；表不存在返回 None。

    用 `list_tables()` 而不是 `table_names()`——后者在当前 lancedb 版本已标记弃用。
    """
    if CHUNKS_TABLE not in db.list_tables().tables:
        return None
    vector_field = db.open_table(CHUNKS_TABLE).schema.field("vector")
    return int(vector_field.type.list_size)


class Store:
    """一个 LanceDB 目录 + 两张表的薄封装。线程安全性由调用方保证（导入串行）。"""

    def __init__(self, db_uri: str | Path, *, dim: int) -> None:
        self.db_uri = str(db_uri)
        Path(self.db_uri).mkdir(parents=True, exist_ok=True)
        self.dim = dim
        self.db = lancedb.connect(self.db_uri)

        # 换了嵌入模型（维度跟着变）时，老库的 schema 对不上：直接给出「怎么处理」的提示，
        # 而不是让 LanceDB 抛一句难懂的 schema 错误，更不能悄悄把两种维度的向量混进一张表。
        existing = _existing_dim(self.db)
        if existing is not None and existing != dim:
            raise StoreError(
                f"向量库现有维度是 {existing}，但当前配置的 EMBED_DIM={dim}。"
                f"换了嵌入模型就得重建库：把目录 {self.db_uri} 删掉（或改指向新目录）后重新导入；"
                f"若只是配错了，把 EMBED_DIM 改回 {existing} 即可。"
            )

        # exist_ok=True：表已存在就复用；此时 LanceDB 也会拿 schema 校验一遍
        self.chunks: Table = self.db.create_table(CHUNKS_TABLE, schema=chunks_schema(dim), exist_ok=True)
        self.sources: Table = self.db.create_table(SOURCES_TABLE, schema=sources_schema(), exist_ok=True)

    # —— 数据源表 ————————————————————————————————————————————————

    def get_source(self, source_id: str) -> dict | None:
        rows = self.sources.search().where(f"source_id = '{source_id}'").limit(1).to_list()
        return rows[0] if rows else None

    def list_sources(self) -> list[dict]:
        return sorted(self.sources.search().to_list(), key=lambda row: row.get("added_at", ""))

    def upsert_source(self, row: dict) -> None:
        """按 source_id 覆盖写：同一个目录重复导入只会更新那一行。"""
        (
            self.sources.merge_insert("source_id")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute([row])
        )

    def delete_source(self, source_id: str, *, purge: bool = True) -> None:
        """删掉数据源登记；purge=True 时连它的分块一起删（重导入前必须先删分块）。"""
        if purge:
            self.drop_source_chunks(source_id)
        self.sources.delete(f"source_id = '{source_id}'")

    # —— 分块表 ————————————————————————————————————————————————

    def add_chunks(self, rows: Sequence[dict]) -> int:
        """批量写入分块；维度或字段不对时 LanceDB 会直接报错，不会写进去半条。"""
        if not rows:
            return 0
        self.chunks.add(list(rows))
        return len(rows)

    def drop_source_chunks(self, source_id: str) -> None:
        """一个数据源的分块全部删除（LanceDB 的主键只是元数据，不保证唯一，所以替换前要显式删）。"""
        self.chunks.delete(f"source_id = '{source_id}'")

    def count_chunks(self, source_id: str | None = None) -> int:
        if source_id is None:
            return self.chunks.count_rows()
        return self.chunks.count_rows(filter=f"source_id = '{source_id}'")

    def stats(self) -> tuple[int, int]:
        """(分块总数, 数据源数量)。"""
        return self.chunks.count_rows(), self.sources.count_rows()

    # —— 检索 ——————————————————————————————————————————————————

    def search(
        self,
        vector: Sequence[float],
        *,
        top_k: int = 20,
        source_ids: Iterable[str] | None = None,
    ) -> list[dict]:
        """余弦近邻检索，返回按距离升序的结果（`_distance` = 1 - 余弦相似度，越小越像）。"""
        if len(vector) != self.dim:
            raise StoreError(f"查询向量维度不符：期望 {self.dim}，实际 {len(vector)}")
        query = self.chunks.search(list(vector), vector_column_name="vector").metric("cosine")
        ids = list(source_ids) if source_ids is not None else []
        if ids:
            quoted = ", ".join(f"'{item}'" for item in ids)
            query = query.where(f"source_id IN ({quoted})", prefilter=True)
        return query.select(SEARCH_COLUMNS).limit(top_k).to_list()

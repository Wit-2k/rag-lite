"""LanceDB 两张表的 Arrow schema 与行构造。

为什么用显式 schema 而不是让 LanceDB 自己猜：
- 向量列必须是**定长**列表，维度写死在 schema 里，写错维度的行会被直接拒绝（多一道保险）；
- 表已存在时 `create_table(..., exist_ok=True)` 会拿 schema 做校验，把嵌入模型换成别的维度会立刻报错，
  而不是悄悄把两种向量混进一张表。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa

# 表名（LanceDB 里就是一个目录名）
CHUNKS_TABLE = "chunks"
SOURCES_TABLE = "sources"


def chunks_schema(dim: int) -> pa.Schema:
    """分块表：一行 = 一个分块 + 它的向量。"""
    return pa.schema(
        [
            pa.field("chunk_id", pa.string()),
            pa.field("source_id", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dim)),
            pa.field("heading", pa.string()),  # 标题面包屑，展示与排序用
            pa.field("text", pa.string()),  # 真正送去嵌入的文本（= 面包屑 + 正文），重排与喂大模型也用它
            pa.field("kind", pa.string()),  # text / table / code
            pa.field("rel_path", pa.string()),  # 源目录内的相对路径，当前恒为 full.md
            pa.field("start", pa.int32()),  # 在 full.md 中的字符区间，便于回溯原文
            pa.field("end", pa.int32()),
            pa.field("char_count", pa.int32()),
            pa.field("images", pa.list_(pa.string())),  # 该块附带的图片文件名
            pa.field("embedded_at", pa.string()),
        ]
    )


def sources_schema() -> pa.Schema:
    """数据源表：一行 = 一个 MinerU 产物目录（含幂等指纹）。"""
    return pa.schema(
        [
            pa.field("source_id", pa.string()),
            pa.field("name", pa.string()),
            pa.field("path", pa.string()),
            pa.field("sha256", pa.string()),  # full.md 的哈希：没变就跳过重新导入
            pa.field("chunk_count", pa.int32()),
            pa.field("image_count", pa.int32()),
            pa.field("embed_model", pa.string()),
            pa.field("embed_dim", pa.int32()),
            pa.field("added_at", pa.string()),
            pa.field("embedded_at", pa.string()),
        ]
    )


def utc_now() -> str:
    """统一用秒精度的 UTC ISO 时间字符串，省得后面为时区头疼。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def source_id_for(path: Path) -> str:
    """由目录绝对路径推出稳定 id：同一个目录反复添加永远是同一条记录，不会越加越多。"""
    import hashlib

    normalized = str(Path(path).resolve()).replace("\\", "/").lower()
    return "src_" + hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def chunk_row(
    *,
    chunk_id: str,
    source_id: str,
    vector: list[float],
    blocking: dict,
    rel_path: str,
    embedded_at: str,
) -> dict:
    """把「分块 + 向量」拼成一行的字典。blocking 是 Block 里要落库的那几项。"""
    return {
        "chunk_id": chunk_id,
        "source_id": source_id,
        "vector": [float(value) for value in vector],
        "heading": blocking["heading"],
        "text": blocking["text"],
        "kind": blocking["kind"],
        "rel_path": rel_path,
        "start": int(blocking["start"]),
        "end": int(blocking["end"]),
        "char_count": int(blocking["char_count"]),
        "images": [Path(p).name for p in blocking["images"]],
        "embedded_at": embedded_at,
    }

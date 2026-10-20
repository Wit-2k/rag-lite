"""导入管线：MinerU 产物目录 → 分块 → 向量化 → 写入 LanceDB。

幂等策略：以内容指纹为准。有 `full.md` 时取它的 sha256；没有 `full.md` 时取「图片相对路径 + 内容哈希」。
同一个目录重复导入时，指纹没变就直接跳过（不花钱、不写库）；变了就
「先删旧分块 → 再写新分块 → 更新数据源行」，避免出现新旧两代分块并存。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.core.chunking import KIND_IMAGE, chunk_images, chunk_markdown
from app.core.openrouter import (
    DEFAULT_BATCH,
    DEFAULT_DIM,
    DEFAULT_EMBED_BASE_URL,
    DEFAULT_MODEL,
    EmbedUsage,
    embed_blocks,
)
from app.core.schema import chunk_row, source_id_for, utc_now
from app.core.store import Store

FULL_MD_NAME = "full.md"

# 进度回调：(已完成, 总数, 当前阶段说明)
ProgressFn = Callable[[int, int, str], None]


@dataclass(slots=True)
class IngestResult:
    source_id: str
    name: str
    path: str
    chunk_count: int
    image_count: int
    skipped: bool
    sha256: str
    reason: str = ""  # 这次为什么导入/跳过（换模型、内容变了……）
    warnings: list[str] = field(default_factory=list)
    usage: EmbedUsage = field(default_factory=EmbedUsage)
    seconds: float = 0.0

    def summary(self) -> str:
        if self.skipped:
            return f"{self.name}: {self.reason or '未变化'}，跳过导入（库里仍有 {self.chunk_count} 块）"
        return (
            f"{self.name}: 写入 {self.chunk_count} 块（含图 {self.image_count} 块），"
            f"{self.usage.requests} 次调用，${self.usage.cost_usd:.6f}，{self.seconds:.1f}s"
        )


def ingest(
    source_dir: str | Path,
    store: Store,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    dim: int = DEFAULT_DIM,
    base_url: str = DEFAULT_EMBED_BASE_URL,
    batch: int = DEFAULT_BATCH,
    force: bool = False,
    on_progress: ProgressFn | None = None,
) -> IngestResult:
    """把一个 MinerU 产物目录导入向量库。

    Args:
        source_dir: MinerU 产物目录。有 `full.md` 时走文本 + 引用图片；
            没有 `full.md` 时只嵌入 `images/` 下的图片（一张图一个块，无文本）。
        store: 已连好的 Store。
        api_key: OpenRouter 密钥。
        force: True 时忽略哈希、无条件重导。
        on_progress: 进度回调，签名 (已完成, 总数, 阶段说明)。
    """
    started = time.perf_counter()
    source_dir = Path(source_dir).resolve()
    full_md = source_dir / FULL_MD_NAME

    source_id = source_id_for(source_dir)

    def report(done: int, total: int, stage: str) -> None:
        if on_progress:
            on_progress(done, total, stage)

    # 0 字节或只有空白的 full.md 等于「没有文本」，走纯图片路径；否则全图目录会被导成 0 块
    text_mode = full_md.is_file() and bool(full_md.read_text(encoding="utf-8").strip())
    if text_mode:
        report(0, 1, "分块")
        chunked = chunk_markdown(full_md)
    else:
        report(0, 1, "扫描图片")
        chunked = chunk_images(source_dir)   # 没有图片时会抛 FileNotFoundError

    existing = store.get_source(source_id)
    # 「没变」= 内容哈希没变 **且** 嵌入模型与维度也没变。
    # 只比哈希的话，换了嵌入模型后会继续用旧模型的向量，检索结果就不可信了。
    same_content = bool(existing) and existing.get("sha256") == chunked.sha256
    same_embedder = bool(existing) and existing.get("embed_model") == model and int(existing.get("embed_dim") or 0) == dim
    if same_content and same_embedder and not force:
        return IngestResult(
            source_id=source_id,
            name=source_dir.name,
            path=str(source_dir),
            chunk_count=store.count_chunks(source_id),
            image_count=existing.get("image_count") or 0,
            skipped=True,
            sha256=chunked.sha256,
            reason="内容与嵌入模型均未变化",
            warnings=chunked.warnings,
            seconds=time.perf_counter() - started,
        )
    if same_content and not same_embedder:
        reason = f"嵌入模型/维度已变为 {model} / {dim} 维，重新向量化"
    elif force:
        reason = "强制重新导入"
    else:
        reason = "full.md 内容已变化" if text_mode else "图片集内容已变化"

    result = embed_blocks(
        chunked.blocks,
        api_key=api_key,
        model=model,
        dim=dim,
        base_url=base_url,
        batch=batch,
        on_progress=lambda done, total: report(done, total, "向量化"),
    )

    def rel_path_for(block) -> str:
        """文本块记 full.md；纯图片块记它自己的相对路径，方便结果回溯。"""
        if block.kind == KIND_IMAGE and block.images:
            return block.images[0].relative_to(source_dir).as_posix()
        return FULL_MD_NAME

    embedded_at = utc_now()
    rows = [
        chunk_row(
            chunk_id=f"{source_id}_{index:05d}",  # 稳定且可读的块 id：源 id + 序号
            source_id=source_id,
            vector=vector,
            blocking={
                "heading": block.heading,
                "text": block.text,
                "kind": block.kind,
                "start": block.start,
                "end": block.end,
                "char_count": block.char_count,
                "images": block.images,
            },
            rel_path=rel_path_for(block),
            embedded_at=embedded_at,
        )
        for index, (block, vector) in enumerate(zip(chunked.blocks, result.vectors))
    ]

    report(len(rows), len(rows), "写库")
    if existing:
        # 先删旧的分块，再写新的：避免同一目录留下两代内容
        store.drop_source_chunks(source_id)
    written = store.add_chunks(rows)
    store.upsert_source(
        {
            "source_id": source_id,
            "name": source_dir.name,
            "path": str(source_dir),
            "sha256": chunked.sha256,
            "chunk_count": written,
            "image_count": sum(1 for block in chunked.blocks if block.images),
            "embed_model": model,
            "embed_dim": result.dim,
            "added_at": (existing or {}).get("added_at") or embedded_at,
            "embedded_at": embedded_at,
        }
    )
    return IngestResult(
        source_id=source_id,
        name=source_dir.name,
        path=str(source_dir),
        chunk_count=written,
        image_count=sum(1 for block in chunked.blocks if block.images),
        skipped=False,
        sha256=chunked.sha256,
        reason=reason,
        warnings=chunked.warnings,
        usage=result.usage,
        seconds=time.perf_counter() - started,
    )


def remove_source(path_or_id: str | Path, store: Store) -> None:
    """从库里移除一个数据源（登记 + 全部分块）。传目录路径或 source_id 都行。"""
    raw = str(path_or_id)
    source_id = raw if raw.startswith("src_") else source_id_for(Path(raw))
    store.delete_source(source_id, purge=True)

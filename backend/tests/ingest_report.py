"""真正的端到端：把两个 MinerU 产物目录导入 LanceDB，然后跨文档检索几个问题。

    cd backend && uv run python tests/ingest_report.py
    # 想推倒重来：加 --force
    # 只看检索、不重新导入：加 --no-ingest

产出（backend/tests/out/，已被 .gitignore 忽略）：
  retrieval.md         跨文档检索结果（余弦距离，尚未重排）
  ingest_summary.md     导入明细、库内统计、幂等验证、磁盘占用

这一步会真花钱调嵌入接口（首次约 $0.015），重复运行会因 full.md 未变化而跳过、不再花钱。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 让 `import app` 生效

from app.core.ingest import IngestResult, ingest  # noqa: E402
from app.core.openrouter import DEFAULT_DIM, DEFAULT_MODEL, EmbedUsage, embed_texts, read_api_key  # noqa: E402
from app.core.store import Store  # noqa: E402

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "lancedb"

# 和前两步用的同一组问题，便于对照；现在库里是两篇文档，跨文档也能找到答案
QUERIES = [
    "研究生国家奖学金的金额和比例是多少？",
    "学位论文延期公开要怎么申请？",
    "学生违纪处分一共有哪几种？",
    "学校校徽是什么样子的？",
    "研究生转专业需要满足什么条件？",
]


def preview(hit: dict, limit: int = 90) -> str:
    """预览正文：`text` 里带标题面包屑，而面包屑已经在单独一列显示，这里去掉避免重复。"""
    text = hit["text"]
    heading = hit.get("heading") or ""
    if heading and text.startswith(f"{heading}\n\n"):
        text = text[len(heading) + 2 :]
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def dir_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def run_ingest(store: Store, dirs: list[Path], api_key: str, *, force: bool) -> list[IngestResult]:
    results: list[IngestResult] = []
    for source_dir in dirs:
        started = time.perf_counter()
        print(f"\n=== 导入 {source_dir.name} …", flush=True)
        result = ingest(
            source_dir,
            store,
            api_key=api_key,
            model=DEFAULT_MODEL,
            dim=DEFAULT_DIM,
            force=force,
            on_progress=lambda done, total, stage: print(
                f"    [{stage}] {done}/{total}  ({time.perf_counter() - started:.1f}s)", flush=True
            ),
        )
        print(f"    {result.summary()}", flush=True)
        results.append(result)
    return results


def run_queries(store: Store, api_key: str, *, top_k: int) -> tuple[EmbedUsage, list[list[dict]]]:
    """把问题向量化，再在**整个库**里检索（不再按文档分开）。"""
    print(f"\n=== 跨文档检索 {len(QUERIES)} 个问题 …", flush=True)
    embedded = embed_texts(QUERIES, api_key=api_key, model=DEFAULT_MODEL, dim=DEFAULT_DIM, input_type="search_query")
    hits = [store.search(vector, top_k=top_k) for vector in embedded.vectors]
    return embedded.usage, hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入 LanceDB 并做跨文档检索")
    parser.add_argument("--sources", default=r"D:\MinerU Outputs", help="存放 MinerU 产物目录的父目录")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="LanceDB 目录")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "out"), help="报告输出目录")
    parser.add_argument("--top-k", type=int, default=5, help="每个问题取前几条")
    parser.add_argument("--force", action="store_true", help="忽略哈希，强制重新导入")
    parser.add_argument("--no-ingest", action="store_true", help="跳过导入，只跑检索")
    args = parser.parse_args(argv)

    api_key = read_api_key()
    db_path = Path(args.db)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    store = Store(db_path, dim=DEFAULT_DIM)

    dirs = sorted(p for p in Path(args.sources).iterdir() if (p / "full.md").is_file())
    if not dirs:
        print(f"没有找到任何 full.md：{args.sources}", file=sys.stderr)
        return 1

    results: list[IngestResult] = []
    if not args.no_ingest:
        results = run_ingest(store, dirs, api_key, force=args.force)
        # 幂等验证：紧接着再导一次第一个源，应当跳过且不再调接口
        print(f"\n=== 幂等验证：再导一次 {dirs[0].name} …", flush=True)
        again = ingest(dirs[0], store, api_key=api_key, model=DEFAULT_MODEL, dim=DEFAULT_DIM)
        print(f"    {again.summary()}", flush=True)
        results.append(again)

    chunks, sources = store.stats()
    print(f"\n库内：{chunks} 块 / {sources} 个数据源，磁盘占用 {dir_size(db_path) / 1048576:.1f}MB")

    query_usage, hits = run_queries(store, api_key, top_k=args.top_k)

    names = {row["source_id"]: row["name"] for row in store.list_sources()}
    lines = [
        "# 跨文档检索结果（余弦距离，未重排）",
        "",
        f"- 库：`{db_path}`（{chunks} 块 / {sources} 个数据源）",
        f"- 检索方式：`store.search(vector).metric(\"cosine\")`，每问取前 {args.top_k} 条",
        "- 相似度 = 1 - 距离（LanceDB 的 `_distance` 对 cosine 就是 1 - 余弦相似度）",
        "",
    ]
    for question, group in zip(QUERIES, hits):
        lines += [f"## 问：{question}", "", "| # | 相似度 | 数据源 | 标题面包屑 | 原文位置 | 预览 |", "| --- | --- | --- | --- | --- | --- |"]
        for rank, hit in enumerate(group, start=1):
            lines.append(
                f"| {rank} | {1 - hit['_distance']:.3f} | {names.get(hit['source_id'], hit['source_id'])} | "
                f"{hit['heading'] or '（前言）'} | [{hit['start']}, {hit['end']}) | {preview(hit)} |"
            )
        lines.append("")

    (out_dir / "retrieval.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    stored = {row["source_id"]: row for row in store.list_sources()}
    by_id = {result.source_id: result for result in results}
    summary = [
        "# 导入报表",
        "",
        f"- 库目录：`{db_path}`（{dir_size(db_path) / 1048576:.1f}MB）",
        f"- 向量维度：{DEFAULT_DIM}，模型：`{DEFAULT_MODEL}`",
        f"- 库内合计：**{chunks} 块 / {sources} 个数据源**",
        "",
        "## 库内数据源",
        "",
        "| 数据源 | 块数 | 含图块 | sha256(前16) | 导入时间 | 本次操作 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in sorted(stored.values(), key=lambda item: item["name"]):
        result = by_id.get(row["source_id"])
        if result is None:
            action = "未导入（--no-ingest）"
        elif result.skipped:
            action = "跳过（full.md 未变化）"
        else:
            action = f"写入成功（${result.usage.cost_usd:.6f} / {result.seconds:.1f}s）"
        summary.append(
            f"| {row['name']} | {row['chunk_count']} | {row.get('image_count', '-')} | "
            f"`{row['sha256'][:16]}…` | {row['embedded_at']} | {action} |"
        )
    summary += [
        "",
        "## 检索侧用量",
        "",
        f"- 问题向量化：{query_usage.requests} 次调用，{query_usage.prompt_tokens} token，"
        f"${query_usage.cost_usd:.6f}",
        "- 检索结果见 `retrieval.md`",
        "",
    ]
    (out_dir / "ingest_summary.md").write_text("\n".join(summary), encoding="utf-8")

    print(f"\n→ {out_dir / 'retrieval.md'}")
    print(f"→ {out_dir / 'ingest_summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

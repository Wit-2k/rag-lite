"""重排对照实验：同一个问题，先余弦取 top-N，再用 rerank-3 重排，看排序怎么变。

    cd backend && uv run python tests/rerank_report.py
    uv run python tests/rerank_report.py --pool 30 --context-n 3

产出 backend/tests/out/rerank.md：每个问题一张「余弦 top3 vs 重排 top3」对照表，
外加完整召回池里的名次升降，用来判断重排到底有没有用（重点看榜首有没有换对）。

费用参考：每次重排 20 篇文档约 $0.0003，5 个问题合计不到 $0.002。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 让 `import app` 生效

from app.core.openrouter import (  # noqa: E402
    DEFAULT_DIM,
    DEFAULT_MODEL,
    DEFAULT_RERANK_MODEL,
    embed_texts,
    read_api_key,
    rerank,
)
from app.core.store import Store  # noqa: E402

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "lancedb"

QUERIES = [
    "研究生国家奖学金的金额和比例是多少？",
    "学位论文延期公开要怎么申请？",
    "学生违纪处分一共有哪几种？",
    "学校校徽是什么样子的？",
    "研究生转专业需要满足什么条件？",
]


def preview(hit: dict, limit: int = 80) -> str:
    text = hit["text"]
    heading = hit.get("heading") or ""
    if heading and text.startswith(f"{heading}\n\n"):
        text = text[len(heading) + 2 :]
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def label(hit: dict, names: dict[str, str]) -> str:
    return f"{names.get(hit['source_id'], hit['source_id'])} · {hit['heading'] or '（前言）'}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="余弦检索 vs 重排对照")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="LanceDB 目录")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "out"), help="报告输出目录")
    parser.add_argument("--pool", type=int, default=20, help="先用余弦召回多少条再重排")
    parser.add_argument("--context-n", type=int, default=3, help="最终要返回给大模型的前几条")
    args = parser.parse_args(argv)

    api_key = read_api_key()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    store = Store(Path(args.db), dim=DEFAULT_DIM)
    names = {row["source_id"]: row["name"] for row in store.list_sources()}
    chunks, sources = store.stats()

    print(f"库：{chunks} 块 / {sources} 源；每问余弦召回 {args.pool} 条再重排取前 {args.context_n} 条")
    embedded = embed_texts(QUERIES, api_key=api_key, model=DEFAULT_MODEL, dim=DEFAULT_DIM, input_type="search_query")

    lines = [
        "# 余弦检索 vs 重排（rerank-3）",
        "",
        f"- 库：`{args.db}`（{chunks} 块 / {sources} 个数据源）",
        f"- 流程：问题向量化 → 余弦召回 {args.pool} 条 → `{DEFAULT_RERANK_MODEL}` 重排 → 取前 {args.context_n} 条",
        "- 「余弦名次」是召回时的原始顺序，「重排名次」是重排后的顺序；看榜首有没有换对。",
        "",
    ]
    total_tokens = 0
    total_cost = 0.0
    changed_first = 0

    for question, vector in zip(QUERIES, embedded.vectors):
        hits = store.search(vector, top_k=args.pool)
        started = time.perf_counter()
        result = rerank(question, [hit["text"] for hit in hits], api_key=api_key)
        elapsed = time.perf_counter() - started
        total_tokens += result.tokens
        total_cost += result.cost_usd

        cosine_rank = {index: position + 1 for position, index in enumerate(range(len(hits)))}
        rerank_rank = {hit_index: position + 1 for position, hit_index in enumerate(result.order)}
        score_of = {hit_index: score for hit_index, score in zip(result.order, result.scores)}

        top_after = result.order[: args.context_n]
        top_before = list(range(min(args.context_n, len(hits))))
        if top_after and top_before and top_after[0] != top_before[0]:
            changed_first += 1

        print(f"\n=== {question}")
        print(f"    余弦 top{args.context_n}: " + " | ".join(label(hits[i], names) for i in top_before))
        print(f"    重排 top{args.context_n}: " + " | ".join(label(hits[i], names) for i in top_after))
        print(f"    重排 {result.requests} 次调用 {result.tokens} token ${result.cost_usd:.6f} {elapsed:.1f}s")

        lines += [
            f"## 问：{question}",
            "",
            f"余弦召回 {len(hits)} 条，重排用量 {result.tokens} token / ${result.cost_usd:.6f} / {elapsed:.1f}s",
            "",
            f"**重排后 top{args.context_n}**",
            "",
            "| # | 重排分 | 余弦名次 | 数据源 | 标题面包屑 | 预览 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for position, hit_index in enumerate(top_after, start=1):
            hit = hits[hit_index]
            lines.append(
                f"| {position} | {score_of[hit_index]:.3f} | {cosine_rank[hit_index]} | "
                f"{names.get(hit['source_id'], hit['source_id'])} | {hit['heading'] or '（前言）'} | {preview(hit)} |"
            )
        lines += [
            "",
            f"**余弦原 top{args.context_n}**（对照）",
            "",
            "| # | 相似度 | 重排名次 | 数据源 | 标题面包屑 | 预览 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for position, hit_index in enumerate(top_before, start=1):
            hit = hits[hit_index]
            lines.append(
                f"| {position} | {1 - hit['_distance']:.3f} | {rerank_rank[hit_index]} | "
                f"{names.get(hit['source_id'], hit['source_id'])} | {hit['heading'] or '（前言）'} | {preview(hit)} |"
            )

        movers = [
            (hit_index, cosine_rank[hit_index], rerank_rank[hit_index])
            for hit_index in range(len(hits))
            if abs(cosine_rank[hit_index] - rerank_rank[hit_index]) >= 3
        ]
        movers.sort(key=lambda item: item[2])
        if movers:
            lines += ["", "**名次变化 ≥3 的块**（升幅最大的在前）", "", "| 余弦名次 → 重排名次 | 重排分 | 标题面包屑 | 预览 |", "| --- | --- | --- | --- |"]
            for hit_index, before, after in movers[:6]:
                hit = hits[hit_index]
                lines.append(
                    f"| {before} → {after} | {score_of[hit_index]:.3f} | {hit['heading'] or '（前言）'} | {preview(hit, 60)} |"
                )
        lines.append("")

    lines += [
        "## 汇总",
        "",
        f"- 5 个问题里榜首被换掉的有 **{changed_first}** 个",
        f"- 重排合计：{total_tokens} token，${total_cost:.6f}",
        f"- 问题向量化：{embedded.usage.requests} 次调用，{embedded.usage.prompt_tokens} token，${embedded.usage.cost_usd:.6f}",
        "",
    ]
    (out_dir / "rerank.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n榜首被换掉：{changed_first}/5；重排合计 {total_tokens} token，${total_cost:.6f}")
    print(f"→ {out_dir / 'rerank.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""把两个数据源的分块结果真正调 API 变成向量，并写报告供人工检查。

    cd backend && uv run python tests/embedding_report.py
    # 先小规模试跑，确认形状和花费：
    uv run python tests/embedding_report.py --limit 20

产出（默认 backend/tests/out/，已被 .gitignore 忽略）：
  <源名>.vectors.npz        向量矩阵（float32，n×2048）+ 每块的溯源信息
  <源名>.embeddings.md      逐块一览 + 用量/花费 + 相似度自检（证明向量确实有用）
  embeddings_summary.md     两个源的合计

"相似度自检"用余弦相似度在**同一份数据内**检索几个中文问题，把 top 3 的块打印出来。
它不涉及重排，只是让人眼确认「向量把相近内容的块排到一起了」。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 让 `import app` 生效

from app.core.chunking import Block, chunk_markdown  # noqa: E402
from app.core.openrouter import (  # noqa: E402
    DEFAULT_DIM,
    DEFAULT_MODEL,
    EmbedUsage,
    embed_blocks,
    embed_texts,
    read_api_key,
)

# 自检问题：本科目语料（广东工业大学研究生手册）里真实存在答案的问题
SELF_CHECK_QUERIES = [
    "研究生国家奖学金的金额和比例是多少？",
    "学位论文延期公开要怎么申请？",
    "学生违纪处分一共有哪几种？",
    "学校校徽是什么样子的？",
    "研究生转专业需要满足什么条件？",
]

PREVIEW_CHARS = 120
OUT_DIR = Path(__file__).resolve().parent / "out"


def _preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    """把多行正文压成一行短预览，方便在表格里看。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _usage_rows(usage: EmbedUsage, blocks: int, elapsed: float) -> list[str]:
    return [
        f"| 调用次数 | {usage.requests} |",
        f"| 输入 token | {usage.prompt_tokens:,} |",
        f"| 其中图片 token | {usage.image_tokens:,} |",
        f"| 上游计费 | ${usage.cost_usd:.6f} |",
        f"| 接口耗时合计 | {usage.seconds:.1f}s（实际墙钟 {elapsed:.1f}s） |",
        f"| 平均每块 | ${usage.cost_usd / blocks:.8f} |" if blocks else "| 平均每块 | - |",
    ]


def _save_npz(path: Path, blocks: list[Block], vectors: list[list[float]], dim: int) -> None:
    """向量存成 npz：后面入库（LanceDB）直接读它，不必重复花钱调 API。"""
    matrix = np.asarray(vectors, dtype=np.float32)
    meta = [
        {
            "index": index,
            "kind": block.kind,
            "heading": block.heading,
            "level": block.level,
            "start": block.start,
            "end": block.end,
            "char_count": block.char_count,
            "images": [str(p) for p in block.images],
        }
        for index, block in enumerate(blocks)
    ]
    np.savez_compressed(path, vectors=matrix, meta=np.asarray(json.dumps(meta, ensure_ascii=False)), dim=np.asarray(dim))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对分块结果做向量化并输出检查报告")
    parser.add_argument("--sources", default=r"D:\MinerU Outputs", help="存放 MinerU 产物目录的父目录")
    parser.add_argument("--out", default=str(OUT_DIR), help="报告输出目录")
    parser.add_argument("--limit", type=int, default=0, help="每个数据源最多处理多少块（0=全部，用于试跑）")
    parser.add_argument("--batch", type=int, default=16, help="一次请求塞多少条文本")
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM, help="向量维度：256/512/1024/2048")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="嵌入模型")
    parser.add_argument("--no-self-check", action="store_true", help="跳过相似度自检")
    args = parser.parse_args(argv)

    api_key = read_api_key()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    docs = sorted(p / "full.md" for p in Path(args.sources).iterdir() if (p / "full.md").is_file())
    if not docs:
        print(f"没有找到任何 full.md：{args.sources}", file=sys.stderr)
        return 1

    summary = [
        "# 向量化报告汇总",
        "",
        f"- 数据源父目录：`{args.sources}`",
        f"- 模型：`{args.model}`，维度 {args.dim}，批大小 {args.batch}",
        "",
        "| 数据源 | 块数 | 含图块 | 调用次数 | 输入 token | 图片 token | 费用 | 耗时 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    total_cost = 0.0

    for full_md in docs:
        chunked = chunk_markdown(full_md)
        blocks = chunked.blocks[: args.limit] if args.limit else chunked.blocks
        rich = sum(1 for block in blocks if block.images)

        print(f"\n=== {full_md.parent.name}：{len(blocks)} 块（含图 {rich} 块），开始向量化…")
        started = time.perf_counter()

        def progress(done: int, total: int, _started=started) -> None:
            print(f"    {done}/{total} 块  已用 {time.perf_counter() - _started:.1f}s", flush=True)

        result = embed_blocks(
            blocks,
            api_key=api_key,
            model=args.model,
            dim=args.dim,
            batch=args.batch,
            on_progress=progress,
        )
        elapsed = time.perf_counter() - started

        _save_npz(out_dir / f"{full_md.parent.name}.vectors.npz", blocks, result.vectors, result.dim)

        matrix = np.asarray(result.vectors, dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1)

        lines = [
            f"# 向量化结果 · {full_md.parent.name}",
            "",
            f"- 分块文件：`{full_md}`（sha256 `{chunked.sha256[:16]}…`）",
            f"- 向量：{matrix.shape[0]} 条 × {matrix.shape[1]} 维（float32）",
            f"- 向量文件：`{full_md.parent.name}.vectors.npz`",
            "",
            "## 用量与花费",
            "",
            "| 项 | 值 |",
            "| --- | --- |",
            *_usage_rows(result.usage, len(blocks), elapsed),
            "",
            "## 逐块一览",
            "",
            "| # | 类型 | 字符 | 图 | 向量 L2 范数 | 标题面包屑 | 预览 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for index, (block, norm) in enumerate(zip(blocks, norms), start=1):
            lines.append(
                f"| {index} | {block.kind} | {block.char_count} | {len(block.images)} | {norm:.3f} | "
                f"{block.heading or '（前言）'} | {_preview(block.body)} |"
            )

        if not args.no_self_check:
            lines += [
                "",
                "## 相似度自检（余弦，未重排）",
                "",
                "在同一份数据的向量里检索下列问题，列出最相近的 3 块。",
                "",
            ]
            query_result = embed_texts(
                SELF_CHECK_QUERIES,
                api_key=api_key,
                model=args.model,
                dim=args.dim,
                input_type="search_query",  # 查询侧要和入库侧的 input_type 区分开
            )
            queries = np.asarray(query_result.vectors, dtype=np.float32)
            query_norms = np.linalg.norm(queries, axis=1)
            similarity = (queries @ matrix.T) / np.outer(query_norms, np.maximum(norms, 1e-9))

            for position, question in enumerate(SELF_CHECK_QUERIES):
                scores = similarity[position]
                top = np.argsort(-scores)[:3]
                lines.append(f"### 问：{question}")
                lines.append("")
                for rank, block_index in enumerate(top, start=1):
                    block = blocks[int(block_index)]
                    lines.append(
                        f"{rank}. **{scores[block_index]:.3f}** · {block.heading or '（前言）'} "
                        f"· [{block.start}, {block.end}) · {_preview(block.body, 90)}"
                    )
                lines.append("")
            result_usage = result.usage + query_result.usage
        else:
            result_usage = result.usage

        (out_dir / f"{full_md.parent.name}.embeddings.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        summary.append(
            f"| {full_md.parent.name} | {len(blocks)} | {rich} | {result_usage.requests} | "
            f"{result_usage.prompt_tokens:,} | {result_usage.image_tokens:,} | "
            f"${result_usage.cost_usd:.6f} | {elapsed:.1f}s |"
        )
        total_cost += result_usage.cost_usd

        print(f"    完成：{result_usage.requests} 次调用，${result_usage.cost_usd:.6f}，{elapsed:.1f}s")
        print(f"    → {out_dir / f'{full_md.parent.name}.embeddings.md'}")
        print(f"    → {out_dir / f'{full_md.parent.name}.vectors.npz'}")

    summary += ["", f"**合计费用：${total_cost:.6f}**", ""]
    (out_dir / "embeddings_summary.md").write_text("\n".join(summary), encoding="utf-8")
    print(f"\n汇总：{out_dir / 'embeddings_summary.md'}（合计 ${total_cost:.6f}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

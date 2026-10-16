"""跑一遍分块并把结果写成可读报告，供人工检查分块质量。

    cd backend && uv run python tests/chunking_report.py
    # 或指定路径/参数
    uv run python tests/chunking_report.py --sources "D:/MinerU Outputs" --chunk-size 1000

产出（默认写到 backend/tests/out/）：
  summary.md              两个数据源的汇总与逐源统计
  <源名>.chunks.md        逐块可读预览（正文截断）
  <源名>.chunks.json      全量字段，无截断
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 让 `import app` 生效

from app.core.chunking import ChunkResult, chunk_markdown  # noqa: E402

PREVIEW_LIMIT = 800


def _stat_rows(stats: dict[str, float]) -> list[tuple[str, str]]:
    keys = [
        ("sections", "标题 section 数"),
        ("atomic_spans", "原子块（表格/代码）数"),
        ("blocks", "分块总数"),
        ("blocks_text", "  正文块"),
        ("blocks_table", "  表格块"),
        ("blocks_code", "  代码块"),
        ("blocks_merged", "小块并入的次数"),
        ("blocks_tiny_left", "仍过小的正文块"),
        ("chars_min", "块字符数 min"),
        ("chars_median", "块字符数 median"),
        ("chars_p90", "块字符数 p90"),
        ("chars_max", "块字符数 max"),
        ("chars_total", "块字符数合计"),
        ("images_referenced", "full.md 引用的图片"),
        ("images_attached", "已归入块的图片"),
        ("images_dropped", "因上限丢弃的图片"),
        ("warnings", "警告数"),
    ]
    return [(label, f"{stats[key]:,.0f}") for key, label in keys if key in stats]


def _fence(text: str) -> str:
    ticks = "````" if "```" in text else "```"
    return f"{ticks}text\n{text}\n{ticks}"


def _write_blocks_md(path: Path, source: Path, full_md: Path, result: ChunkResult, preview: int) -> None:
    lines: list[str] = [
        f"# 分块结果 · {source.name}",
        "",
        f"- 产物目录：`{source}`",
        f"- 分块对象：`{full_md.name}`（{full_md.stat().st_size:,} 字节，sha256 `{result.sha256[:16]}…`）",
        "- 说明：`嵌入文本 = 标题面包屑 + 空行 + 正文`；下面的 `正文` 即原文切片，`[start, end)` 是它在 `full.md` 中的字符区间。",
        "",
        "## 统计",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        *[f"| {label} | {value} |" for label, value in _stat_rows(result.stats)],
        "",
    ]
    if result.warnings:
        lines += ["## 警告", "", *[f"- {w}" for w in result.warnings], ""]
    for index, block in enumerate(result.blocks, start=1):
        images = "、".join(p.name for p in block.images) or "无"
        lines += [
            f"## 块 {index}　`{block.kind}`　{block.char_count} 字符　[{block.start}, {block.end})",
            "",
            f"- 标题面包屑：{block.heading or '（无，正文前的封面/前言）'}（层级 {block.level}）",
            f"- 附图：{images}",
            "",
        ]
        body = block.body
        if len(body) > preview:
            body = f"{body[:preview]}\n……（正文共 {len(block.body)} 字符，此处只显示前 {preview} 字符，全文见 JSON）"
        lines += [_fence(body), ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_blocks_json(path: Path, source: Path, full_md: Path, result: ChunkResult) -> None:
    payload = {
        "source": str(source),
        "full_md": str(full_md),
        "sha256": result.sha256,
        "stats": result.stats,
        "warnings": result.warnings,
        "blocks": [
            {
                "index": index,
                "kind": block.kind,
                "heading": block.heading,
                "level": block.level,
                "start": block.start,
                "end": block.end,
                "char_count": block.char_count,
                "body": block.body,
                "images": [str(p) for p in block.images],
            }
            for index, block in enumerate(result.blocks, start=1)
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对 MinerU 产物跑分块并输出检查报告")
    parser.add_argument("--sources", default=r"D:\MinerU Outputs", help="存放 MinerU 产物目录的父目录")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "out"), help="报告输出目录")
    parser.add_argument("--chunk-size", type=int, default=1000, help="正文块字符上限")
    parser.add_argument("--min-chunk-size", type=int, default=120, help="合并过小片段的下限")
    parser.add_argument("--max-table-chars", type=int, default=2000, help="表格/代码块超过它才拆")
    parser.add_argument("--preview", type=int, default=PREVIEW_LIMIT, help="报告里每块正文预览字符数")
    args = parser.parse_args(argv)

    sources_root = Path(args.sources)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = sorted(p / "full.md" for p in sources_root.iterdir() if (p / "full.md").is_file())
    if not targets:
        print(f"没有找到任何 full.md：{sources_root}", file=sys.stderr)
        return 1

    summary: list[str] = [
        "# 分块报告汇总",
        "",
        f"- 数据源父目录：`{sources_root}`",
        f"- 参数：chunk_size={args.chunk_size}，min_chunk_size={args.min_chunk_size}，"
        f"max_table_chars={args.max_table_chars}",
        "",
        "| 数据源 | 块数 | 正文 | 表格 | 代码 | median | p90 | max | 图片 | 警告 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    for full_md in targets:
        source = full_md.parent
        result = chunk_markdown(
            full_md,
            chunk_size=args.chunk_size,
            min_chunk_size=args.min_chunk_size,
            max_table_chars=args.max_table_chars,
        )
        _write_blocks_md(out_dir / f"{source.name}.chunks.md", source, full_md, result, args.preview)
        _write_blocks_json(out_dir / f"{source.name}.chunks.json", source, full_md, result)

        s = result.stats
        summary.append(
            f"| {source.name} | {s['blocks']:.0f} | {s['blocks_text']:.0f} | {s['blocks_table']:.0f} | "
            f"{s['blocks_code']:.0f} | {s['chars_median']:.0f} | {s['chars_p90']:.0f} | {s['chars_max']:.0f} | "
            f"{s['images_attached']:.0f}/{s['images_referenced']:.0f} | {s['warnings']:.0f} |"
        )

        print(f"\n=== {source.name} ===")
        print(f"  full.md {full_md.stat().st_size:,} 字节  sha256 {result.sha256[:16]}…")
        for label, value in _stat_rows(s):
            print(f"  {label.strip():<22} {value:>10}")
        for warning in result.warnings:
            print(f"  [警告] {warning}")
        print(f"  → {out_dir / f'{source.name}.chunks.md'}")
        print(f"  → {out_dir / f'{source.name}.chunks.json'}")

    summary_path = out_dir / "summary.md"
    summary_path.write_text("\n".join(summary) + "\n", encoding="utf-8")
    print(f"\n汇总：{summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

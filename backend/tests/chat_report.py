"""真实对话演示：多轮历史默认全带，清空上下文后立刻「失忆」。

    cd backend && uv run python tests/chat_report.py

流程（4 轮，全部真调 API）：
  1. 问「研究生国家奖学金奖励标准」→ 应引 3 万/2 万；
  2. 追问「那学业奖学金呢」→ 这一轮靠历史才知道「那…呢」指什么（第 1 轮问答已在 messages 里）；
  3. 打印上下文轮数，然后 clear()；
  4. 再问「我上一条问的是什么」→ 历史已清空，模型应当说不知道（证明清空真的生效）。

产出 backend/tests/out/chat.md：逐轮的问题、命中的来源、回答全文、用量与费用。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 让 `import app` 生效

from app.core.openrouter import DEFAULT_DIM  # noqa: E402
from app.core.query import ChatSession, DoneEvent, RetrievalEvent, TokenEvent, load_chat_config  # noqa: E402
from app.core.store import Store  # noqa: E402

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "lancedb"

TURNS = [
    ("研究生国家奖学金的奖励标准是多少？", "history"),
    ("那学业奖学金呢？", "history"),  # 靠历史才知道「那…」指的是奖学金
    ("我上一条问的是什么？", "cleared"),  # 先 clear 再问，模型应当不知道
]


def preview(text: str, limit: int = 70) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实多轮对话演示")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="LanceDB 目录")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "out"), help="报告输出目录")
    parser.add_argument("--top-k", type=int, default=20, help="余弦召回条数")
    parser.add_argument("--context-n", type=int, default=3, help="送给大模型的片段数")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    store = Store(Path(args.db), dim=DEFAULT_DIM)
    config = load_chat_config()
    session = ChatSession(
        store,
        api_key=config.api_key,
        chat_model=config.model,
        chat_base_url=config.base_url,
        retrieve_top_k=args.top_k,
        context_n=args.context_n,
    )

    chunks, sources = store.stats()
    print(f"库：{chunks} 块 / {sources} 源；回答模型：{config.model}（{config.base_url}）")

    lines = [
        "# 真实对话记录",
        "",
        f"- 库：`{args.db}`（{chunks} 块 / {sources} 个数据源）",
        f"- 回答模型：`{config.model}`（{config.base_url}）",
        f"- 检索：余弦召回 {args.top_k} 条 → `voyageai/rerank-3` 重排 → 取前 {args.context_n} 条",
        f"- 上下文策略：每次提问都带上全部历史（`ChatSession.ask` 默认行为），`clear()` 清空",
        "",
    ]
    total_cost = 0.0

    for turn, (question, mode) in enumerate(TURNS, start=1):
        if mode == "cleared":
            cleared = session.clear()
            print(f"\n=== 第 {turn} 轮：调用 clear() 清掉 {cleared} 条历史（{cleared // 2} 轮）")
            lines += [f"## 第 {turn} 轮（提问前先 clear()）", "", f"`clear()` 清掉 **{cleared}** 条历史消息。", ""]
        else:
            print(f"\n=== 第 {turn} 轮（带上 {session.turns} 轮历史）")
            lines += [f"## 第 {turn} 轮（带上 {session.turns} 轮历史）", ""]

        lines += [f"**问：{question}**", ""]
        print(f"问：{question}")

        sources_seen = []
        answer_parts: list[str] = []
        started = time.perf_counter()
        for event in session.ask(question):
            if isinstance(event, RetrievalEvent):
                sources_seen = event.sources
                for source in sources_seen:
                    print(f"    [来源 {source.rank}] {source.name} · {source.heading} （重排 {source.rerank_score:.3f}）")
            elif isinstance(event, TokenEvent):
                answer_parts.append(event.text)
            elif isinstance(event, DoneEvent):
                usage = event.usage
                total_cost += usage.cost_usd
                print(f"答：{event.answer}")
                print(
                    f"    {event.seconds:.1f}s，prompt {usage.prompt_tokens} / completion "
                    f"{usage.completion_tokens} token，${usage.cost_usd:.6f}"
                )
                lines += [
                    "**命中的来源**",
                    "",
                    "| # | 数据源 | 标题面包屑 | 重排分 | 余弦距离 |",
                    "| --- | --- | --- | --- | --- |",
                    *[
                        f"| {source.rank} | {source.name} | {source.heading or '（前言）'} | "
                        f"{source.rerank_score:.3f} | {source.cosine_distance:.3f} |"
                        for source in sources_seen
                    ],
                    "",
                    "**答：**",
                    "",
                    event.answer,
                    "",
                    f"（{event.seconds:.1f}s，prompt {usage.prompt_tokens} / completion "
                    f"{usage.completion_tokens} token，${usage.cost_usd:.6f}）",
                    "",
                ]

    lines += [
        "## 汇总",
        "",
        f"- 本次对话合计费用：**${total_cost:.6f}**",
        f"- 结束时上下文轮数：{session.turns}",
        "",
    ]
    (out_dir / "chat.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n合计 ${total_cost:.6f}；→ {out_dir / 'chat.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

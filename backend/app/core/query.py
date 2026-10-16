"""问答管线：提问 → 余弦召回 → 重排 → 取前 N 条 → 交给大模型流式回答。

对话历史的行为（按需求定）：
- **每次提问默认带上全部历史**：`ChatSession.ask()` 会把历史上每一轮的「用户原文 + 助手回答」
  按顺序放进 messages，再在最后一条消息里附上这一次检索到的参考片段；
- 历史里只存**用户原始问题**，不存参考片段。片段每次都重新检索，历史就不会随轮数膨胀；
- 历史由调用方注入（HTTP 层按会话从 SQLite 读出来）；`ChatSession.clear()` 只清内存里这份，
  真正「清空某个会话」是删库里的消息（`POST /api/chat/clear`）。

回答生成走 `ChatStream`（OpenAI Compatible），逐段产出文本；管线自己产出三类事件：
`RetrievalEvent`（这次用到的片段）→ 若干 `TokenEvent`（文本增量）→ `DoneEvent`（完整回答 + 用量）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from app.core.openrouter import (
    DEFAULT_CHAT_BASE_URL,
    DEFAULT_CHAT_MODEL,
    DEFAULT_EMBED_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_RERANK_BASE_URL,
    DEFAULT_RERANK_MODEL,
    ChatStream,
    ChatUsage,
    embed_texts,
    read_api_key,
    read_env_value,
    rerank,
)
from app.core.store import Store

# 系统提示词：把「只依据片段回答」和引用格式定死，避免模型自由发挥
SYSTEM_PROMPT = (
    "你是 rag-lite 的文档问答助手。请严格依据「参考片段」回答问题：\n"
    "1. 片段里没有的信息不要编造，直接说明依据不足；\n"
    "2. 引用时用 [来源 n] 标注，n 对应参考片段前的编号；\n"
    "3. 用简体中文回答，简洁直接。"
)

EMPTY_CONTEXT_NOTE = "（本次没有检索到任何参考片段，请直接说明无法依据文档回答这个问题。）"


@dataclass(frozen=True, slots=True)
class Source:
    """一条被选中的参考片段（既用于拼提示词，也用于前端展示来源）。"""

    rank: int
    chunk_id: str
    source_id: str
    name: str
    heading: str
    text: str
    rerank_score: float
    cosine_distance: float
    start: int
    end: int
    images: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalEvent:
    sources: list[Source]


@dataclass(frozen=True, slots=True)
class TokenEvent:
    text: str


@dataclass(frozen=True, slots=True)
class DoneEvent:
    answer: str
    usage: ChatUsage
    seconds: float


Event = RetrievalEvent | TokenEvent | DoneEvent


@dataclass(frozen=True, slots=True)
class ChatConfig:
    """回答用的大模型配置（任意 OpenAI Compatible 端点）。"""

    model: str = DEFAULT_CHAT_MODEL
    base_url: str = DEFAULT_CHAT_BASE_URL
    api_key: str = ""


def load_chat_config(*, env_file: Path | None = None) -> ChatConfig:
    """从环境变量 / .env 读对话配置；CHAT_API_KEY 缺省时复用 OpenRouter 的密钥。"""
    kwargs = {} if env_file is None else {"env_file": env_file}
    key = read_env_value("CHAT_API_KEY", default=None, **kwargs) or read_api_key(**kwargs)
    return ChatConfig(
        model=read_env_value("CHAT_MODEL", default=DEFAULT_CHAT_MODEL, **kwargs) or DEFAULT_CHAT_MODEL,
        base_url=read_env_value("CHAT_BASE_URL", default=DEFAULT_CHAT_BASE_URL, **kwargs) or DEFAULT_CHAT_BASE_URL,
        api_key=key,
    )


def format_context(sources: list[Source]) -> str:
    """把片段拼成提示词里的「参考片段」段落。"""
    if not sources:
        return EMPTY_CONTEXT_NOTE
    blocks = []
    for source in sources:
        location = f"{source.name}"
        if source.heading:
            location += f" · {source.heading}"
        blocks.append(f"[来源 {source.rank}] {location}\n{source.text}")
    return "\n\n".join(blocks)


def build_messages(
    question: str,
    sources: list[Source],
    history: list[dict],
    *,
    system_prompt: str = SYSTEM_PROMPT,
) -> list[dict]:
    """拼出完整 messages：系统提示词 + 全部历史 + 本轮（参考片段 + 问题）。

    history 里是历史各轮的原始问答，按时间顺序原样带上；参考片段只属于本轮。
    """
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend({"role": item["role"], "content": item["content"]} for item in history)
    messages.append(
        {
            "role": "user",
            "content": f"参考片段：\n\n{format_context(sources)}\n\n问题：{question}",
        }
    )
    return messages


class ChatSession:
    """一次会话。每次提问都会重新检索；历史默认全量携带，可用 clear() 清空。"""

    def __init__(
        self,
        store: Store,
        *,
        api_key: str,
        chat: ChatStream | None = None,
        chat_model: str = DEFAULT_CHAT_MODEL,
        chat_base_url: str = DEFAULT_CHAT_BASE_URL,
        chat_api_key: str | None = None,
        embed_model: str = DEFAULT_MODEL,
        embed_base_url: str = DEFAULT_EMBED_BASE_URL,
        embed_api_key: str | None = None,
        rerank_model: str = DEFAULT_RERANK_MODEL,
        rerank_base_url: str = DEFAULT_RERANK_BASE_URL,
        rerank_api_key: str | None = None,
        retrieve_top_k: int = 20,
        context_n: int = 3,
        rerank_doc_chars: int | None = None,
        history: list[dict] | None = None,
        source_ids: Iterable[str] | None = None,
    ) -> None:
        self.store = store
        self.api_key = api_key
        self.chat = chat or ChatStream(
            api_key=chat_api_key or api_key, model=chat_model, base_url=chat_base_url
        )
        # 三个角色的密钥各自可覆盖，没给就沿用 api_key（= 共享的 OpenRouter 密钥）
        self.embed_api_key = embed_api_key or api_key
        self.rerank_api_key = rerank_api_key or api_key
        self.embed_model = embed_model
        self.embed_base_url = embed_base_url
        self.rerank_model = rerank_model
        self.rerank_base_url = rerank_base_url
        self.retrieve_top_k = retrieve_top_k
        self.context_n = context_n
        self.rerank_doc_chars = rerank_doc_chars
        # 历史由调用方传入（HTTP 层从 SQLite 按会话读）；不传就是空会话
        self.history: list[dict] = list(history or [])
        # 检索范围：None = 不限制（全部数据源）；给了列表就只在这个范围里搜（空列表 = 一条都不搜）
        self.source_ids: list[str] | None = None if source_ids is None else list(source_ids)

    # —— 上下文管理 ————————————————————————————————————————————

    def clear(self) -> int:
        """清空对话上下文，返回被清掉的条数（= 轮数 × 2）。"""
        cleared = len(self.history)
        self.history = []
        return cleared

    @property
    def turns(self) -> int:
        """已完成的问答轮数。"""
        return len(self.history) // 2

    # —— 提问 ——————————————————————————————————————————————

    def retrieve(self, question: str) -> list[Source]:
        """检索并重排，返回最终要交给大模型的前 N 条片段。"""
        if self.source_ids is not None and not self.source_ids:
            # 明确给了「空范围」（比如会话绑的分组里还没放数据源）：
            # 这时必须一条都不搜，绝不能退回全库——Store 那边的空列表等于「不限制」。
            return []
        embedded = embed_texts(
            [question],
            api_key=self.embed_api_key,
            model=self.embed_model,
            dim=self.store.dim,
            base_url=self.embed_base_url,
            input_type="search_query",  # 查询侧要和入库侧的 search_document 区分开
        )
        hits = self.store.search(
            embedded.vectors[0], top_k=self.retrieve_top_k, source_ids=self.source_ids
        )
        if not hits:
            return []

        extra = {} if self.rerank_doc_chars is None else {"truncate_chars": self.rerank_doc_chars}
        ranking = rerank(
            question,
            [hit["text"] for hit in hits],
            api_key=self.rerank_api_key,
            model=self.rerank_model,
            base_url=self.rerank_base_url,
            **extra,
        )
        names = {row["source_id"]: row["name"] for row in self.store.list_sources()}
        scores = dict(zip(ranking.order, ranking.scores))

        sources: list[Source] = []
        for rank, hit_index in enumerate(ranking.order[: self.context_n], start=1):
            hit = hits[hit_index]
            sources.append(
                Source(
                    rank=rank,
                    chunk_id=hit["chunk_id"],
                    source_id=hit["source_id"],
                    name=names.get(hit["source_id"], hit["source_id"]),
                    heading=hit["heading"],
                    text=hit["text"],
                    rerank_score=float(scores.get(hit_index, 0.0)),
                    cosine_distance=float(hit["_distance"]),
                    start=int(hit["start"]),
                    end=int(hit["end"]),
                    images=tuple(hit.get("images") or ()),
                )
            )
        return sources

    def ask(self, question: str) -> Iterator[Event]:
        """提一个问题：产出检索结果事件、文本增量事件，最后是结束事件（含完整回答）。"""
        started = time.perf_counter()
        sources = self.retrieve(question)
        yield RetrievalEvent(sources=sources)

        messages = build_messages(question, sources, self.history)
        pieces: list[str] = []
        for piece in self.chat.send(messages):
            pieces.append(piece)
            yield TokenEvent(text=piece)

        answer = "".join(pieces)
        # 历史里只留下干净的问答：参考片段不进历史，避免多轮之后提示词越来越长
        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": answer})
        yield DoneEvent(answer=answer, usage=self.chat.usage, seconds=time.perf_counter() - started)

r"""问答管线的测试：检索 → 重排 → 拼提示词 → 流式回答 → 历史管理。

    cd backend && uv run pytest -q tests/test_query.py

用真实的临时 LanceDB（本地、免费），把「向量化 / 重排 / 对话」三处外部调用换成假的，
所以整套流程可以在不联网、不花钱的情况下逐环节断言。真实的端到端见 tests/chat_report.py。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import query as query_module
from app.core.openrouter import ChatUsage, EmbedResult, EmbedUsage, RerankResult
from app.core.query import (
    ChatSession,
    DoneEvent,
    RetrievalEvent,
    Source,
    TokenEvent,
    build_messages,
    format_context,
)
from app.core.schema import chunk_row, utc_now
from app.core.store import Store

DIM = 4
CHUNKS = [
    ("研究生国家奖学金奖励标准为博士研究生每生每年 3 万元，硕士研究生每生每年 2 万元。", "第三章 奖励标准"),
    ("学校校徽为圆形，以五个相同单元的人字形图案围合成红棉花的虚形。", "第九章 学校标识"),
    ("研究生转专业须向导师和所在学院提出申请，报研究生院审核。", "第九条 转专业的程序"),
]


class FakeChat:
    """假的流式对话：把预设的分段依次吐出来，并记下每次收到的 messages。"""

    def __init__(self, pieces: list[str], *, usage: ChatUsage | None = None) -> None:
        self.pieces = pieces
        self.usage = usage or ChatUsage(prompt_tokens=100, completion_tokens=20, cost_usd=0.00002)
        self.calls: list[list[dict]] = []

    def send(self, messages: list[dict]):
        self.calls.append(messages)
        yield from self.pieces


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """一个有 3 块的临时库，向量都指向同一方向，检索必然命中。"""
    store = Store(tmp_path / "lancedb", dim=DIM)
    store.upsert_source(
        {
            "source_id": "src_test",
            "name": "手册.pdf",
            "path": r"D:\MinerU Outputs\手册.pdf",
            "sha256": "abc",
            "chunk_count": len(CHUNKS),
            "image_count": 0,
            "embed_model": "fake",
            "embed_dim": DIM,
            "added_at": utc_now(),
            "embedded_at": utc_now(),
        }
    )
    store.add_chunks(
        [
            chunk_row(
                chunk_id=f"src_test_{index:05d}",
                source_id="src_test",
                vector=[1.0, 0.0, 0.0, 0.0],
                blocking={
                    "heading": heading,
                    "text": text,
                    "kind": "text",
                    "start": index * 10,
                    "end": index * 10 + 9,
                    "char_count": len(text),
                    "images": [],
                },
                rel_path="full.md",
                embedded_at=utc_now(),
            )
            for index, (text, heading) in enumerate(CHUNKS)
        ]
    )
    return store


@pytest.fixture
def wired(store: Store, monkeypatch: pytest.MonkeyPatch) -> FakeChat:
    """把向量化换成假实现、重排换成「反转顺序」，返回假对话对象。"""
    monkeypatch.setattr(
        query_module,
        "embed_texts",
        lambda texts, **kwargs: EmbedResult(
            vectors=[[1.0, 0.0, 0.0, 0.0] for _ in texts], usage=EmbedUsage(requests=1), dim=DIM
        ),
    )

    def fake_rerank(query, documents, **kwargs) -> RerankResult:
        # 故意反转：如果管线真的用了重排结果，片段顺序就会与余弦顺序相反
        count = len(documents)
        return RerankResult(
            order=list(reversed(range(count))),
            scores=[1.0 - index * 0.1 for index in range(count)],
            requests=1,
            tokens=10,
            cost_usd=0.00001,
            seconds=0.01,
        )

    monkeypatch.setattr(query_module, "rerank", fake_rerank)
    chat = FakeChat(["这", "是", "回答。"])
    monkeypatch.setattr(query_module, "ChatStream", lambda **kwargs: chat)
    return chat


def make_session(store: Store, chat: FakeChat | None = None, **kwargs) -> ChatSession:
    return ChatSession(store, api_key="fake", chat=chat or FakeChat(["答"]), **kwargs)


def collect(session: ChatSession, question: str) -> list:
    return list(session.ask(question))


# ---------------------------------------------------------------- 事件流


def test_ask_emits_sources_then_tokens_then_done(store: Store, wired: FakeChat) -> None:
    session = make_session(store, wired)

    events = collect(session, "国家奖学金多少钱？")

    assert isinstance(events[0], RetrievalEvent)
    assert all(isinstance(event, TokenEvent) for event in events[1:-1])
    assert isinstance(events[-1], DoneEvent)
    assert "".join(event.text for event in events[1:-1]) == events[-1].answer == "这是回答。"
    assert events[-1].usage.prompt_tokens == 100
    assert events[-1].seconds >= 0


def test_rerank_result_decides_source_order(store: Store, wired: FakeChat) -> None:
    session = make_session(store, wired, context_n=2)

    sources = collect(session, "问题")[0].sources

    assert [source.heading for source in sources] == ["第九条 转专业的程序", "第九章 学校标识"]
    assert [source.rank for source in sources] == [1, 2]
    assert sources[0].rerank_score == pytest.approx(1.0)
    assert sources[0].name == "手册.pdf"
    assert sources[0].cosine_distance >= 0


def test_image_only_hits_skip_text_rerank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """纯图片命中没有文本可重排，直接沿用余弦顺序，不能把空串发给 rerank。"""
    store = Store(tmp_path / "image-lancedb", dim=DIM)
    names = ["a.jpg", "b.jpg", "c.jpg"]
    source_dir = tmp_path / "图片集"
    (source_dir / "images").mkdir(parents=True)
    for name in names:
        (source_dir / "images" / name).write_bytes(b"\xff\xd8\xff\xe0" + name.encode())
    store.upsert_source(
        {
            "source_id": "src_images",
            "name": "图片集",
            "path": str(source_dir),
            "sha256": "images",
            "chunk_count": len(names),
            "image_count": len(names),
            "embed_model": "fake",
            "embed_dim": DIM,
            "added_at": utc_now(),
            "embedded_at": utc_now(),
        }
    )
    store.add_chunks(
        [
            chunk_row(
                chunk_id=f"src_images_{index:05d}",
                source_id="src_images",
                vector=[1.0, 0.0, 0.0, 0.0],
                blocking={
                    "heading": f"图片：images/{name}",
                    "text": "",
                    "kind": "image",
                    "start": 0,
                    "end": 0,
                    "char_count": 0,
                    "images": [name],
                },
                rel_path=f"images/{name}",
                embedded_at=utc_now(),
            )
            for index, name in enumerate(names)
        ]
    )
    monkeypatch.setattr(
        query_module,
        "embed_texts",
        lambda texts, **kwargs: EmbedResult(
            vectors=[[1.0, 0.0, 0.0, 0.0] for _ in texts], usage=EmbedUsage(requests=1), dim=DIM
        ),
    )

    def fail_rerank(*args, **kwargs):
        raise AssertionError("纯图片命中不应调用文本重排")

    monkeypatch.setattr(query_module, "rerank", fail_rerank)
    session = ChatSession(store, api_key="fake", chat=FakeChat(["答"]))

    sources = session.retrieve("风景")

    assert len(sources) == 3
    assert all(source.text == "" for source in sources)
    assert {source.images[0] for source in sources} == set(names)
    assert all(source.image_paths and source.image_paths[0].is_file() for source in sources)
    content = build_messages("风景", sources, [])[-1]["content"]
    assert isinstance(content, list)
    assert sum(1 for part in content if part["type"] == "image_url") == 3


def test_context_n_limits_sources(store: Store, wired: FakeChat) -> None:
    session = make_session(store, wired, context_n=1)
    assert len(collect(session, "问题")[0].sources) == 1


# ---------------------------------------------------------------- 提示词与历史


def test_prompt_contains_numbered_snippets_and_question(store: Store, wired: FakeChat) -> None:
    session = make_session(store, wired)

    collect(session, "学校校徽是什么样子？")

    messages = wired.calls[0]
    assert messages[0]["role"] == "system"
    assert "参考片段" in messages[-1]["content"]
    assert "[来源 1]" in messages[-1]["content"] and "[来源 2]" in messages[-1]["content"]
    assert "学校校徽是什么样子？" in messages[-1]["content"]
    assert CHUNKS[2][0] in messages[-1]["content"], "片段正文要真的进提示词"


def test_history_is_fully_sent_on_every_turn(store: Store, wired: FakeChat) -> None:
    """需求：每次提问默认附上全部对话历史。"""
    session = make_session(store, wired)

    collect(session, "第一个问题")
    collect(session, "第二个问题")
    collect(session, "第三个问题")

    third = wired.calls[2]
    contents = [message["content"] for message in third]
    assert third[0]["role"] == "system"
    # 历史上两轮的问答都在，且顺序正确
    assert third[1] == {"role": "user", "content": "第一个问题"}
    assert third[2]["role"] == "assistant" and third[2]["content"] == "这是回答。"
    assert third[3] == {"role": "user", "content": "第二个问题"}
    assert third[4]["role"] == "assistant"
    assert third[-1]["role"] == "user" and "第三个问题" in third[-1]["content"]
    non_system = [message for message in third if message["role"] != "system"]
    assert sum(1 for message in non_system if "参考片段" in message["content"]) == 1, "参考片段只属于本轮"


def test_history_stores_raw_question_not_context(store: Store, wired: FakeChat) -> None:
    session = make_session(store, wired)

    collect(session, "国奖多少钱？")

    assert session.history[0] == {"role": "user", "content": "国奖多少钱？"}
    assert "参考片段" not in session.history[0]["content"]
    assert session.turns == 1


# ---------------------------------------------------------------- 清空上下文


def test_clear_drops_all_history(store: Store, wired: FakeChat) -> None:
    session = make_session(store, wired)
    collect(session, "第一个问题")
    collect(session, "第二个问题")
    assert session.turns == 2

    cleared = session.clear()

    assert cleared == 4, "两轮 = 4 条消息"
    assert session.history == []
    assert session.turns == 0

    collect(session, "清空之后的问题")
    after_clear = wired.calls[-1]
    assert len(after_clear) == 2, "只剩 system + 本轮问题"
    assert "第一个问题" not in after_clear[-1]["content"]
    assert "清空之后的问题" in after_clear[-1]["content"]


def test_clear_on_empty_session_is_safe(store: Store, wired: FakeChat) -> None:
    assert make_session(store, wired).clear() == 0


# ---------------------------------------------------------------- 边界


def test_empty_store_asks_model_to_admit_unknown(tmp_path: Path, wired: FakeChat) -> None:
    empty = Store(tmp_path / "empty", dim=DIM)
    session = make_session(empty, wired)

    events = collect(session, "库里什么都没有，怎么办？")

    assert events[0].sources == []
    assert "没有检索到任何参考片段" in wired.calls[0][-1]["content"]


def test_ask_stops_early_without_breaking_session(store: Store, wired: FakeChat) -> None:
    """调用方中途取消（只取前两段）时，历史仍然完整、可以继续问。"""
    session = make_session(store, wired)
    stream = session.ask("问题")

    next(stream)  # RetrievalEvent
    next(stream)  # 第一段文本
    stream.close()

    assert session.history == [], "回答没结束就不该把半截内容写进历史"
    collect(session, "再问一次")
    assert session.turns == 1


# ---------------------------------------------------------------- 纯函数


def test_format_context_marks_missing_sources() -> None:
    assert "没有检索到" in format_context([])


def test_build_messages_includes_source_images(tmp_path: Path) -> None:
    image = tmp_path / "images" / "a.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    source = Source(
        rank=1,
        chunk_id="c1",
        source_id="s1",
        name="图片集",
        heading="图片：images/a.jpg",
        text="",
        rerank_score=1.0,
        cosine_distance=0.0,
        start=0,
        end=0,
        images=("a.jpg",),
        image_paths=(image,),
    )

    messages = build_messages("图里是什么？", [source], [])

    content = messages[-1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text" and "图里是什么" in content[0]["text"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_build_messages_layout() -> None:
    source = Source(
        rank=1,
        chunk_id="c1",
        source_id="s1",
        name="手册.pdf",
        heading="第一章",
        text="正文内容",
        rerank_score=0.9,
        cosine_distance=0.1,
        start=0,
        end=4,
    )

    messages = build_messages("问题", [source], [{"role": "user", "content": "旧问题"}])

    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "旧问题"}
    assert messages[2]["content"].startswith("参考片段：")
    assert "[来源 1] 手册.pdf · 第一章" in messages[2]["content"]

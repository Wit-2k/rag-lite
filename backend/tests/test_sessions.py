"""会话管理与聊天记录落库（SQLite）的测试。

    cd backend && uv run pytest -q tests/test_sessions.py

分两层：
- `SessionStore` 直接测（不碰 HTTP）：这是存储语义的边界（删除连带、历史隔离、自动命名）；
- 接口层用 TestClient 打真实请求栈（外部模型调用全部换假），重点验
  **两个会话的历史互不串**、以及失败时不留「没有回答的问答」。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config import Settings
from app.core import query as query_module
from app.core.openrouter import ChatUsage, EmbedResult, EmbedUsage, RerankResult
from app.core.schema import chunk_row, utc_now
from app.core.sessions import DEFAULT_TITLE, SessionStore, auto_title
from app.core.store import Store

DIM = 4


# ---------------------------------------------------------------- 存储层


@pytest.fixture
def sessions(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions.db")


def test_create_list_and_default_title(sessions: SessionStore) -> None:
    created = sessions.create()

    assert created["session_id"].startswith("sess_")
    assert created["title"] == DEFAULT_TITLE
    rows = sessions.list()
    assert [row["session_id"] for row in rows] == [created["session_id"]]
    assert rows[0]["message_count"] == 0


def test_append_history_and_messages(sessions: SessionStore) -> None:
    session_id = sessions.create()["session_id"]
    sessions.append(session_id, "user", "问题一")
    sessions.append(session_id, "assistant", "回答一", sources=[{"rank": 1, "name": "甲.pdf"}])

    assert sessions.history(session_id) == [
        {"role": "user", "content": "问题一"},
        {"role": "assistant", "content": "回答一"},
    ]
    messages = sessions.messages(session_id)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["sources"][0]["name"] == "甲.pdf"
    assert sessions.list()[0]["message_count"] == 2


def test_history_is_isolated_between_sessions(sessions: SessionStore) -> None:
    """这是整个功能的关键：两个会话的历史绝不能互相串。"""
    a = sessions.create()["session_id"]
    b = sessions.create()["session_id"]
    sessions.append(a, "user", "A 的问题")
    sessions.append(a, "assistant", "A 的回答")

    assert sessions.history(b) == []
    assert sessions.messages(b) == []
    assert len(sessions.history(a)) == 2


def test_delete_removes_messages_and_spares_others(sessions: SessionStore) -> None:
    a = sessions.create()["session_id"]
    b = sessions.create()["session_id"]
    sessions.append(a, "user", "A 的问题")
    sessions.append(b, "user", "B 的问题")

    assert sessions.delete(a) is True
    assert sessions.get(a) is None
    assert sessions.messages(a) == []          # 消息一起删干净了
    assert sessions.history(b) == [{"role": "user", "content": "B 的问题"}]  # 别的会话不受影响
    assert sessions.delete(a) is False          # 再删一次：没有这个会话了


def test_clear_keeps_session_but_drops_messages(sessions: SessionStore) -> None:
    a = sessions.create()["session_id"]
    b = sessions.create()["session_id"]
    sessions.append(a, "user", "A 的问题")
    sessions.append(b, "user", "B 的问题")

    assert sessions.clear(a) == 1
    assert sessions.messages(a) == []
    assert sessions.get(a) is not None          # 会话本身还在
    assert len(sessions.messages(b)) == 1       # 不碰别的会话


def test_rename(sessions: SessionStore) -> None:
    session_id = sessions.create()["session_id"]

    assert sessions.rename(session_id, "我的第一个会话") is True
    assert sessions.get(session_id)["title"] == "我的第一个会话"
    assert sessions.rename("sess_nope", "x") is False
    assert sessions.rename(session_id, "   ") is True   # 空白标题回落到默认名
    assert sessions.get(session_id)["title"] == DEFAULT_TITLE


def test_auto_title_only_on_first_question(sessions: SessionStore) -> None:
    session_id = sessions.create()["session_id"]

    assert sessions.title_from_question(session_id, "研究生转专业需要什么条件？") == "研究生转专业需要什么条件？"
    sessions.append(session_id, "user", "研究生转专业需要什么条件？")
    # 第二问不再改标题
    assert sessions.title_from_question(session_id, "那奖学金呢？") is None
    assert sessions.get(session_id)["title"] == "研究生转专业需要什么条件？"


def test_auto_title_respects_manual_rename(sessions: SessionStore) -> None:
    session_id = sessions.create()["session_id"]
    sessions.rename(session_id, "我改过的名字")

    assert sessions.title_from_question(session_id, "随便问点什么") is None
    assert sessions.get(session_id)["title"] == "我改过的名字"


def test_auto_title_truncates_and_flattens() -> None:
    long_question = "第一行\n第二行 " + "很长很长" * 10
    title = auto_title(long_question)

    assert "\n" not in title
    assert len(title) == 25 and title.endswith("…")   # 24 个字 + 省略号


def test_drop_message_removes_only_that_row(sessions: SessionStore) -> None:
    session_id = sessions.create()["session_id"]
    keep = sessions.append(session_id, "user", "要保留的")
    drop = sessions.append(session_id, "user", "要撤掉的")

    sessions.drop_message(drop)

    assert sessions.history(session_id) == [{"role": "user", "content": "要保留的"}]
    assert keep != drop


def test_list_orders_by_recent_update(sessions: SessionStore) -> None:
    first = sessions.create()["session_id"]
    second = sessions.create()["session_id"]

    sessions.append(first, "user", "先建的会话有新消息")   # 顶一下 updated_at

    assert [row["session_id"] for row in sessions.list()][0] == first
    assert second in [row["session_id"] for row in sessions.list()]


# ---------------------------------------------------------------- 接口层


class FakeChat:
    """假对话：记录每次收到的 messages（用来验证历史有没有串），固定吐两段文本。"""

    def __init__(self) -> None:
        self.usage = ChatUsage(prompt_tokens=10, completion_tokens=2, cost_usd=0.00001)
        self.calls: list[list[dict]] = []

    def send(self, messages: list[dict]):
        self.calls.append(messages)
        yield "答"
        yield "案"


class BoomChat(FakeChat):
    """一个字都不吐就报错：用来验证「问题会被撤掉」。"""

    def send(self, messages: list[dict]):
        self.calls.append(messages)
        raise RuntimeError("上游挂了")
        yield ""  # pragma: no cover - 让它是生成器


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        root=tmp_path,
        data_dir=tmp_path,
        db_uri=str(tmp_path / "lancedb"),
        api_key="fake-key",
        embed_dim=DIM,
    )


@pytest.fixture
def store(settings: Settings) -> Store:
    store = Store(settings.db_uri, dim=DIM)
    store.add_chunks([
        chunk_row(
            chunk_id="src_a_00000",
            source_id="src_a",
            vector=[1.0, 0.0, 0.0, 0.0],
            blocking={"heading": "第三章", "text": "研究生转专业需要满足条件。", "kind": "text",
                      "start": 0, "end": 10, "char_count": 10, "images": []},
            rel_path="full.md",
            embedded_at=utc_now(),
        )
    ])
    return store


@pytest.fixture
def chat(monkeypatch: pytest.MonkeyPatch) -> FakeChat:
    """外部调用全部换假：向量化、重排、对话。"""
    monkeypatch.setattr(query_module, "embed_texts",
                        lambda texts, **kw: EmbedResult(vectors=[[1.0, 0.0, 0.0, 0.0] for _ in texts],
                                                        usage=EmbedUsage(requests=1), dim=DIM))
    monkeypatch.setattr(query_module, "rerank",
                        lambda query, documents, **kw: RerankResult(
                            order=list(range(len(documents))),
                            scores=[0.9 - 0.1 * i for i in range(len(documents))],
                            requests=1, tokens=10, cost_usd=0.00001, seconds=0.01))
    fake = FakeChat()
    monkeypatch.setattr(query_module, "ChatStream", lambda **kwargs: fake)
    return fake


@pytest.fixture
def client(settings: Settings, store: Store, chat: FakeChat) -> TestClient:
    return TestClient(create_app(settings=settings, store=store))


def sse_frames(response) -> list[dict]:
    import json
    return [json.loads(line[len("data: "):]) for line in response.text.splitlines() if line.startswith("data: ")]


def test_session_crud_via_api(client: TestClient) -> None:
    created = client.post("/api/sessions", json={})
    assert created.status_code == 201
    session_id = created.json()["session_id"]

    listed = client.get("/api/sessions").json()
    assert [row["session_id"] for row in listed] == [session_id]

    renamed = client.put(f"/api/sessions/{session_id}", json={"title": "改名了"})
    assert renamed.status_code == 200 and renamed.json()["title"] == "改名了"

    assert client.delete(f"/api/sessions/{session_id}").status_code == 204
    assert client.get(f"/api/sessions/{session_id}/messages").status_code == 404
    assert client.delete(f"/api/sessions/{session_id}").status_code == 404


def test_chat_persists_question_and_answer(client: TestClient) -> None:
    session_id = client.post("/api/sessions", json={}).json()["session_id"]

    frames = sse_frames(client.post("/api/chat", json={"question": "转专业条件？", "session_id": session_id}))
    assert [f["type"] for f in frames][:2] == ["sources", "token"]
    assert frames[-1]["type"] == "done"

    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assert [(m["role"], m["content"]) for m in messages] == [("user", "转专业条件？"), ("assistant", "答案")]
    assert messages[1]["sources"][0]["name"] == "src_a"      # 来源也存下来了，切换会话能还原来源行
    # 首问自动命名
    assert client.get("/api/sessions").json()[0]["title"] == "转专业条件？"


def test_chat_requires_a_real_session(client: TestClient) -> None:
    assert client.post("/api/chat", json={"question": "问", "session_id": "sess_nope"}).status_code == 404
    assert client.post("/api/chat", json={"question": "问"}).status_code == 422   # 必须带 session_id


def test_two_sessions_do_not_share_history(client: TestClient, chat: FakeChat) -> None:
    """A 会话问过之后，B 会话提问时**不能**把 A 的历史带进提示词。"""
    a = client.post("/api/sessions", json={}).json()["session_id"]
    b = client.post("/api/sessions", json={}).json()["session_id"]

    client.post("/api/chat", json={"question": "A 的第一问", "session_id": a}).read()
    client.post("/api/chat", json={"question": "B 的第一问", "session_id": b}).read()

    b_prompt = chat.calls[-1]
    text = "\n".join(item["content"] for item in b_prompt)
    assert "A 的第一问" not in text and "A 的回答" not in text
    assert "B 的第一问" in text

    # A 的第二问要带上 A 自己的历史
    client.post("/api/chat", json={"question": "A 的第二问", "session_id": a}).read()
    a_prompt = "\n".join(item["content"] for item in chat.calls[-1])
    assert "A 的第一问" in a_prompt and "答案" in a_prompt


def test_chat_rolls_back_question_when_nothing_produced(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """一个字都没产出（上游报错）时，不留一条没有回答的问答。"""
    session_id = client.post("/api/sessions", json={}).json()["session_id"]
    monkeypatch.setattr(query_module, "ChatStream", lambda **kwargs: BoomChat())

    frames = sse_frames(client.post("/api/chat", json={"question": "会失败的问题", "session_id": session_id}))
    assert frames[-1]["type"] == "error"

    assert client.get(f"/api/sessions/{session_id}/messages").json() == []


def test_clear_only_clears_that_session(client: TestClient) -> None:
    a = client.post("/api/sessions", json={}).json()["session_id"]
    b = client.post("/api/sessions", json={}).json()["session_id"]
    client.post("/api/chat", json={"question": "A 问", "session_id": a}).read()
    client.post("/api/chat", json={"question": "B 问", "session_id": b}).read()

    cleared = client.post("/api/chat/clear", json={"session_id": a})
    assert cleared.status_code == 200 and cleared.json() == {"cleared": 2, "turns": 1}

    assert client.get(f"/api/sessions/{a}/messages").json() == []
    assert len(client.get(f"/api/sessions/{b}/messages").json()) == 2
    assert client.post("/api/chat/clear", json={"session_id": "sess_nope"}).status_code == 404


def test_sessions_live_in_data_dir(settings: Settings, store: Store, chat: FakeChat) -> None:
    """库文件要在数据目录里——便携版整包搬走时会话跟着走。"""
    other = TestClient(create_app(settings=settings, store=store))
    other.post("/api/sessions", json={})

    assert (Path(settings.data_dir) / "sessions.db").is_file()

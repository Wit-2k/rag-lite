"""数据源分组（多对多）与会话「检索范围」的测试。

    cd backend && uv run pytest -q tests/test_groups.py

分两层：
- `GroupStore` / `SessionStore` 直接测：分组的增删改、多对多成员、删除前查引用，
  以及**老库原地升级**（v0.1.6 建的 sessions.db 没有分组也不该坏）；
- 接口层用 TestClient 打真实请求栈（向量化/重排/对话三处换假），重点验
  **会话绑了分组后只在那组里检索**、以及「还有会话在用时拒绝删分组」。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.server import create_app
from app.config import Settings
from app.core import query as query_module
from app.core.groups import GroupStore, GroupResult
from app.core.openrouter import ChatUsage, EmbedResult, EmbedUsage, RerankResult
from app.core.schema import chunk_row, utc_now
from app.core.sessions import SessionStore
from app.core.store import Store

DIM = 4


# ---------------------------------------------------------------- 存储层


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "sessions.db"


@pytest.fixture
def groups(db_path: Path) -> GroupStore:
    return GroupStore(db_path)


def test_create_list_rename_and_counts(groups: GroupStore) -> None:
    created = groups.create("民法")
    assert created.ok and created.group["group_id"].startswith("grp_")
    assert created.group["source_count"] == 0

    other = groups.create("  刑法  ")  # 首尾空白会被去掉
    assert other.group["name"] == "刑法"
    assert [row["name"] for row in groups.list()] == sorted(["刑法", "民法"])

    assert groups.rename(created.group["group_id"], "民法典").ok
    assert groups.get(created.group["group_id"])["name"] == "民法典"


def test_bad_and_duplicate_names_are_refused(groups: GroupStore) -> None:
    for bad in ("   ", "未分组", "全部数据源", "长" * 41):
        assert groups.create(bad).reason == "reserved", bad
    assert groups.list() == []

    first = groups.create("甲")
    assert groups.create(" 甲 ").reason == "duplicate"  # 判重也去掉首尾空白
    assert groups.rename(first.group["group_id"], "甲").ok  # 改成自己原来的名字没问题
    second = groups.create("乙")
    assert groups.rename(second.group["group_id"], "甲").reason == "duplicate"
    assert groups.rename("grp_nope", "丙").reason == "missing"
    assert len(groups.list()) == 2


def test_a_source_can_belong_to_several_groups(groups: GroupStore) -> None:
    a = groups.create("A").group["group_id"]
    b = groups.create("B").group["group_id"]

    first = groups.set_source_groups("src_1", [a, b])
    assert first == GroupResult(ok=True, group_ids=(a, b))
    assert groups.set_source_groups("src_1", [a, b]).group_ids == (a, b)  # 重复提交结果一样
    assert groups.set_source_groups("src_2", [b]).ok

    assert groups.sources_of(a) == ["src_1"]
    assert sorted(groups.sources_of(b)) == ["src_1", "src_2"]
    assert groups.groups_of_sources(["src_1", "src_2"]).keys() == {"src_1", "src_2"}
    assert set(groups.groups_of_sources(["src_1"])["src_1"]) == {a, b}
    assert groups.get(a)["source_count"] == 1 and groups.get(b)["source_count"] == 2
    assert groups.groups_of_sources([]) == {}

    # 整体替换：给空列表 = 从所有分组里拿出来（回到「未分组」）
    assert groups.set_source_groups("src_1", []).ok
    assert groups.sources_of(a) == []
    assert groups.groups_of_sources(["src_1"]) == {}
    assert groups.get(b)["source_count"] == 1


def test_unknown_group_is_refused_and_nothing_is_written(groups: GroupStore) -> None:
    a = groups.create("A").group["group_id"]
    groups.set_source_groups("src_1", [a])

    result = groups.set_source_groups("src_1", [a, "grp_nope"])

    assert result.ok is False and result.reason == "unknown" and result.unknown == ("grp_nope",)
    assert groups.sources_of(a) == ["src_1"]  # 半个请求都不生效，不会只写进去一半


def test_delete_refuses_while_a_session_uses_it(db_path: Path) -> None:
    """删分组前必须确认没人拿它当检索范围（否则那些会话的范围会悄悄变大）。"""
    groups, sessions = GroupStore(db_path), SessionStore(db_path)
    group_id = groups.create("A").group["group_id"]
    session = sessions.create("问过法律的会话", group_id=group_id)

    refused = groups.delete(group_id)

    assert refused.reason == "in_use"
    assert [row["title"] for row in refused.blocking] == ["问过法律的会话"]
    assert groups.get(group_id) is not None  # 没删成

    sessions.set_group(session["session_id"], None)  # 改回「全部数据源」
    assert groups.delete(group_id).ok
    assert groups.get(group_id) is None and groups.list() == []


def test_delete_only_touches_that_group(groups: GroupStore) -> None:
    a = groups.create("A").group["group_id"]
    b = groups.create("B").group["group_id"]
    groups.set_source_groups("src_1", [a, b])

    assert groups.delete(a).ok

    assert groups.sources_of(a) == []
    assert groups.sources_of(b) == ["src_1"]  # 别的分组不受影响
    assert groups.get(b)["source_count"] == 1


def test_forget_source_clears_membership(groups: GroupStore) -> None:
    a = groups.create("A").group["group_id"]
    groups.set_source_groups("src_1", [a])
    groups.set_source_groups("src_2", [a])

    assert groups.forget_source("src_1") == 1

    assert groups.sources_of(a) == ["src_2"]
    assert groups.get(a)["source_count"] == 1


def test_old_database_is_upgraded_in_place(tmp_path: Path) -> None:
    """拿 v0.1.6 形状的库（sessions 表没有 group_id、也没有分组两张表）打开：不能坏。"""
    path = tmp_path / "sessions.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                title      TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                seq        INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                sources    TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            INSERT INTO sessions VALUES ('sess_old', '老会话', '2026-10-01T00:00:00+00:00',
                                        '2026-10-01T00:00:00+00:00', 1);
            INSERT INTO messages (session_id, role, content, created_at)
            VALUES ('sess_old', 'user', '老问题', '2026-10-01T00:00:00+00:00');
            """
        )

    sessions, groups = SessionStore(path), GroupStore(path)

    old = sessions.get("sess_old")
    assert old["title"] == "老会话" and old["group_id"] is None  # 老会话 = 全部数据源
    assert sessions.history("sess_old") == [{"role": "user", "content": "老问题"}]

    created = groups.create("新建的分组")
    assert created.ok
    assert sessions.create("新会话", group_id=created.group["group_id"])["group_id"] == created.group["group_id"]
    assert [row["group_id"] for row in sessions.list()] == [created.group["group_id"], None]


# ---------------------------------------------------------------- 接口层

CHUNKS = {
    "src_a": ("甲.pdf", "甲章", "甲文：研究生国家奖学金博士每生每年 3 万元。"),
    "src_b": ("乙.pdf", "乙章", "乙文：校徽以人字形图案围合成红棉花的虚形。"),
}


class FakeChat:
    """假对话：固定吐两段文本（这里只关心检索范围，不关心回答内容）。"""

    def __init__(self) -> None:
        self.usage = ChatUsage(prompt_tokens=1, completion_tokens=1, cost_usd=0.0)

    def send(self, messages: list[dict]):
        yield "答"
        yield "案"


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
    """两个数据源，向量刻意正交——加上按组过滤才能证明「只搜了本组」。"""
    store = Store(settings.db_uri, dim=DIM)
    for index, (source_id, (name, heading, text)) in enumerate(CHUNKS.items()):
        store.upsert_source(
            {
                "source_id": source_id,
                "name": name,
                "path": str(settings.root / name),
                "sha256": source_id,
                "chunk_count": 1,
                "image_count": 0,
                "embed_model": settings.embed_model,
                "embed_dim": settings.embed_dim,
                "added_at": utc_now(),
                "embedded_at": utc_now(),
            }
        )
        store.add_chunks(
            [
                chunk_row(
                    chunk_id=f"{source_id}_00000",
                    source_id=source_id,
                    vector=[1.0, 0.0, 0.0, 0.0] if index == 0 else [0.0, 1.0, 0.0, 0.0],
                    blocking={
                        "heading": heading,
                        "text": text,
                        "kind": "text",
                        "start": 0,
                        "end": len(text),
                        "char_count": len(text),
                        "images": [],
                    },
                    rel_path="full.md",
                    embedded_at=utc_now(),
                )
            ]
        )
    return store


@pytest.fixture
def chat(monkeypatch: pytest.MonkeyPatch) -> FakeChat:
    monkeypatch.setattr(
        query_module,
        "embed_texts",
        lambda texts, **kwargs: EmbedResult(
            vectors=[[1.0, 0.0, 0.0, 0.0] for _ in texts], usage=EmbedUsage(requests=1), dim=DIM
        ),
    )
    monkeypatch.setattr(
        query_module,
        "rerank",
        lambda query, documents, **kwargs: RerankResult(
            order=list(range(len(documents))),
            scores=[0.9 - 0.1 * index for index in range(len(documents))],
            requests=1,
            tokens=10,
            cost_usd=0.0,
            seconds=0.01,
        ),
    )
    fake = FakeChat()
    monkeypatch.setattr(query_module, "ChatStream", lambda **kwargs: fake)
    return fake


@pytest.fixture
def client(settings: Settings, store: Store, chat: FakeChat) -> TestClient:
    return TestClient(create_app(settings=settings, store=store))


def new_group(client: TestClient, name: str) -> str:
    response = client.post("/api/groups", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["group_id"]


def ask(client: TestClient, session_id: str, question: str = "问一句") -> list[dict]:
    """打完一次提问，返回 SSE 帧。"""
    with client.stream("POST", "/api/chat", json={"question": question, "session_id": session_id}) as response:
        assert response.status_code == 200, response.text
        response.read()
        text = response.text
    return [json.loads(line[len("data: ") :]) for line in text.splitlines() if line.startswith("data: ")]


def source_names(frames: list[dict]) -> set[str]:
    assert frames[0]["type"] == "sources"
    return {item["name"] for item in frames[0]["items"]}


def test_group_crud_over_http(client: TestClient) -> None:
    assert client.get("/api/groups").json() == []

    created = client.post("/api/groups", json={"name": "民法"})
    assert created.status_code == 201 and created.json()["source_count"] == 0
    group_id = created.json()["group_id"]

    assert client.post("/api/groups", json={"name": "民法"}).status_code == 409
    assert client.post("/api/groups", json={"name": "未分组"}).status_code == 409
    assert client.post("/api/groups", json={"name": "   "}).status_code == 409
    assert client.post("/api/groups", json={"name": ""}).status_code == 422  # 请求模型就挡下了
    assert client.post("/api/groups", json={"name": "长" * 41}).status_code == 422

    renamed = client.put(f"/api/groups/{group_id}", json={"name": "民法典"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "民法典"
    assert client.put("/api/groups/grp_nope", json={"name": "甲"}).status_code == 404

    assert client.delete("/api/groups/grp_nope").status_code == 404
    assert client.delete(f"/api/groups/{group_id}").status_code == 204
    assert client.get("/api/groups").json() == []


def test_source_membership_over_http(client: TestClient) -> None:
    a, b = new_group(client, "A"), new_group(client, "B")

    updated = client.put("/api/sources/src_a/groups", json={"group_ids": [a, b]})
    assert updated.status_code == 200 and sorted(updated.json()["group_ids"]) == sorted([a, b])

    rows = {row["source_id"]: row for row in client.get("/api/sources").json()}
    assert sorted(rows["src_a"]["group_ids"]) == sorted([a, b])
    assert rows["src_b"]["group_ids"] == []
    assert {row["name"]: row["source_count"] for row in client.get("/api/groups").json()} == {"A": 1, "B": 1}

    assert client.put("/api/sources/src_a/groups", json={"group_ids": ["grp_nope"]}).status_code == 404
    assert client.put("/api/sources/src_nope/groups", json={"group_ids": []}).status_code == 404
    assert client.put("/api/sources/src_a/groups", json={"group_ids": []}).json()["group_ids"] == []
    assert {row["source_count"] for row in client.get("/api/groups").json()} == {0}


def test_deleting_a_source_clears_its_membership(client: TestClient) -> None:
    group_id = new_group(client, "A")
    client.put("/api/sources/src_a/groups", json={"group_ids": [group_id]})

    assert client.delete("/api/sources/src_a").status_code == 204

    assert client.get("/api/groups").json()[0]["source_count"] == 0
    assert client.delete(f"/api/groups/{group_id}").status_code == 204  # 没人用了，删得掉


def test_session_binding_and_delete_guard(client: TestClient) -> None:
    group_id = new_group(client, "民法")
    session_id = client.post("/api/sessions", json={"title": "问法律", "group_id": group_id}).json()["session_id"]
    assert client.get("/api/sessions").json()[0]["group_id"] == group_id

    blocked = client.delete(f"/api/groups/{group_id}")
    assert blocked.status_code == 409
    assert "问法律" in blocked.json()["detail"]  # 告诉用户是哪几个会话在用

    # 换成「全部数据源」后就能删了；绑定关系只影响之后的问题，不顶列表顺序
    assert client.put(f"/api/sessions/{session_id}/group", json={"group_id": None}).json()["group_id"] is None
    assert client.delete(f"/api/groups/{group_id}").status_code == 204

    assert client.post("/api/sessions", json={"group_id": "grp_nope"}).status_code == 404
    assert client.put("/api/sessions/sess_nope/group", json={"group_id": None}).status_code == 404
    assert client.put(f"/api/sessions/{session_id}/group", json={"group_id": "grp_nope"}).status_code == 404


def test_chat_only_searches_the_bound_group(client: TestClient) -> None:
    group_a = new_group(client, "只看甲")
    client.put("/api/sources/src_a/groups", json={"group_ids": [group_a]})

    scoped = client.post("/api/sessions", json={"group_id": group_a}).json()["session_id"]
    assert source_names(ask(client, scoped)) == {"甲.pdf"}

    # 不绑分组 = 全部数据源：两个源都能被检索到
    everywhere = client.post("/api/sessions", json={}).json()["session_id"]
    assert source_names(ask(client, everywhere)) == {"甲.pdf", "乙.pdf"}


def test_chat_in_an_empty_group_retrieves_nothing(client: TestClient) -> None:
    """空分组必须一条都不搜——绝不能因为「范围是空的」就退回全库。"""
    empty = new_group(client, "空组")
    session_id = client.post("/api/sessions", json={"group_id": empty}).json()["session_id"]

    frames = ask(client, session_id)

    assert frames[0]["items"] == []
    assert frames[-1]["type"] == "done"  # 还是照常问模型（它会说依据不足）


def test_changing_the_scope_only_affects_later_questions(client: TestClient) -> None:
    group_b = new_group(client, "只看乙")
    client.put("/api/sources/src_b/groups", json={"group_ids": [group_b]})
    session_id = client.post("/api/sessions", json={}).json()["session_id"]

    first = ask(client, session_id, "先问全部")
    assert source_names(first) == {"甲.pdf", "乙.pdf"}

    client.put(f"/api/sessions/{session_id}/group", json={"group_id": group_b})
    second = ask(client, session_id, "再问只看乙")

    assert source_names(second) == {"乙.pdf"}
    # 历史照旧带着（换范围不是清空）
    messages = client.get(f"/api/sessions/{session_id}/messages").json()
    assert [row["role"] for row in messages] == ["user", "assistant", "user", "assistant"]


def test_session_survives_group_deletion_from_a_stale_db(
    settings: Settings, store: Store, chat: FakeChat
) -> None:
    """分组没了（比如手工改过库）→ 会话退回「全部数据源」，不能报错、也不能查不到东西。"""
    sessions = SessionStore(Path(settings.data_dir) / "sessions.db")
    session_id = sessions.create("老会话", group_id="grp_手动删掉的")["session_id"]
    client = TestClient(create_app(settings=settings, store=store))

    frames = ask(client, session_id)

    assert source_names(frames) == {"甲.pdf", "乙.pdf"}

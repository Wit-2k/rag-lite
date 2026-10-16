r"""HTTP 接口的测试。

    cd backend && uv run pytest -q tests/test_api.py

用 FastAPI 的 TestClient 打真实的请求/响应栈（含 SSE 帧解析），
向量库是临时目录里的真库，只有「向量化 / 重排 / 对话」三处外部调用被换成假的。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import routes as routes_module
from app.api.server import create_app
from app.config import Settings
from app.core import query as query_module
from app.core.openrouter import ChatUsage, EmbedResult, EmbedUsage, RerankResult
from app.core.schema import chunk_row, source_id_for, utc_now
from app.core.store import Store

DIM = 4
CHUNKS = [
    ("研究生国家奖学金奖励标准为博士研究生每生每年 3 万元。", "第三章 奖励标准"),
    ("学校校徽为圆形，以五个相同单元的人字形图案围合成红棉花的虚形。", "第九章 学校标识"),
]


class FakeChat:
    """假对话：固定吐两段文本，并记录收到的 messages（用来验证历史是否带上）。"""

    def __init__(self) -> None:
        self.usage = ChatUsage(prompt_tokens=42, completion_tokens=7, cost_usd=0.00001)
        self.calls: list[list[dict]] = []

    def send(self, messages: list[dict]):
        self.calls.append(messages)
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
    store = Store(settings.db_uri, dim=DIM)
    store.upsert_source(
        {
            "source_id": "src_a",
            "name": "手册.pdf",
            "path": str(settings.root / "手册.pdf"),
            "sha256": "abc",
            "chunk_count": len(CHUNKS),
            "image_count": 1,
            "embed_model": settings.embed_model,  # 与 Settings 保持一致，否则会被判成「需重新导入」
            "embed_dim": settings.embed_dim,
            "added_at": utc_now(),
            "embedded_at": utc_now(),
        }
    )
    store.add_chunks(
        [
            chunk_row(
                chunk_id=f"src_a_{index:05d}",
                source_id="src_a",
                vector=[1.0, 0.0, 0.0, 0.0],
                blocking={
                    "heading": heading,
                    "text": text,
                    "kind": "text",
                    "start": index * 10,
                    "end": index * 10 + 9,
                    "char_count": len(text),
                    "images": ["pic0.jpg"] if index == 0 else [],
                },
                rel_path="full.md",
                embedded_at=utc_now(),
            )
            for index, (text, heading) in enumerate(CHUNKS)
        ]
    )
    return store


@pytest.fixture
def chat(monkeypatch: pytest.MonkeyPatch) -> FakeChat:
    """外部调用全部换假：向量化、重排、对话。"""
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
            cost_usd=0.00001,
            seconds=0.01,
        ),
    )
    fake = FakeChat()
    monkeypatch.setattr(query_module, "ChatStream", lambda **kwargs: fake)
    return fake


@pytest.fixture
def client(settings: Settings, store: Store, chat: FakeChat) -> TestClient:
    return TestClient(create_app(settings=settings, store=store))


def sse_frames(response) -> list[dict]:
    """把 SSE 响应体解析成一个个 JSON 帧。"""
    frames = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            frames.append(json.loads(line[len("data: ") :]))
    return frames


# ---------------------------------------------------------------- 就绪与数据源


def test_health_reports_store_and_config(client: TestClient) -> None:
    body = client.get("/api/health").json()

    assert body["status"] == "ok"
    assert body["chunks"] == len(CHUNKS)
    assert body["sources"] == 1
    assert body["configured"] is True
    assert body["missing_keys"] == []
    assert body["embed_dim"] == DIM
    assert body["chat_model"]
    # 三个角色的接入点与模型都要能从 health 看到，方便确认配置生效
    assert body["embed_base_url"] and body["rerank_base_url"] and body["chat_base_url"]
    assert body["rerank_model"]
    assert body["sources_stale"] == 0


def test_health_without_key(tmp_path: Path, store: Store) -> None:
    settings = Settings(root=tmp_path, data_dir=tmp_path, db_uri=store.db_uri, api_key="", embed_dim=DIM)
    other = TestClient(create_app(settings=settings, store=store))

    assert other.get("/api/health").json()["configured"] is False


def test_list_sources_marks_missing_directory(client: TestClient, settings: Settings) -> None:
    body = client.get("/api/sources").json()

    assert len(body) == 1
    assert body[0]["name"] == "手册.pdf"
    assert body[0]["chunk_count"] == len(CHUNKS)
    assert body[0]["missing"] is True, "path 下面没有 full.md，应当标为 missing"

    (settings.root / "手册.pdf").mkdir(parents=True, exist_ok=True)
    (settings.root / "手册.pdf" / "full.md").write_text("# 标题\n\n正文", encoding="utf-8")
    assert client.get("/api/sources").json()[0]["missing"] is False


def test_add_source_rejects_directory_without_full_md(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/api/sources", json={"path": str(tmp_path / "不存在")})

    assert response.status_code == 400
    assert "full.md" in response.json()["detail"]


def test_add_source_runs_ingest_job(client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """添加数据源是异步任务：立刻拿到 job_id，轮询能看到进度与结果。"""
    source = settings.root / "新论文.pdf"
    source.mkdir(parents=True, exist_ok=True)
    (source / "full.md").write_text("# 甲\n\n" + "这是一段足够长的正文。" * 20, encoding="utf-8")

    def fake_ingest(path, store, *, api_key, model, dim, on_progress=None, **kwargs):
        from app.core.ingest import IngestResult

        if on_progress:
            on_progress(1, 1, "向量化")
        return IngestResult(
            source_id="src_new",
            name=Path(path).name,
            path=str(path),
            chunk_count=5,
            image_count=0,
            skipped=False,
            sha256="deadbeef",
        )

    monkeypatch.setattr(routes_module, "ingest", fake_ingest)

    accepted = client.post("/api/sources", json={"path": str(source)})
    assert accepted.status_code == 202
    job_id = accepted.json()["job_id"]

    for _ in range(100):  # 后台线程很快，轮询到结束即可
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] != "running":
            break

    assert job["state"] == "done"
    assert job["result"]["chunk_count"] == 5
    assert job["result"]["source_id"] == "src_new"


def test_add_source_requires_api_key(client: TestClient, settings: Settings, tmp_path: Path) -> None:
    from app.api.server import create_app as build

    source = tmp_path / "x.pdf"
    source.mkdir(parents=True, exist_ok=True)
    (source / "full.md").write_text("# 甲", encoding="utf-8")
    no_key = replace(settings, api_key="")
    other = TestClient(build(settings=no_key, store=Store(settings.db_uri, dim=DIM)))

    response = other.post("/api/sources", json={"path": str(source)})

    assert response.status_code == 400
    assert "OPENROUTER_API_KEY" in response.json()["detail"]


def blocking_ingest(gate: threading.Event, started: list[str]):
    """假的导入：先记一笔名字，然后卡在 gate 上——方便观察「谁在跑、谁在排队」。"""

    def fake(path, store, *, api_key, model, dim, on_progress=None, **kwargs):
        from app.core.ingest import IngestResult

        resolved = Path(path).resolve()
        started.append(resolved.name)
        gate.wait(timeout=5)
        if on_progress:
            on_progress(1, 1, "向量化")
        return IngestResult(
            source_id=source_id_for(resolved),
            name=resolved.name,
            path=str(resolved),
            chunk_count=1,
            image_count=0,
            skipped=False,
            sha256="deadbeef",
        )

    return fake


def make_source(root: Path, name: str) -> Path:
    source = root / name
    source.mkdir(parents=True, exist_ok=True)
    (source / "full.md").write_text(f"# {name}\n\n" + "这是一段足够长的正文。" * 20, encoding="utf-8")
    return source


def test_add_same_source_twice_conflicts(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一个目录重复导入（连点两次、批量清单里重复选到）→ 409，不是两路并发写库。"""
    gate = threading.Event()
    monkeypatch.setattr(routes_module, "ingest", blocking_ingest(gate, []))
    source = make_source(settings.root, "同一份.pdf")

    first = client.post("/api/sources", json={"path": str(source)})
    second = client.post("/api/sources", json={"path": str(source)})

    assert first.status_code == 202
    assert second.status_code == 409
    assert "已经在导入队列里" in second.json()["detail"]
    gate.set()


def test_more_than_limit_queues_instead_of_rejecting(
    settings: Settings, store: Store, chat: FakeChat, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超过并发上限的目录排队，而不是直接报错；放行后全部跑完。"""
    gate = threading.Event()
    started: list[str] = []
    monkeypatch.setattr(routes_module, "ingest", blocking_ingest(gate, started))

    one_at_a_time = replace(settings, max_parallel_imports=1)
    client = TestClient(create_app(settings=one_at_a_time, store=store))
    sources = [make_source(settings.root, f"目录{index}.pdf") for index in range(3)]

    jobs = [client.post("/api/sources", json={"path": str(source)}) for source in sources]
    assert [response.status_code for response in jobs] == [202, 202, 202]
    job_ids = [response.json()["job_id"] for response in jobs]

    states = [client.get(f"/api/jobs/{job_id}").json()["state"] for job_id in job_ids]
    assert states == ["running", "queued", "queued"], f"上限 1 时应只有一个在跑：{states}"
    assert started == ["目录0.pdf"], "排队的不该提前开跑"

    gate.set()
    for _ in range(200):  # 放行后等它们跑完
        states = [client.get(f"/api/jobs/{job_id}").json()["state"] for job_id in job_ids]
        if all(state == "done" for state in states):
            break
        time.sleep(0.01)
    assert states == ["done", "done", "done"]
    assert sorted(started) == ["目录0.pdf", "目录1.pdf", "目录2.pdf"]


def test_delete_source_removes_chunks(client: TestClient, store: Store) -> None:
    response = client.delete("/api/sources/src_a")

    assert response.status_code == 204
    assert store.stats() == (0, 0)
    assert client.get("/api/sources").json() == []


def test_delete_unknown_source_404(client: TestClient) -> None:
    assert client.delete("/api/sources/nope").status_code == 404


def test_unknown_job_404(client: TestClient) -> None:
    assert client.get("/api/jobs/nope").status_code == 404


# ---------------------------------------------------------------- 对话（SSE）


def test_chat_streams_sources_tokens_done(client: TestClient) -> None:
    with client.stream("POST", "/api/chat", json={"question": "国奖多少钱？"}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        response.read()  # 流式响应要先读完才能取 text
        frames = sse_frames(response)

    assert frames[0]["type"] == "sources"
    assert frames[0]["items"][0]["name"] == "手册.pdf"
    assert frames[0]["items"][0]["preview"]
    assert [frame["type"] for frame in frames[1:-1]] == ["token", "token"]
    assert "".join(frame["text"] for frame in frames[1:-1]) == "答案"
    done = frames[-1]
    assert done["type"] == "done"
    assert done["usage"]["prompt_tokens"] == 42
    assert done["seconds"] >= 0


def test_chat_keeps_history_and_clear_empties_it(client: TestClient, chat: FakeChat) -> None:
    client.post("/api/chat", json={"question": "第一问"}).read()
    client.post("/api/chat", json={"question": "第二问"}).read()

    second_call = chat.calls[-1]
    assert any(message.get("content") == "第一问" for message in second_call), "第二问应带上第一问的历史"

    cleared = client.post("/api/chat/clear")
    assert cleared.status_code == 200
    assert cleared.json() == {"cleared": 4, "turns": 0}

    client.post("/api/chat", json={"question": "第三问"}).read()
    assert len(chat.calls[-1]) == 2, "清空后只剩 system + 本轮问题"


def test_chat_clear_before_any_turn(client: TestClient) -> None:
    assert client.post("/api/chat/clear").json() == {"cleared": 0, "turns": 0}


def test_chat_rejects_empty_question(client: TestClient) -> None:
    assert client.post("/api/chat", json={"question": ""}).status_code == 422


def test_chat_without_key_is_400(tmp_path: Path, store: Store) -> None:
    settings = Settings(root=tmp_path, data_dir=tmp_path, db_uri=store.db_uri, api_key="", embed_dim=DIM)
    client = TestClient(create_app(settings=settings, store=store))

    response = client.post("/api/chat", json={"question": "问"})

    assert response.status_code == 400
    assert "OPENROUTER_API_KEY" in response.json()["detail"]


def test_chat_error_is_reported_as_sse_frame(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """上游出错时要发一个 error 帧，而不是让前端只看到断流。"""

    def boom(self, question):
        raise RuntimeError("上游炸了")
        yield  # pragma: no cover

    monkeypatch.setattr(query_module.ChatSession, "ask", boom)

    frames = sse_frames(client.post("/api/chat", json={"question": "问"}))

    assert frames[-1]["type"] == "error"
    assert "上游炸了" in frames[-1]["message"]


# ---------------------------------------------------------------- 入口


def test_free_port_skips_busy_ports() -> None:
    """端口被占用时要顺延（前端拿到的 port 以握手行为准）。"""
    import socket

    from main import free_port

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(("127.0.0.1", 0))
        port = busy.getsockname()[1]
        busy.listen(1)

        assert free_port("127.0.0.1", port) != port
        assert free_port("127.0.0.1", port) > port


# ---------------------------------------------------------------- 可配置项的可见性


def test_health_shows_configured_endpoints(tmp_path: Path, store: Store) -> None:
    """换了接入点/模型后，health 一眼能看出当前在用什么。"""
    settings = Settings(
        root=tmp_path,
        data_dir=tmp_path,
        db_uri=store.db_uri,
        api_key="k",
        embed_dim=DIM,
        embed_model="my/embed",
        embed_base_url="https://embed.example.com/v1",
        rerank_model="my/rerank",
        rerank_base_url="https://rank.example.com/v1",
        chat_model="my/chat",
        chat_base_url="https://chat.example.com/v1",
    )
    client = TestClient(create_app(settings=settings, store=store))

    body = client.get("/api/health").json()

    assert body["embed_model"] == "my/embed"
    assert body["embed_base_url"] == "https://embed.example.com/v1"
    assert body["rerank_model"] == "my/rerank"
    assert body["rerank_base_url"] == "https://rank.example.com/v1"
    assert body["chat_model"] == "my/chat"
    assert body["chat_base_url"] == "https://chat.example.com/v1"


def test_missing_role_key_is_reported(tmp_path: Path, store: Store) -> None:
    settings = Settings(root=tmp_path, data_dir=tmp_path, db_uri=store.db_uri, api_key="", embed_dim=DIM)
    client = TestClient(create_app(settings=settings, store=store))

    body = client.get("/api/health").json()

    assert body["configured"] is False
    assert body["missing_keys"] == ["嵌入", "重排", "回答"]


def test_sources_are_marked_stale_after_switching_embed_model(tmp_path: Path, store: Store) -> None:
    """库里是用旧嵌入模型入库的，换模型后这些源要标为「需重新导入」。"""
    same = Settings(root=tmp_path, data_dir=tmp_path, db_uri=store.db_uri, api_key="k", embed_dim=DIM)
    assert TestClient(create_app(settings=same, store=store)).get("/api/sources").json()[0]["stale"] is False

    switched = replace(same, embed_model="another/embed-model")
    client = TestClient(create_app(settings=switched, store=store))

    row = client.get("/api/sources").json()[0]
    assert row["stale"] is True
    assert row["embed_model"] == same.embed_model, "行里记着入库时用的模型"
    assert client.get("/api/health").json()["sources_stale"] == 1


# ---------------------------------------------------------------- 设置读写


def test_get_settings_masks_secrets(client: TestClient) -> None:
    """settings 接口绝不能把明文密钥回给前端。"""
    body = client.get("/api/settings").json()

    assert body["api_key_set"] is True
    assert body["api_key_hint"] and "fake-key" not in body["api_key_hint"]
    assert "fake-key" not in json.dumps(body), "响应里不该出现明文密钥"
    assert body["embed_dim"] == DIM
    assert body["store_dim"] == DIM
    assert body["env_file"].endswith(".env")
    assert body["retrieve_top_k"] == 20 and body["context_n"] == 3


def test_put_settings_writes_env_and_applies_live(client: TestClient, settings: Settings) -> None:
    """改配置要落盘到 .env，并且立刻生效（不用重启）。"""
    response = client.put(
        "/api/settings",
        json={"chat_model": "another/chat-model", "retrieve_top_k": 30, "context_n": 5},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["chat_model"] == "another/chat-model"
    assert (body["retrieve_top_k"], body["context_n"]) == (30, 5)

    # 落盘
    env_text = (settings.root / ".env").read_text(encoding="utf-8")
    assert "CHAT_MODEL=another/chat-model" in env_text
    assert "RETRIEVE_TOP_K=30" in env_text

    # 立刻生效：health 与内部状态都变了，且会话被丢弃以便按新配置重建
    assert client.get("/api/health").json()["chat_model"] == "another/chat-model"


def test_put_settings_key_only_replaces_that_key(client: TestClient, settings: Settings) -> None:
    """密钥留空 = 不改；只填新密钥时旧密钥不被清掉，且新值立即生效。"""
    before = client.get("/api/settings").json()

    client.put("/api/settings", json={"chat_api_key": "new-chat-key-1234567890"})
    after = client.get("/api/settings").json()

    assert after["api_key_hint"] == before["api_key_hint"], "共享密钥不该被动到"
    assert after["chat_api_key_set"] is True
    assert "new-chat-key" not in json.dumps(after)


def test_put_settings_ignores_blank_values(client: TestClient) -> None:
    """空字符串代表「不修改」——界面上密钥框留空就不会误清。"""
    before = client.get("/api/settings").json()

    body = client.put("/api/settings", json={"chat_model": "", "api_key": ""}).json()

    assert body["chat_model"] == before["chat_model"]
    assert body["api_key_set"] is True


def test_put_settings_rejects_changing_dimension_with_existing_db(client: TestClient, settings: Settings) -> None:
    """库里已是某维度时改 EMBED_DIM 必须被拒，且不能动 .env。"""
    response = client.put("/api/settings", json={"embed_dim": 2048 if DIM != 2048 else 512})

    assert response.status_code == 400
    assert "现有维度" in response.json()["detail"]
    assert not (settings.root / ".env").exists(), "校验没过就不该写文件"


def test_put_settings_rejects_bad_values(client: TestClient) -> None:
    assert client.put("/api/settings", json={"embed_dim": 999}).status_code == 400
    assert client.put("/api/settings", json={"retrieve_top_k": 0}).status_code == 400
    assert client.put("/api/settings", json={"embed_base_url": "ftp://x"}).status_code == 400


def test_put_settings_rejects_context_larger_than_top_k(client: TestClient) -> None:
    response = client.put("/api/settings", json={"retrieve_top_k": 2, "context_n": 5})

    assert response.status_code == 400
    assert "不能大于" in response.json()["detail"]


def test_put_settings_chat_uses_new_model(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """改完回答模型后，下一次提问要用新模型（会话被重建）。"""
    seen: list[dict] = []

    def fake_stream(**kwargs):
        seen.append(kwargs)
        return FakeChat()

    monkeypatch.setattr(query_module, "ChatStream", fake_stream)
    client.post("/api/chat", json={"question": "旧模型问一句"}).read()  # 先建一次会话

    client.put("/api/settings", json={"chat_model": "brand/new-model"})
    client.post("/api/chat", json={"question": "新模型问一句"}).read()

    assert seen[-1]["model"] == "brand/new-model"

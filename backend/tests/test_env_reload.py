"""`.env` 被手工改动后要不要重启才生效——不该要。

场景（用户报的）：便携包/源码第一次跑时还没有 `.env`，添加目录报「还没有配置 OPENROUTER_API_KEY」；
随后手工把 `.env` 建好，再添加目录仍然报同一句——因为后端只在启动时读了一次 `.env`。
包内 `使用说明.txt` 里承诺过「手工改那个文件也一样管用」，所以这是 bug。

    cd backend && uv run pytest -q tests/test_env_reload.py
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import routes as routes_module
from app.api.server import create_app
from app.config import Settings
from app.core.openrouter import EmbedUsage
from app.core.store import Store


def make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """起一个还没配密钥的 app（root 指向临时目录，等于「第一次跑、还没有 .env」）。"""
    from app.core.ingest import IngestResult

    settings = Settings(root=tmp_path, data_dir=tmp_path, db_uri=str(tmp_path / "lancedb"), api_key="")

    def fake_ingest(path, store, *, api_key, model, dim, on_progress=None, **kwargs):
        assert api_key, "走到导入这一步就说明密钥已经读到了"
        return IngestResult(
            source_id="src_new", name="x", path=str(path), chunk_count=1, image_count=0,
            skipped=False, sha256="deadbeef", usage=EmbedUsage(),
        )

    monkeypatch.setattr(routes_module, "ingest", fake_ingest)
    return TestClient(create_app(settings=settings, store=Store(settings.db_uri, dim=settings.embed_dim)))


def make_source(tmp_path: Path) -> Path:
    source = tmp_path / "论文.pdf"
    source.mkdir(parents=True, exist_ok=True)
    (source / "full.md").write_text("# 甲\n\n正文", encoding="utf-8")
    return source


def test_manual_env_is_picked_up_without_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """手工写好 .env 之后，不用重启也应该能加目录。"""
    client = make_app(tmp_path, monkeypatch)
    source = make_source(tmp_path)

    before = client.post("/api/sources", json={"path": str(source)})
    assert before.status_code == 400
    assert "OPENROUTER_API_KEY" in before.json()["detail"]

    # 用户手工补上密钥（模拟在便携根/backend 目录里新建 .env）
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-v1-manual\n", encoding="utf-8")

    after = client.post("/api/sources", json={"path": str(source)})
    assert after.status_code == 202, f"补上 .env 之后应该能加目录了：{after.text}"

    health = client.get("/api/health").json()
    assert health["configured"] is True


def test_health_reports_env_file_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """健康检查要给出「到底该改哪个 .env」——便携版和源码版路径不一样，写死 backend/.env 会误导人。"""
    client = make_app(tmp_path, monkeypatch)
    health = client.get("/api/health").json()

    assert Path(health["env_file"]) == tmp_path / ".env"


def test_manual_model_change_is_picked_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """手工改模型/接入点同样应该立刻生效，不用重启。"""
    client = make_app(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text(
        "OPENROUTER_API_KEY=k\nCHAT_MODEL=some/other-model\nCHAT_BASE_URL=https://example.test/v1\n",
        encoding="utf-8",
    )

    health = client.get("/api/health").json()
    assert health["chat_model"] == "some/other-model"
    assert health["chat_base_url"] == "https://example.test/v1"


def test_command_line_overrides_survive_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """命令行参数（--port/--data-dir）优先级最高，重读 .env 不能把它们顶掉。"""
    from app.core.ingest import IngestResult

    settings = replace(
        Settings(root=tmp_path, data_dir=tmp_path, db_uri=str(tmp_path / "lancedb"), api_key=""),
        port=12345,
        host="127.0.0.1",
    )
    monkeypatch.setattr(
        routes_module, "ingest",
        lambda *a, **k: IngestResult(source_id="s", name="n", path="p", chunk_count=0, image_count=0,
                                     skipped=True, sha256="x"),
    )
    client = TestClient(create_app(settings=settings, store=Store(settings.db_uri, dim=settings.embed_dim)))

    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=k\nRAGLITE_PORT=9999\nRAGLITE_HOST=0.0.0.0\n", encoding="utf-8")
    client.get("/api/health")  # 触发重读

    assert client.app.state.settings.port == 12345
    assert client.app.state.settings.host == "127.0.0.1"

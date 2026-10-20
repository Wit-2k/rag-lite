r"""配置层的测试：三个模型角色的接入点 / 模型 ID / 密钥 / 维度都要能单独配。

    cd backend && uv run pytest -q tests/test_config.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, default_data_dir, load_settings, mask_secret
from app.core.openrouter import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_DIM,
    DEFAULT_EMBED_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_RERANK_MODEL,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """把可能干扰的环境变量清掉，测试之间互不影响。"""
    for name in (
        "OPENROUTER_API_KEY",
        "EMBED_BASE_URL",
        "EMBED_MODEL",
        "EMBED_DIM",
        "EMBED_API_KEY",
        "RERANK_BASE_URL",
        "RERANK_MODEL",
        "RERANK_API_KEY",
        "CHAT_BASE_URL",
        "CHAT_MODEL",
        "CHAT_API_KEY",
        "RETRIEVE_TOP_K",
        "CONTEXT_N",
        "RAGLITE_HOST",
        "RAGLITE_PORT",
        "RAGLITE_DB_URI",
    ):
        monkeypatch.delenv(name, raising=False)


def write_env(tmp_path: Path, body: str) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(body, encoding="utf-8")
    return env_file


def test_defaults_are_openrouter_and_2048_dim(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path, env_file=tmp_path / "不存在.env")

    assert settings.embed_base_url == DEFAULT_EMBED_BASE_URL
    assert settings.embed_model == DEFAULT_MODEL
    assert settings.embed_dim == DEFAULT_DIM
    assert settings.rerank_model == DEFAULT_RERANK_MODEL
    assert settings.chat_model == DEFAULT_CHAT_MODEL
    assert settings.retrieve_top_k == 20
    assert settings.context_n == 3


def test_env_file_can_override_every_role(tmp_path: Path) -> None:
    env_file = write_env(
        tmp_path,
        """
# 三个角色各配各的
EMBED_BASE_URL=https://embed.example.com/v1/
EMBED_MODEL=some/embed-model
EMBED_DIM=512
EMBED_API_KEY=embed-key
RERANK_BASE_URL=https://rerank.example.com/v1
RERANK_MODEL=some/rerank-model
RERANK_API_KEY=rerank-key
CHAT_BASE_URL=https://chat.example.com/v1
CHAT_MODEL=some/chat-model
CHAT_API_KEY=chat-key
""",
    )

    settings = load_settings(root=tmp_path, env_file=env_file)

    assert settings.embed_base_url == "https://embed.example.com/v1/"
    assert settings.embed_model == "some/embed-model"
    assert settings.embed_dim == 512, "维度要按整数解析，不是字符串"
    assert settings.embed_key == "embed-key"
    assert settings.rerank_model == "some/rerank-model"
    assert settings.rerank_key == "rerank-key"
    assert settings.chat_model == "some/chat-model"
    assert settings.chat_key == "chat-key"


def test_role_keys_fall_back_to_shared_key(tmp_path: Path) -> None:
    env_file = write_env(tmp_path, "OPENROUTER_API_KEY=shared-key\n")

    settings = load_settings(root=tmp_path, env_file=env_file)

    assert settings.api_key == "shared-key"
    assert (settings.embed_key, settings.rerank_key, settings.chat_key) == ("shared-key",) * 3
    assert settings.configured is True
    assert settings.missing_keys == []


def test_role_key_overrides_shared_key(tmp_path: Path) -> None:
    env_file = write_env(
        tmp_path,
        "OPENROUTER_API_KEY=shared\nEMBED_API_KEY=only-embed\n",
    )

    settings = load_settings(root=tmp_path, env_file=env_file)

    assert settings.embed_key == "only-embed"
    assert settings.rerank_key == "shared"
    assert settings.chat_key == "shared"


def test_missing_keys_are_reported(tmp_path: Path) -> None:
    settings = load_settings(root=tmp_path, env_file=tmp_path / "不存在.env")

    assert settings.configured is False
    assert settings.missing_keys == ["嵌入", "重排", "回答"]


def test_environment_beats_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = write_env(tmp_path, "EMBED_MODEL=from-file\nEMBED_DIM=1024\n")
    monkeypatch.setenv("EMBED_MODEL", "from-env")
    monkeypatch.setenv("EMBED_DIM", "256")

    settings = load_settings(root=tmp_path, env_file=env_file)

    assert settings.embed_model == "from-env"
    assert settings.embed_dim == 256


def test_explicit_overrides_beat_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBED_DIM", "256")

    settings = load_settings(root=tmp_path, env_file=tmp_path / "不存在.env", embed_dim=2048, db_uri=None)

    assert settings.embed_dim == 2048
    assert settings.db_uri == str(default_data_dir() / "lancedb"), "None 的覆盖项应被忽略"


def test_other_settings_readable_from_env(tmp_path: Path) -> None:
    env_file = write_env(
        tmp_path,
        "RETRIEVE_TOP_K=30\nCONTEXT_N=5\nRAGLITE_HOST=0.0.0.0\nRAGLITE_PORT=18000\n",
    )

    settings = load_settings(root=tmp_path, env_file=env_file)

    assert (settings.retrieve_top_k, settings.context_n) == (30, 5)
    assert (settings.host, settings.port) == ("0.0.0.0", 18000)


def test_db_uri_override_updates_data_dir(tmp_path: Path) -> None:
    settings = load_settings(
        root=tmp_path, env_file=tmp_path / "不存在.env", db_uri=str(tmp_path / "别的库" / "lancedb")
    )

    assert settings.data_dir == tmp_path / "别的库"


def test_data_dir_override_updates_db_uri(tmp_path: Path) -> None:
    """只给 data_dir 时，库位置要跟着走（别指向默认的 backend/data）。"""
    settings = load_settings(root=tmp_path, env_file=tmp_path / "不存在.env", data_dir=tmp_path / "便携")

    assert settings.db_uri == str(tmp_path / "便携" / "lancedb")


def test_mask_secret_never_leaks_fragments() -> None:
    """设置页只回固定圆点，连头尾字符都不露。"""
    assert mask_secret("sk-or-abcdef123456") == "••••••••"
    assert mask_secret("short") == "••••••••"
    assert mask_secret("") == ""


def test_db_uri_from_env_updates_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RAGLITE_DB_URI 从环境变量进来时，会话库也要跟着落到它的上一级。

    这是第 10 节记录的已知问题：以前只改了向量库位置，sessions.db 还留在 backend/data。
    """
    db_uri = tmp_path / "便携" / "lancedb"
    monkeypatch.setenv("RAGLITE_DB_URI", str(db_uri))

    settings = load_settings(root=tmp_path, env_file=tmp_path / "不存在.env")

    assert settings.db_uri == str(db_uri)
    assert settings.data_dir == tmp_path / "便携"


def test_explicit_data_dir_beats_env_db_uri(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """命令行 --data-dir 的优先级高于环境变量 RAGLITE_DB_URI，库位置应跟着 data_dir 走。"""
    monkeypatch.setenv("RAGLITE_DB_URI", str(tmp_path / "旧库" / "lancedb"))
    settings = load_settings(root=tmp_path, env_file=tmp_path / "不存在.env", data_dir=tmp_path / "便携")

    assert settings.data_dir == tmp_path / "便携"
    assert settings.db_uri == str(tmp_path / "便携" / "lancedb")


def test_settings_is_frozen() -> None:
    """配置对象应当只读，避免运行期被某处悄悄改掉。"""
    settings = Settings()

    with pytest.raises(Exception):
        settings.embed_dim = 123  # type: ignore[misc]

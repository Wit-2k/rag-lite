"""全局配置：一处读环境变量 / `.env`，其他地方只认 Settings。

三个模型角色——**嵌入 / 重排 / 回答**——各自的「接入点 base_url + 模型 ID + 密钥」都能单独配，
密钥没配时回落到共享的 `OPENROUTER_API_KEY`。嵌入维度 `EMBED_DIM` 也是配置项：
它写进向量库 schema，改了维度就必须重建库（`Store` 会给出明确提示，见 app/core/store.py）。

取值优先级：显式传参（命令行）> 环境变量 > `backend/.env` > 默认值。
"""

from __future__ import annotations

from dataclasses import dataclass, replace, fields
from pathlib import Path

from app.core.openrouter import (
    DEFAULT_CHAT_BASE_URL,
    DEFAULT_CHAT_MODEL,
    DEFAULT_DIM,
    DEFAULT_EMBED_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_RERANK_BASE_URL,
    DEFAULT_RERANK_MODEL,
    read_api_key,
    read_env_value,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = BACKEND_DIR / "data"
DEFAULT_DB_NAME = "lancedb"

# 字段 → 环境变量名。想加配置项就在这里加一行，`.env` 和环境变量都会自动生效。
ENV_NAMES: dict[str, str] = {
    "api_key": "OPENROUTER_API_KEY",  # 三个角色共享的兜底密钥
    "embed_base_url": "EMBED_BASE_URL",
    "embed_model": "EMBED_MODEL",
    "embed_dim": "EMBED_DIM",
    "embed_api_key": "EMBED_API_KEY",
    "rerank_base_url": "RERANK_BASE_URL",
    "rerank_model": "RERANK_MODEL",
    "rerank_api_key": "RERANK_API_KEY",
    "chat_base_url": "CHAT_BASE_URL",
    "chat_model": "CHAT_MODEL",
    "chat_api_key": "CHAT_API_KEY",
    "retrieve_top_k": "RETRIEVE_TOP_K",
    "context_n": "CONTEXT_N",
    "host": "RAGLITE_HOST",
    "port": "RAGLITE_PORT",
    "db_uri": "RAGLITE_DB_URI",
}

INT_FIELDS = {"embed_dim", "retrieve_top_k", "context_n", "port"}


@dataclass(frozen=True, slots=True)
class Settings:
    """运行期需要的一切参数。字段都有默认值，`load_settings()` 负责按优先级覆盖。"""

    root: Path = BACKEND_DIR
    data_dir: Path = DEFAULT_DATA_DIR
    db_uri: str = str(DEFAULT_DATA_DIR / DEFAULT_DB_NAME)

    # —— 嵌入（dim 写进库 schema，改了要重建库）——
    embed_base_url: str = DEFAULT_EMBED_BASE_URL
    embed_model: str = DEFAULT_MODEL
    embed_dim: int = DEFAULT_DIM
    embed_api_key: str = ""

    # —— 重排 ——
    rerank_base_url: str = DEFAULT_RERANK_BASE_URL
    rerank_model: str = DEFAULT_RERANK_MODEL
    rerank_api_key: str = ""

    # —— 回答（任意 OpenAI Compatible 端点）——
    chat_base_url: str = DEFAULT_CHAT_BASE_URL
    chat_model: str = DEFAULT_CHAT_MODEL
    chat_api_key: str = ""

    # —— 共享兜底密钥 ——
    api_key: str = ""

    # —— 检索参数 ——
    retrieve_top_k: int = 20
    context_n: int = 3

    # —— 服务 ——
    host: str = "127.0.0.1"
    port: int = 17650

    # 每个角色实际用哪把钥匙：单独配了就用单独的，否则用共享的
    @property
    def embed_key(self) -> str:
        return self.embed_api_key or self.api_key

    @property
    def rerank_key(self) -> str:
        return self.rerank_api_key or self.api_key

    @property
    def chat_key(self) -> str:
        return self.chat_api_key or self.api_key

    @property
    def missing_keys(self) -> list[str]:
        """哪些角色还没配到密钥——用于启动日志和 /api/health。"""
        return [
            name
            for name, key in (("嵌入", self.embed_key), ("重排", self.rerank_key), ("回答", self.chat_key))
            if not key
        ]

    @property
    def configured(self) -> bool:
        return not self.missing_keys


def load_settings(
    *,
    root: Path | None = None,
    env_file: Path | None = None,
    **overrides: object,
) -> Settings:
    """读配置。`overrides` 里值为 None 的项会被忽略（方便直接把 argparse 结果丢进来）。"""
    base = Settings(root=root or BACKEND_DIR)
    env_file = env_file if env_file is not None else base.root / ".env"
    kwargs: dict[str, object] = {}

    for field in fields(Settings):
        env_name = ENV_NAMES.get(field.name)
        if not env_name:
            continue
        value = read_env_value(env_name, env_file=env_file)
        if value:
            kwargs[field.name] = int(value) if field.name in INT_FIELDS else value

    settings = replace(base, **kwargs)  # type: ignore[arg-type]
    explicit = {key: value for key, value in overrides.items() if value is not None}
    if explicit:
        settings = replace(settings, **explicit)  # type: ignore[arg-type]
    if not settings.api_key:  # 兜底：环境变量和 .env 都没读到就再试一次标准读取
        try:
            settings = replace(settings, api_key=read_api_key(env_file=env_file))
        except Exception:
            pass
    if settings.data_dir == DEFAULT_DATA_DIR and settings.db_uri != str(DEFAULT_DATA_DIR / DEFAULT_DB_NAME):
        settings = replace(settings, data_dir=Path(settings.db_uri).parent)
    return settings

"""全局配置：一处读环境变量 / `.env`，其他地方只认 Settings。

取值优先级：显式传参（命令行）> 环境变量 > `backend/.env` > 默认值。
之所以现在才抽出来：前面几步各函数用入参就够了，到了 HTTP 层才真的需要「一份配置多处共享」。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from app.core.openrouter import (
    DEFAULT_CHAT_BASE_URL,
    DEFAULT_CHAT_MODEL,
    DEFAULT_DIM,
    DEFAULT_MODEL,
    DEFAULT_RERANK_MODEL,
    read_api_key,
    read_env_value,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = BACKEND_DIR / "data"
DEFAULT_DB_NAME = "lancedb"


@dataclass(frozen=True, slots=True)
class Settings:
    """运行期需要的一切参数。字段都有默认值，`load_settings()` 负责按优先级覆盖。"""

    root: Path = BACKEND_DIR
    data_dir: Path = DEFAULT_DATA_DIR
    db_uri: str = str(DEFAULT_DATA_DIR / DEFAULT_DB_NAME)

    api_key: str = ""  # OpenRouter 密钥（嵌入 + 重排 + 默认回答模型都用它）
    embed_model: str = DEFAULT_MODEL
    embed_dim: int = DEFAULT_DIM
    rerank_model: str = DEFAULT_RERANK_MODEL

    chat_model: str = DEFAULT_CHAT_MODEL
    chat_base_url: str = DEFAULT_CHAT_BASE_URL
    chat_api_key: str = ""  # 留空则复用 api_key

    retrieve_top_k: int = 20
    context_n: int = 3

    host: str = "127.0.0.1"
    port: int = 17650

    @property
    def configured(self) -> bool:
        """有没有配密钥——没配的话前端应该提示去填，而不是等调用时报错。"""
        return bool(self.api_key)

    @property
    def effective_chat_key(self) -> str:
        """回答用哪把钥匙：单独配了就用单独的，否则复用 OpenRouter 的。"""
        return self.chat_api_key or self.api_key


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

    for field, env_name in (
        ("api_key", "OPENROUTER_API_KEY"),
        ("embed_model", "EMBED_MODEL"),
        ("embed_dim", "EMBED_DIM"),
        ("rerank_model", "RERANK_MODEL"),
        ("chat_model", "CHAT_MODEL"),
        ("chat_base_url", "CHAT_BASE_URL"),
        ("chat_api_key", "CHAT_API_KEY"),
        ("retrieve_top_k", "RETRIEVE_TOP_K"),
        ("context_n", "CONTEXT_N"),
        ("host", "RAGLITE_HOST"),
        ("port", "RAGLITE_PORT"),
        ("db_uri", "RAGLITE_DB_URI"),
    ):
        value = read_env_value(env_name, env_file=env_file)
        if value:
            kwargs[field] = int(value) if field in {"embed_dim", "retrieve_top_k", "context_n", "port"} else value

    settings = replace(base, **kwargs)  # type: ignore[arg-type]
    explicit = {key: value for key, value in overrides.items() if value is not None}
    if explicit:
        settings = replace(settings, **explicit)  # type: ignore[arg-type]
    if not settings.api_key:  # 兜底：环境变量和 .env 都没有时再试一次标准读取（失败就保持为空）
        try:
            settings = replace(settings, api_key=read_api_key(env_file=env_file))
        except Exception:
            pass
    if settings.data_dir == DEFAULT_DATA_DIR and settings.db_uri != str(DEFAULT_DATA_DIR / DEFAULT_DB_NAME):
        settings = replace(settings, data_dir=Path(settings.db_uri).parent)
    return settings

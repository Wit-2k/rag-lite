"""全局配置：一处读环境变量 / `.env`，其他地方只认 Settings。

三个模型角色——**嵌入 / 重排 / 回答**——各自的「接入点 base_url + 模型 ID + 密钥」都能单独配，
密钥没配时回落到共享的 `OPENROUTER_API_KEY`。嵌入维度 `EMBED_DIM` 也是配置项：
它写进向量库 schema，改了维度就必须重建库（`Store` 会给出明确提示，见 app/core/store.py）。

取值优先级：显式传参（命令行）> 环境变量 > `backend/.env` > 默认值。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, fields, replace
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
DEFAULT_DB_NAME = "lancedb"


def backend_root() -> Path:
    """配置与数据的「根」。

    开发时是 `backend/`；PyInstaller 打包后代码在 `_internal/` 里，
    这时应当是**便携根**（exe 的上一级），这样 `.env` 和 `data/` 都在用户一眼能看到的地方，
    整包拷走数据也跟着走。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent
    return BACKEND_DIR


def default_data_dir() -> Path:
    return backend_root() / "data"


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

# 密钥类字段：回给前端时一律打码，永不回传明文
SECRET_FIELDS = {"api_key", "embed_api_key", "rerank_api_key", "chat_api_key"}

# 允许前端改的字段（顺序即界面上的顺序）。root/data_dir/db_uri/host/port 不在其中：
# 它们决定「库在哪」，改它们等于换一个库，属于命令行/文件的活。
EDITABLE_FIELDS = tuple(
    name for name in ENV_NAMES if name not in {"host", "port", "db_uri"}
)


@dataclass(frozen=True, slots=True)
class Settings:
    """运行期需要的一切参数。字段都有默认值，`load_settings()` 负责按优先级覆盖。"""

    root: Path = field(default_factory=backend_root)
    data_dir: Path = field(default_factory=default_data_dir)
    db_uri: str = ""

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

    def __post_init__(self) -> None:
        # db_uri 留空就跟着 data_dir 走，这样只改 data_dir 也能生效（frozen 所以用 object.__setattr__）
        if not self.db_uri:
            object.__setattr__(self, "db_uri", str(Path(self.data_dir) / DEFAULT_DB_NAME))

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
    base = Settings(root=root or backend_root())
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
    # data_dir 与 db_uri 只要给了一个，另一个就跟着走（replace 会把旧值一起带过来，所以两个方向都要处理）
    if "data_dir" in explicit and "db_uri" not in explicit:
        settings = replace(settings, db_uri=str(Path(settings.data_dir) / DEFAULT_DB_NAME))
    elif "db_uri" in explicit and "data_dir" not in explicit:
        settings = replace(settings, data_dir=Path(settings.db_uri).parent)
    if not settings.api_key:  # 兜底：环境变量和 .env 都没读到就再试一次标准读取
        try:
            settings = replace(settings, api_key=read_api_key(env_file=env_file))
        except Exception:
            pass
    return settings


# —— 读写 .env（前端改配置就落到这个文件）————————————————————————————


def env_file_path(root: Path | None = None) -> Path:
    """当前生效的 .env 路径：开发期 backend/.env，便携版 <便携根>/.env。"""
    return (root or backend_root()) / ".env"


def mask_secret(value: str) -> str:
    """打码：只露头尾，用于回显「配了哪把钥匙」，绝不回传明文。"""
    if not value:
        return ""
    return f"{value[:6]}…{value[-4:]}" if len(value) > 12 else "已配置"


def read_env_file(path: Path) -> dict[str, str]:
    """把 .env 读成 {环境变量名: 值}（忽略注释行）。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """把若干「环境变量名 → 值」写进 .env，尽量保留原有注释与未涉及的项。

    - 该键已有生效行 → 就地替换；
    - 该键只有被注释掉的行（比如 .env.example 抄来的）→ 取消注释并写入；
    - 完全没有 → 追加到文件末尾。
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(updates)
    result: list[str] = []

    for line in lines:
        stripped = line.strip()
        target = None
        for key in list(remaining):
            if stripped == f"{key}={remaining[key]}" or stripped.split("=", 1)[0].strip() == key:
                target = key
            elif stripped.startswith("#") and stripped.lstrip("# ").split("=", 1)[0].strip() == key:
                target = key  # 被注释掉的模板行
        if target is not None:
            result.append(f"{target}={remaining.pop(target)}")
        else:
            result.append(line)

    if remaining:
        if result and result[-1].strip():
            result.append("")
        result.append("# 由 rag-lite 界面写入")
        result.extend(f"{key}={value}" for key, value in remaining.items())

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(result).rstrip("\n") + "\n", encoding="utf-8")

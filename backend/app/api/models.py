"""HTTP 接口的请求/响应模型。字段与前端 DTO 一一对应，改动要同步 frontend/Models。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthOut(BaseModel):
    """就绪探测：前端据此判断「后端起来了没」「密钥配了没」「库里有多少东西」。"""

    status: str = "ok"
    chunks: int
    sources: int
    sources_stale: int  # 用别的嵌入模型/维度入库、需要重新导入的数据源数量
    configured: bool
    missing_keys: list[str] = []  # 哪些角色还没配到密钥
    embed_dim: int
    embed_model: str
    embed_base_url: str
    rerank_model: str
    rerank_base_url: str
    chat_model: str
    chat_base_url: str
    data_dir: str


class SourceOut(BaseModel):
    """一个数据源（MinerU 产物目录）。missing=True 表示目录里的 full.md 已经不在了。"""

    source_id: str
    name: str
    path: str
    chunk_count: int
    image_count: int
    added_at: str
    embedded_at: str
    missing: bool = False
    embed_model: str = ""
    embed_dim: int = 0
    stale: bool = False  # 入库时用的嵌入模型/维度与当前配置不一致 → 建议重新导入


class SourceCreateIn(BaseModel):
    """添加数据源：传 MinerU 产物目录的绝对路径。"""

    path: str = Field(min_length=1, description="MinerU 产物目录（里面要有 full.md）")
    name: str | None = Field(default=None, description="显示名，留空则用目录名")


class JobAcceptedOut(BaseModel):
    """耗时任务（导入）的受理回执，前端拿 job_id 去轮询进度。"""

    job_id: str
    kind: str


class JobOut(BaseModel):
    """任务状态。state: running / done / failed。"""

    id: str
    kind: str
    state: str
    total: int = 0
    done: int = 0
    message: str = ""
    error: str = ""
    result: dict | None = None
    started_at: str = ""
    finished_at: str = ""


class ChatIn(BaseModel):
    """提问。历史由后端会话持有，前端不需要每次上传。"""

    question: str = Field(min_length=1)


class ChatClearOut(BaseModel):
    """清空上下文的结果：被清掉的消息条数与轮数。"""

    cleared: int
    turns: int


class ErrorOut(BaseModel):
    """统一的错误体，方便前端直接显示 detail。"""

    detail: str


class SettingsOut(BaseModel):
    """当前生效的配置。密钥只回打码后的提示，永不回传明文。"""

    env_file: str  # 这些值落在哪个文件里
    data_dir: str
    db_uri: str
    store_dim: int  # 向量库实际维度（与 embed_dim 必须一致，否则启动时就报错了）

    api_key_set: bool
    api_key_hint: str

    embed_base_url: str
    embed_model: str
    embed_dim: int
    embed_api_key_set: bool
    embed_api_key_hint: str

    rerank_base_url: str
    rerank_model: str
    rerank_api_key_set: bool
    rerank_api_key_hint: str

    chat_base_url: str
    chat_model: str
    chat_api_key_set: bool
    chat_api_key_hint: str

    retrieve_top_k: int
    context_n: int


class SettingsIn(BaseModel):
    """要改的配置项。**留空 / 不传 = 不改**（密钥因此可以只填新值、不必重敲旧密钥）。

    改 `embed_dim` 必须能对上向量库现有维度，否则会被拒绝（换模型要重建库）。
    """

    api_key: str | None = None

    embed_base_url: str | None = None
    embed_model: str | None = None
    embed_dim: int | None = None
    embed_api_key: str | None = None

    rerank_base_url: str | None = None
    rerank_model: str | None = None
    rerank_api_key: str | None = None

    chat_base_url: str | None = None
    chat_model: str | None = None
    chat_api_key: str | None = None

    retrieve_top_k: int | None = None
    context_n: int | None = None

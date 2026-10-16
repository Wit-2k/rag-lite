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
    env_file: str  # 该改哪个 .env：便携版是便携根、源码运行是 backend/——别让用户猜


class SessionOut(BaseModel):
    """一个会话（列表项）。`message_count` 用于列表里显示「N 条消息」。"""

    session_id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int = 0
    group_id: str | None = None  # 检索范围：绑定的数据源分组；None = 全部数据源


class SessionCreateIn(BaseModel):
    """新建会话；标题留空则用默认名（发出第一个问题后会自动命名）。"""

    title: str | None = Field(default=None, max_length=200)
    group_id: str | None = None  # 顺手绑定检索范围（None = 全部数据源）


class SessionRenameIn(BaseModel):
    """重命名会话。"""

    title: str = Field(min_length=1, max_length=200)


class MessageOut(BaseModel):
    """一条聊天记录。助手消息会带上本轮用到的参考片段，便于切换会话时还原来源行。"""

    message_id: int
    role: str  # user / assistant
    content: str
    sources: list[dict] = []
    created_at: str


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
    group_ids: list[str] = []  # 它所在的分组（可以同时在多个分组里）


class SourceCreateIn(BaseModel):
    """添加数据源：传 MinerU 产物目录的绝对路径。"""

    path: str = Field(min_length=1, description="MinerU 产物目录（里面要有 full.md）")
    name: str | None = Field(default=None, description="显示名，留空则用目录名")


class GroupOut(BaseModel):
    """一个数据源分组。`source_count` 用于界面上显示「N 个数据源」。"""

    group_id: str
    name: str
    created_at: str
    source_count: int = 0


class GroupCreateIn(BaseModel):
    """新建分组；名字不许空、不许与别的分组重名。"""

    name: str = Field(min_length=1, max_length=40)


class GroupRenameIn(BaseModel):
    """给分组改名（规则同新建）。"""

    name: str = Field(min_length=1, max_length=40)


class SourceGroupsIn(BaseModel):
    """把一个数据源的分组整体替换成这些（可以给多个分组；空列表 = 移出所有分组）。"""

    group_ids: list[str] = []


class SessionGroupIn(BaseModel):
    """设置某个会话的检索范围；`group_id=null` 表示「全部数据源」。"""

    group_id: str | None = None


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
    """提问。必须带 session_id：历史存在库里，按会话取，前端不需要每次上传。"""

    question: str = Field(min_length=1)
    session_id: str = Field(min_length=1, description="往哪个会话里问")


class ChatClearIn(BaseModel):
    """清空某个会话的消息（会话本身保留）。"""

    session_id: str = Field(min_length=1)


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

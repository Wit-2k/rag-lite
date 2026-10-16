"""HTTP 接口的请求/响应模型。字段与前端 DTO 一一对应，改动要同步 frontend/Models。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthOut(BaseModel):
    """就绪探测：前端据此判断「后端起来了没」「密钥配了没」「库里有多少东西」。"""

    status: str = "ok"
    chunks: int
    sources: int
    configured: bool
    embed_dim: int
    embed_model: str
    chat_model: str
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

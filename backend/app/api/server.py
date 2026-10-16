"""组装 FastAPI 应用：把配置、向量库、任务表、对话会话挂到 app.state 上。

`create_app()` 不启动服务器，也不依赖网络（不建对话会话、不调接口），
所以测试可以直接 `TestClient(create_app(...))` 全套跑通。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import FastAPI

from app.api.jobs import JobRegistry
from app.api.routes import router
from app.config import Settings
from app.core.store import Store


def create_app(
    *,
    settings: Settings,
    store: Store | None = None,
    on_startup: Callable[[], None] | None = None,
) -> FastAPI:
    """建应用。`store` 可注入（测试用临时库）；`on_startup` 用于启动后打印握手行。"""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 端口真正绑定成功之后才回调，前端因此不会在端口没起来时就发请求
        if on_startup:
            on_startup()
        yield

    app = FastAPI(
        title="rag-lite",
        version="0.1.0",
        description="MinerU 产物 → 分块 → 向量化 → 检索 → 重排 → 流式问答（本机自用）",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store or Store(settings.db_uri, dim=settings.embed_dim)
    app.state.jobs = JobRegistry()
    app.state.chat = None  # 首次提问时才建会话（这样没配密钥也能正常启动）
    app.include_router(router)
    return app


def data_files(settings: Settings) -> list[Path]:
    """库里落盘的位置，打包/备份时会用到。"""
    return [Path(settings.db_uri)]

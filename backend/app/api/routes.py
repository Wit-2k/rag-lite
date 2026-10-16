"""HTTP 路由：就绪探测、数据源管理与导入、对话（SSE）。

七个端点全放在一个文件里——总共不到 200 行，拆三个文件反而更难读。
对话是 SSE：`data: {...}\\n\\n` 逐帧推，前端按 `type` 分派（sources / token / done / error）。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.jobs import DuplicateError, Job
from app.api.models import (
    ChatClearOut,
    ChatIn,
    ErrorOut,
    HealthOut,
    JobAcceptedOut,
    JobOut,
    SettingsIn,
    SettingsOut,
    SourceCreateIn,
    SourceOut,
)
from app.config import EDITABLE_FIELDS, ENV_NAMES, mask_secret, update_env_file
from app.core.ingest import ingest, remove_source
from app.core.openrouter import ALLOWED_DIMS
from app.core.query import ChatSession, DoneEvent, RetrievalEvent, TokenEvent
from app.core.schema import source_id_for
from app.core.store import Store, StoreError

router = APIRouter(prefix="/api")

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def get_store(request: Request) -> Store:
    return request.app.state.store


def is_stale(row: dict, settings) -> bool:
    """入库时用的嵌入模型/维度与当前配置不一致 → 这行向量不可信，要重新导入。"""
    return row.get("embed_model") != settings.embed_model or int(row.get("embed_dim") or 0) != settings.embed_dim


def source_rows(store: Store, settings) -> list[SourceOut]:
    """把库里的数据源登记转成前端要的形状；顺手判断产物目录是否还在、向量是否过期。"""
    rows = []
    for row in sorted(store.list_sources(), key=lambda item: item.get("name", "")):
        path = Path(row["path"])
        rows.append(
            SourceOut(
                source_id=row["source_id"],
                name=row["name"],
                path=row["path"],
                chunk_count=row["chunk_count"],
                image_count=row.get("image_count", 0),
                added_at=row["added_at"],
                embedded_at=row["embedded_at"],
                missing=not (path / "full.md").is_file(),
                embed_model=row.get("embed_model", ""),
                embed_dim=int(row.get("embed_dim") or 0),
                stale=is_stale(row, settings),
            )
        )
    return rows


@router.get("/health", response_model=HealthOut)
def health(request: Request) -> HealthOut:
    settings = request.app.state.settings
    store = get_store(request)
    chunks, sources = store.stats()
    stale = sum(1 for row in store.list_sources() if is_stale(row, settings))
    return HealthOut(
        chunks=chunks,
        sources=sources,
        sources_stale=stale,
        configured=settings.configured,
        missing_keys=settings.missing_keys,
        # 三个角色当前实际在用的接入点与模型——换配置后一眼就能确认有没有生效
        embed_dim=store.dim,
        embed_model=settings.embed_model,
        embed_base_url=settings.embed_base_url,
        rerank_model=settings.rerank_model,
        rerank_base_url=settings.rerank_base_url,
        chat_model=settings.chat_model,
        chat_base_url=settings.chat_base_url,
        data_dir=str(settings.data_dir),
    )


@router.get("/sources", response_model=list[SourceOut])
def list_sources(request: Request) -> list[SourceOut]:
    return source_rows(get_store(request), request.app.state.settings)


@router.post("/sources", response_model=JobAcceptedOut, status_code=202, responses={400: {"model": ErrorOut}, 409: {"model": ErrorOut}})
def add_source(payload: SourceCreateIn, request: Request) -> JobAcceptedOut:
    """登记一个 MinerU 产物目录并开始导入（后台任务，立刻返回 job_id；超过并发上限则排队）。"""
    settings = request.app.state.settings
    store = get_store(request)
    path = Path(payload.path)
    resolved = path.resolve()

    if not (path / "full.md").is_file():
        raise HTTPException(status_code=400, detail=f"目录里没有 full.md：{path}（请先在 MinerU 客户端完成解析）")
    if not settings.configured:
        raise HTTPException(status_code=400, detail="还没有配置 OPENROUTER_API_KEY，无法向量化（见 backend/.env.example）")

    def work(job: Job) -> dict:
        result = ingest(
            resolved,
            store,
            api_key=settings.embed_key,
            model=settings.embed_model,
            dim=settings.embed_dim,
            base_url=settings.embed_base_url,
            on_progress=request.app.state.jobs.progress(job),
        )
        # 跳过时不经过任何进度回调，这里把进度补成「完成」，免得前端停在 0/1
        job.total = job.total or 1
        job.done = job.total
        job.message = f"已跳过（{result.reason}）" if result.skipped else "完成"
        return {
            "source_id": result.source_id,
            "name": payload.name or result.name,
            "chunk_count": result.chunk_count,
            "image_count": result.image_count,
            "skipped": result.skipped,
            "reason": result.reason,
            "cost_usd": result.usage.cost_usd,
            "seconds": round(result.seconds, 1),
            "warnings": result.warnings[:20],
        }

    try:
        # 去重键用「按目录算出的数据源 id」：同一个目录重复点、或在批量清单里重复选到，
        # 第二次直接 409，而不是两路并发「先删后写」把两代分块留在库里。
        job = request.app.state.jobs.start("ingest", work, dedup_key=source_id_for(resolved))
    except DuplicateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JobAcceptedOut(job_id=job.id, kind=job.kind)


@router.delete("/sources/{source_id}", status_code=204, responses={404: {"model": ErrorOut}})
def delete_source(source_id: str, request: Request, purge: bool = True) -> None:
    """移除数据源。purge=true（默认）连它的分块一起删。"""
    store = get_store(request)
    if store.get_source(source_id) is None:
        raise HTTPException(status_code=404, detail=f"没有这个数据源：{source_id}")
    remove_source(source_id, store)


@router.get("/jobs/{job_id}", response_model=JobOut, responses={404: {"model": ErrorOut}})
def get_job(job_id: str, request: Request) -> JobOut:
    """前端每 500ms 轮询一次即可。"""
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"没有这个任务：{job_id}")
    return JobOut(**job.as_dict())


def get_chat_session(request: Request) -> ChatSession:
    """会话在内存里，首次提问时创建（这样没配密钥也能正常启动）。"""
    session = getattr(request.app.state, "chat", None)
    if session is None:
        settings = request.app.state.settings
        session = ChatSession(
            get_store(request),
            api_key=settings.embed_key,  # 兜底：三个角色都没单独配时用共享密钥
            chat_model=settings.chat_model,
            chat_base_url=settings.chat_base_url,
            chat_api_key=settings.chat_key,
            embed_model=settings.embed_model,
            embed_base_url=settings.embed_base_url,
            embed_api_key=settings.embed_key,
            rerank_model=settings.rerank_model,
            rerank_base_url=settings.rerank_base_url,
            rerank_api_key=settings.rerank_key,
            retrieve_top_k=settings.retrieve_top_k,
            context_n=settings.context_n,
        )
        request.app.state.chat = session
    return session


@router.post("/chat")
def chat(payload: ChatIn, request: Request) -> StreamingResponse:
    """提问，返回 text/event-stream。历史默认全带（后端会话持有），清空用 /api/chat/clear。"""
    settings = request.app.state.settings
    if not settings.configured:
        raise HTTPException(status_code=400, detail="还没有配置 OPENROUTER_API_KEY，无法检索与回答")
    session = get_chat_session(request)

    def events():
        try:
            for event in session.ask(payload.question):
                if isinstance(event, RetrievalEvent):
                    frame = {
                        "type": "sources",
                        "items": [
                            {
                                "rank": source.rank,
                                "name": source.name,
                                "heading": source.heading,
                                "score": round(source.rerank_score, 4),
                                "distance": round(source.cosine_distance, 4),
                                "start": source.start,
                                "end": source.end,
                                "images": list(source.images),
                                "preview": " ".join(source.text.split())[:120],
                            }
                            for source in event.sources
                        ],
                    }
                elif isinstance(event, TokenEvent):
                    frame = {"type": "token", "text": event.text}
                else:
                    assert isinstance(event, DoneEvent)
                    frame = {
                        "type": "done",
                        "seconds": round(event.seconds, 2),
                        "usage": {
                            "prompt_tokens": event.usage.prompt_tokens,
                            "completion_tokens": event.usage.completion_tokens,
                            "cost_usd": event.usage.cost_usd,
                        },
                    }
                yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
        except Exception as exc:  # 出错也要让前端知道，而不是静默断流
            yield f"data: {json.dumps({'type': 'error', 'message': f'{type(exc).__name__}: {exc}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/chat/clear", response_model=ChatClearOut)
def clear_chat(request: Request) -> ChatClearOut:
    """清空对话上下文（历史不再随下一次提问发出）。"""
    session = getattr(request.app.state, "chat", None)
    if session is None:
        return ChatClearOut(cleared=0, turns=0)
    cleared = session.clear()
    return ChatClearOut(cleared=cleared, turns=session.turns)


# ---------------------------------------------------------------- 设置


def env_file_for(settings) -> Path:
    """当前生效的 .env：settings.root 是便携根（开发期即 backend/）。"""
    return Path(settings.root) / ".env"


def settings_payload(settings, store: Store) -> SettingsOut:
    """把生效配置转成回给前端的形状：密钥打码，附上文件位置与实际库维度。"""
    return SettingsOut(
        env_file=str(env_file_for(settings)),
        data_dir=str(settings.data_dir),
        db_uri=settings.db_uri,
        store_dim=store.dim,
        api_key_set=bool(settings.api_key),
        api_key_hint=mask_secret(settings.api_key),
        embed_base_url=settings.embed_base_url,
        embed_model=settings.embed_model,
        embed_dim=settings.embed_dim,
        embed_api_key_set=bool(settings.embed_api_key),
        embed_api_key_hint=mask_secret(settings.embed_api_key),
        rerank_base_url=settings.rerank_base_url,
        rerank_model=settings.rerank_model,
        rerank_api_key_set=bool(settings.rerank_api_key),
        rerank_api_key_hint=mask_secret(settings.rerank_api_key),
        chat_base_url=settings.chat_base_url,
        chat_model=settings.chat_model,
        chat_api_key_set=bool(settings.chat_api_key),
        chat_api_key_hint=mask_secret(settings.chat_api_key),
        retrieve_top_k=settings.retrieve_top_k,
        context_n=settings.context_n,
    )


@router.get("/settings", response_model=SettingsOut)
def get_settings(request: Request) -> SettingsOut:
    return settings_payload(request.app.state.settings, get_store(request))


@router.put("/settings", response_model=SettingsOut, responses={400: {"model": ErrorOut}})
def put_settings(payload: SettingsIn, request: Request) -> SettingsOut:
    """改配置：写入 .env **并立即生效**（无需重启）。

    - 字符串字段留空 / 不传 = 不改（密钥可以只填新的那把，不必重敲旧的）；
    - `embed_dim` 要能对上向量库现有维度，否则拒绝并说明怎么办（换嵌入模型得重建库）；
    - 改完之后丢掉当前对话会话，让下一次提问用新模型重建。
    """
    app = request.app
    current = app.state.settings
    store = get_store(request)

    # 空字符串视为「不修改」；None 已被 exclude_none 去掉
    updates = {
        name: value
        for name, value in payload.model_dump(exclude_none=True).items()
        if not (isinstance(value, str) and not value.strip())
    }
    if not updates:
        return settings_payload(current, store)

    unknown = set(updates) - set(EDITABLE_FIELDS)
    if unknown:
        raise HTTPException(status_code=400, detail=f"这些项不支持在界面里改：{sorted(unknown)}")
    if "embed_dim" in updates and int(updates["embed_dim"]) not in ALLOWED_DIMS:
        raise HTTPException(status_code=400, detail=f"嵌入维度只能是 {list(ALLOWED_DIMS)}")
    for name in ("retrieve_top_k", "context_n"):
        if name in updates and int(updates[name]) < 1:
            raise HTTPException(status_code=400, detail=f"{ENV_NAMES[name]} 必须 ≥ 1")
    top_k = int(updates.get("retrieve_top_k", current.retrieve_top_k))
    context_n = int(updates.get("context_n", current.context_n))
    if context_n > top_k:
        raise HTTPException(status_code=400, detail=f"送给大模型的片段数（{context_n}）不能大于召回条数（{top_k}）")
    for name, value in updates.items():
        if name.endswith("_base_url") and not str(value).startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail=f"{name} 必须是 http:// 或 https:// 开头的地址")

    # 换维度 = 换向量空间：库里有别的维度就直接拒绝，别把 .env 改了却起不来
    new_store: Store | None = None
    target_dim = int(updates.get("embed_dim", current.embed_dim))
    if target_dim != store.dim:
        try:
            new_store = Store(current.db_uri, dim=target_dim)
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 先落盘再生效：文件写坏了也只是下次启动不对，当前进程用的仍是已验证的值
    env_path = env_file_for(current)
    update_env_file(env_path, {ENV_NAMES[name]: str(value) for name, value in updates.items()})

    new_settings = replace(current, **updates)
    app.state.settings = new_settings
    if new_store is not None:
        app.state.store = new_store
    app.state.chat = None  # 下次提问按新配置重建会话
    print(f"[设置] 已更新 {sorted(updates)} → {env_path}", flush=True)
    return settings_payload(new_settings, app.state.store)

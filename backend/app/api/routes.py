"""HTTP 路由：就绪探测、数据源管理与导入、对话（SSE）。

七个端点全放在一个文件里——总共不到 200 行，拆三个文件反而更难读。
对话是 SSE：`data: {...}\\n\\n` 逐帧推，前端按 `type` 分派（sources / token / done / error）。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.jobs import BusyError, Job
from app.api.models import (
    ChatClearOut,
    ChatIn,
    ErrorOut,
    HealthOut,
    JobAcceptedOut,
    JobOut,
    SourceCreateIn,
    SourceOut,
)
from app.core.ingest import ingest, remove_source
from app.core.query import ChatSession, DoneEvent, RetrievalEvent, TokenEvent
from app.core.store import Store

router = APIRouter(prefix="/api")

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def get_store(request: Request) -> Store:
    return request.app.state.store


def source_rows(store: Store) -> list[SourceOut]:
    """把库里的数据源登记转成前端要的形状；顺手判断产物目录是否还在。"""
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
            )
        )
    return rows


@router.get("/health", response_model=HealthOut)
def health(request: Request) -> HealthOut:
    settings = request.app.state.settings
    store = get_store(request)
    chunks, sources = store.stats()
    return HealthOut(
        chunks=chunks,
        sources=sources,
        configured=settings.configured,
        embed_dim=store.dim,
        embed_model=settings.embed_model,
        chat_model=settings.chat_model,
        data_dir=str(settings.data_dir),
    )


@router.get("/sources", response_model=list[SourceOut])
def list_sources(request: Request) -> list[SourceOut]:
    return source_rows(get_store(request))


@router.post("/sources", response_model=JobAcceptedOut, status_code=202, responses={400: {"model": ErrorOut}, 409: {"model": ErrorOut}})
def add_source(payload: SourceCreateIn, request: Request) -> JobAcceptedOut:
    """登记一个 MinerU 产物目录并开始导入（后台任务，立刻返回 job_id）。"""
    settings = request.app.state.settings
    store = get_store(request)
    path = Path(payload.path)

    if not (path / "full.md").is_file():
        raise HTTPException(status_code=400, detail=f"目录里没有 full.md：{path}（请先在 MinerU 客户端完成解析）")
    if not settings.configured:
        raise HTTPException(status_code=400, detail="还没有配置 OPENROUTER_API_KEY，无法向量化（见 backend/.env.example）")

    def work(job: Job) -> dict:
        result = ingest(
            path,
            store,
            api_key=settings.api_key,
            model=settings.embed_model,
            dim=settings.embed_dim,
            on_progress=request.app.state.jobs.progress(job),
        )
        # 跳过时不经过任何进度回调，这里把进度补成「完成」，免得前端停在 0/1
        job.total = job.total or 1
        job.done = job.total
        job.message = "已跳过（full.md 未变化）" if result.skipped else "完成"
        return {
            "source_id": result.source_id,
            "name": payload.name or result.name,
            "chunk_count": result.chunk_count,
            "image_count": result.image_count,
            "skipped": result.skipped,
            "cost_usd": result.usage.cost_usd,
            "seconds": round(result.seconds, 1),
            "warnings": result.warnings[:20],
        }

    try:
        job = request.app.state.jobs.start("ingest", work)
    except BusyError as exc:
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
            api_key=settings.api_key,
            chat_model=settings.chat_model,
            chat_base_url=settings.chat_base_url,
            chat_api_key=settings.effective_chat_key,
            embed_model=settings.embed_model,
            rerank_model=settings.rerank_model,
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

"""HTTP 路由：就绪探测、数据源管理与导入、对话（SSE）。

二十一个端点按功能分段放在一个文件里（健康 / 数据源 / 任务 / 会话 / 分组 / 以图搜图 / 聊天 / 设置），拆成多个文件反而增加跳转成本。
对话是 SSE：`data: {...}\\n\\n` 逐帧推，前端按 `type` 分派（sources / token / done / error）。
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.jobs import DuplicateError, Job
from app.api.models import (
    ChatClearIn,
    ChatClearOut,
    ChatIn,
    GroupCreateIn,
    GroupOut,
    GroupRenameIn,
    ImageSearchIn,
    ImageSearchItem,
    ImageSearchOut,
    MessageOut,
    SessionCreateIn,
    SessionGroupIn,
    SessionOut,
    SessionRenameIn,
    ErrorOut,
    HealthOut,
    JobAcceptedOut,
    JobOut,
    SettingsIn,
    SettingsOut,
    SourceCreateIn,
    SourceGroupsIn,
    SourceOut,
)
from app.config import EDITABLE_FIELDS, ENV_NAMES, mask_secret, update_env_file
from app.core.chunking import image_files
from app.core.groups import GroupStore
from app.core.ingest import ingest, remove_source
from app.core.openrouter import ALLOWED_DIMS, embed_image_bytes
from app.core.query import ChatSession, DoneEvent, RetrievalEvent, TokenEvent
from app.core.schema import source_id_for
from app.core.sessions import DEFAULT_TITLE, NO_SOURCES_SCOPE, SessionStore
from app.core.store import Store, StoreError

router = APIRouter(prefix="/api")

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_sessions(request: Request) -> SessionStore:
    return request.app.state.sessions


def get_groups(request: Request) -> GroupStore:
    return request.app.state.groups


def is_stale(row: dict, settings) -> bool:
    """入库时用的嵌入模型/维度与当前配置不一致 → 这行向量不可信，要重新导入。"""
    return row.get("embed_model") != settings.embed_model or int(row.get("embed_dim") or 0) != settings.embed_dim


def source_rows(store: Store, settings, groups: GroupStore) -> list[SourceOut]:
    """把库里的数据源登记转成前端要的形状；顺手判断产物目录是否还在、向量是否过期。

    分组关系存在 SQLite 里（分组是使用状态，不进向量库），所以这里做一次拼接：
    一次查完所有源的分组，避免每个源一次查询。
    """
    listed = sorted(store.list_sources(), key=lambda item: item.get("name", ""))
    membership = groups.groups_of_sources([row["source_id"] for row in listed])
    rows = []
    for row in listed:
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
                missing=not path.is_dir(),
                embed_model=row.get("embed_model", ""),
                embed_dim=int(row.get("embed_dim") or 0),
                stale=is_stale(row, settings),
                group_ids=membership.get(row["source_id"], []),
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
        env_file=str(env_file_for(settings)),
    )


@router.get("/sources", response_model=list[SourceOut])
def list_sources(request: Request) -> list[SourceOut]:
    return source_rows(get_store(request), request.app.state.settings, get_groups(request))


@router.post("/sources", response_model=JobAcceptedOut, status_code=202, responses={400: {"model": ErrorOut}, 409: {"model": ErrorOut}})
def add_source(payload: SourceCreateIn, request: Request) -> JobAcceptedOut:
    """登记一个 MinerU 产物目录并开始导入（后台任务，立刻返回 job_id；超过并发上限则排队）。"""
    settings = request.app.state.settings
    store = get_store(request)
    path = Path(payload.path)
    resolved = path.resolve()

    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"目录不存在：{path}")
    full_md = resolved / "full.md"
    text_ready = full_md.is_file() and bool(full_md.read_text(encoding="utf-8").strip())
    if not text_ready and not image_files(resolved):
        raise HTTPException(
            status_code=400,
            detail=f"目录里没有可用的 full.md（缺失或为空），也没有 images/ 或源目录下的图片：{path}"
                   "（文本目录请先在 MinerU 客户端完成解析；纯图片目录放在 images/ 或直接放图片文件）",
        )
    if not settings.configured:
        raise HTTPException(
            status_code=400,
            detail=f"还没有配置 OPENROUTER_API_KEY，无法向量化："
                   f"在界面右上角「设置」里填，或写进 {env_file_for(settings)}（写完会自动生效，不用重启）",
        )

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
    # 顺手清掉它的分组关系，免得分组里留着一条指向已删数据源的成员记录
    get_groups(request).forget_source(source_id)


@router.get("/jobs/{job_id}", response_model=JobOut, responses={404: {"model": ErrorOut}})
def get_job(job_id: str, request: Request) -> JobOut:
    """前端每 500ms 轮询一次即可。"""
    job = request.app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"没有这个任务：{job_id}")
    return JobOut(**job.as_dict())


def build_chat_session(request: Request, session_id: str) -> ChatSession:
    """按会话构建一次问答用的 ChatSession：历史从库里读，不再有内存单例。

    每次请求新建一个很便宜（只持有配置与一个列表），好处是不必再管「切会话」时那份内存状态，
    两个会话也不会互相串历史。
    """
    settings = request.app.state.settings
    sessions = get_sessions(request)
    session_row = sessions.get(session_id) or {}
    return ChatSession(
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
        history=sessions.history(session_id),
        source_ids=retrieval_scope(request, session_id),
        grounded=session_row.get("group_id") != NO_SOURCES_SCOPE,
    )


def retrieval_scope(request: Request, session_id: str) -> list[str] | None:
    """这个会话该在哪些数据源里检索。

    - 没绑分组（老会话、或选了「全部数据源」）→ None：不限制
    - 绑了 ``__no_sources__`` → 空列表：明确不使用任何数据源
    - 绑了分组 → 只搜那一组里的数据源；分组里为空就是空列表（一条都不搜，不会退回全库）
    - 分组已经不存在（比如手工改过库）→ 退回 None，与不带分组的老行为一致
    """
    group_id = (get_sessions(request).get(session_id) or {}).get("group_id")
    if group_id == NO_SOURCES_SCOPE:
        return []   # 明确不使用数据源：一条都不搜
    if not group_id:
        return None
    groups = get_groups(request)
    if groups.get(group_id) is None:
        return None
    return groups.sources_of(group_id)


# ---------------------------------------------------------------- 以图搜图


def _resolve_hit_image(row: dict, hit: dict) -> Path | None:
    """把检索命中的图片还原成本机绝对路径。"""
    base = Path(row["path"])
    rel = str(hit.get("rel_path") or "")
    if rel:
        exact = base / rel
        if exact.is_file():
            return exact
    for name in hit.get("images") or ():
        for candidate in (base / "images" / name, base / name):
            if candidate.is_file():
                return candidate
    return None


@router.post(
    "/search/image",
    response_model=ImageSearchOut,
    responses={400: {"model": ErrorOut}, 502: {"model": ErrorOut}},
)
def search_by_image(payload: ImageSearchIn, request: Request) -> ImageSearchOut:
    """以图搜图：查询图片嵌入后，只在纯图片块里做余弦检索。"""
    settings = request.app.state.settings
    if not settings.configured:
        raise HTTPException(status_code=400, detail="还没有配置 OPENROUTER_API_KEY，无法进行图片嵌入")

    mime = payload.mime.lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime not in {"image/png", "image/jpeg", "image/webp"}:
        raise HTTPException(status_code=400, detail="只支持 PNG、JPEG（jpg/jpeg）和 WebP 图片")
    try:
        data = base64.b64decode(payload.image_base64, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"图片 base64 无效：{exc}") from exc
    if not data:
        raise HTTPException(status_code=400, detail="图片内容为空")
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片不能超过 10MB")
    if mime == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(status_code=400, detail="文件内容不是有效的 PNG")
    if mime == "image/jpeg" and not data.startswith(b"\xff\xd8\xff"):
        raise HTTPException(status_code=400, detail="文件内容不是有效的 JPEG")
    if mime == "image/webp" and not (
        len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    ):
        raise HTTPException(status_code=400, detail="文件内容不是有效的 WebP")

    try:
        embedded = embed_image_bytes(
            data,
            mime=mime,
            api_key=settings.embed_key,
            model=settings.embed_model,
            dim=settings.embed_dim,
            base_url=settings.embed_base_url,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"图片嵌入失败：{exc}") from exc

    store = get_store(request)
    hits = store.search(embedded.vectors[0], top_k=payload.top_k, image_only=True)
    source_rows = {row["source_id"]: row for row in store.list_sources()}
    items: list[ImageSearchItem] = []
    for rank, hit in enumerate(hits, start=1):
        row = source_rows.get(hit["source_id"])
        if row is None:
            continue
        image_path = _resolve_hit_image(row, hit)
        if image_path is None:
            continue
        items.append(
            ImageSearchItem(
                rank=rank,
                source_id=hit["source_id"],
                name=row["name"],
                heading=hit["heading"],
                image_path=str(image_path),
                score=round(1.0 - float(hit.get("_distance") or 0.0), 4),
            )
        )
    return ImageSearchOut(items=items)


# ---------------------------------------------------------------- 会话


@router.get("/sessions", response_model=list[SessionOut])
def list_sessions(request: Request) -> list[SessionOut]:
    """按最近更新倒序列出所有会话。"""
    return [SessionOut(**row) for row in get_sessions(request).list()]


@router.post("/sessions", response_model=SessionOut, status_code=201, responses={404: {"model": ErrorOut}})
def create_session(payload: SessionCreateIn, request: Request) -> SessionOut:
    """新建会话（标题留空则用默认名，发出第一个问题后会自动命名）。"""
    if payload.group_id and payload.group_id != NO_SOURCES_SCOPE and get_groups(request).get(payload.group_id) is None:
        raise HTTPException(status_code=404, detail=f"没有这个分组：{payload.group_id}")
    row = get_sessions(request).create(payload.title or DEFAULT_TITLE, group_id=payload.group_id)
    return SessionOut(**row)


@router.put("/sessions/{session_id}", response_model=SessionOut, responses={404: {"model": ErrorOut}})
def rename_session(session_id: str, payload: SessionRenameIn, request: Request) -> SessionOut:
    """重命名会话。"""
    sessions = get_sessions(request)
    if not sessions.rename(session_id, payload.title):
        raise HTTPException(status_code=404, detail=f"没有这个会话：{session_id}")
    row = sessions.get(session_id) or {}
    row.setdefault("message_count", len(sessions.messages(session_id)))
    return SessionOut(**row)


@router.delete("/sessions/{session_id}", status_code=204, responses={404: {"model": ErrorOut}})
def delete_session(session_id: str, request: Request) -> None:
    """删会话，连带它的聊天记录（前端会先弹一次确认）。"""
    if not get_sessions(request).delete(session_id):
        raise HTTPException(status_code=404, detail=f"没有这个会话：{session_id}")


@router.get("/sessions/{session_id}/messages", response_model=list[MessageOut], responses={404: {"model": ErrorOut}})
def session_messages(session_id: str, request: Request) -> list[MessageOut]:
    """某个会话的全部消息（含助手消息附带的参考片段），切换到该会话时用它渲染。"""
    sessions = get_sessions(request)
    if sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail=f"没有这个会话：{session_id}")
    return [MessageOut(**row) for row in sessions.messages(session_id)]


@router.put("/sessions/{session_id}/group", response_model=SessionOut, responses={404: {"model": ErrorOut}})
def set_session_group(session_id: str, payload: SessionGroupIn, request: Request) -> SessionOut:
    """设置某个会话的检索范围：绑一个数据源分组、传 null 表示「全部数据源」，或传 ``__no_sources__`` 表示「不使用数据源」。

    只影响之后的问题（已经答过的内容不动），也不会把会话顶到列表最前面。
    """
    groups = get_groups(request)
    if payload.group_id is not None and payload.group_id != NO_SOURCES_SCOPE and groups.get(payload.group_id) is None:
        raise HTTPException(status_code=404, detail=f"没有这个分组：{payload.group_id}")
    sessions = get_sessions(request)
    if not sessions.set_group(session_id, payload.group_id):
        raise HTTPException(status_code=404, detail=f"没有这个会话：{session_id}")
    row = sessions.get(session_id) or {}
    row.setdefault("message_count", len(sessions.messages(session_id)))
    return SessionOut(**row)


# ---------------------------------------------------------------- 数据源分组


@router.get("/groups", response_model=list[GroupOut])
def list_groups(request: Request) -> list[GroupOut]:
    """所有分组（带成员数），按名字排。界面上的分组下拉与分组管理都用它。"""
    return [GroupOut(**row) for row in get_groups(request).list()]


@router.post("/groups", response_model=GroupOut, status_code=201, responses={409: {"model": ErrorOut}})
def create_group(payload: GroupCreateIn, request: Request) -> GroupOut:
    """新建分组。名字空/超过 40 字由请求模型挡下（422），重名与保留名返回 409。"""
    result = get_groups(request).create(payload.name)
    if not result.ok:
        raise HTTPException(status_code=409, detail=group_error_detail(result))
    return GroupOut(**result.group)


@router.put("/groups/{group_id}", response_model=GroupOut, responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}})
def rename_group(group_id: str, payload: GroupRenameIn, request: Request) -> GroupOut:
    """给分组改名（规则同新建）。"""
    result = get_groups(request).rename(group_id, payload.name)
    if not result.ok:
        if result.reason == "missing":
            raise HTTPException(status_code=404, detail=f"没有这个分组：{group_id}")
        raise HTTPException(status_code=409, detail=group_error_detail(result))
    return GroupOut(**result.group)


@router.delete("/groups/{group_id}", status_code=204, responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}})
def delete_group(group_id: str, request: Request) -> None:
    """删分组。组里的数据源本身不受影响（只是回到「未分组」）。

    还有会话拿它当检索范围就**拒绝删除**：否则那些会话的范围会在用户不知情时变大。
    接口把在用的会话标题一起回去，前端可以直接告诉用户去改哪几个。
    """
    result = get_groups(request).delete(group_id)
    if result.ok:
        return
    if result.reason == "missing":
        raise HTTPException(status_code=404, detail=f"没有这个分组：{group_id}")
    names = "、".join(f"《{row['title']}》" for row in result.blocking)
    raise HTTPException(
        status_code=409,
        detail=f"还有 {len(result.blocking)} 个会话在用这个分组：{names}。"
               f"先把这些会话的检索范围改成别的分组（或「全部数据源」），再来删。",
    )


@router.put("/sources/{source_id}/groups", response_model=SourceOut, responses={404: {"model": ErrorOut}})
def set_source_groups(source_id: str, payload: SourceGroupsIn, request: Request) -> SourceOut:
    """设置一个数据源属于哪些分组（整体替换；空列表 = 移出所有分组）。"""
    store = get_store(request)
    if store.get_source(source_id) is None:
        raise HTTPException(status_code=404, detail=f"没有这个数据源：{source_id}")
    groups = get_groups(request)
    result = groups.set_source_groups(source_id, payload.group_ids)
    if not result.ok:
        raise HTTPException(status_code=404, detail=f"没有这些分组：{'、'.join(result.unknown)}")
    rows = source_rows(store, request.app.state.settings, groups)
    return next(row for row in rows if row.source_id == source_id)


def group_error_detail(result) -> str:
    """把分组写操作的失败原因翻成人话（前端直接把 detail 显示给用户）。"""
    if result.reason == "duplicate":
        return "已经有同名的分组了，换个名字吧"
    return "分组名不能用：不能为空、不超过 40 个字，也不能叫「全部数据源」或「未分组」"


@router.post("/chat")
def chat(payload: ChatIn, request: Request) -> StreamingResponse:
    """提问，返回 text/event-stream。历史按 session_id 从库里取，回答完落库。

    落库时机：**问题先落**（这样上游挂了也能看到问过什么），回答在流结束时落。
    若一个字都没产出（比如上游 401），把刚才那条问题撤掉，免得库里留下没有回答的问答。
    """
    settings = request.app.state.settings
    sessions = get_sessions(request)
    if sessions.get(payload.session_id) is None:
        raise HTTPException(status_code=404, detail=f"没有这个会话：{payload.session_id}（先 POST /api/sessions 建一个）")
    if not settings.configured:
        raise HTTPException(status_code=400, detail="还没有配置 OPENROUTER_API_KEY，无法检索与回答")

    # 顺序要紧：先按「不含本轮提问」的历史构建会话，再把问题落库。
    # 反过来的话历史里已经有本轮问题，拼提示词时会再问一遍（实测踩过）。
    session = build_chat_session(request, payload.session_id)
    sessions.title_from_question(payload.session_id, payload.question)  # 首次提问后据首问命名
    question_id = sessions.append(payload.session_id, "user", payload.question)

    def events():
        sources: list[dict] = []
        answer: list[str] = []
        try:
            for event in session.ask(payload.question):
                if isinstance(event, RetrievalEvent):
                    sources = [
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
                    ]
                    frame = {"type": "sources", "items": sources}
                elif isinstance(event, TokenEvent):
                    answer.append(event.text)
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
        finally:
            # 无论正常结束、报错、还是前端中途断开（生成器被关闭），都在这里落库
            text = "".join(answer)
            if text:
                sessions.append(payload.session_id, "assistant", text, sources=sources)
            else:
                sessions.drop_message(question_id)

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/chat/clear", response_model=ChatClearOut)
def clear_chat(payload: ChatClearIn, request: Request) -> ChatClearOut:
    """清空某个会话的消息（会话本身保留，列表里还在）。"""
    sessions = get_sessions(request)
    if sessions.get(payload.session_id) is None:
        raise HTTPException(status_code=404, detail=f"没有这个会话：{payload.session_id}")
    cleared = sessions.clear(payload.session_id)
    return ChatClearOut(cleared=cleared, turns=cleared // 2)


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

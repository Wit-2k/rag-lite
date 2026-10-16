"""调用 OpenRouter 的嵌入接口，把分块结果变成向量。

接口形状来自 OpenRouter 官方文档 + `backend/help/demo.py` 实测：
  POST https://openrouter.ai/api/v1/embeddings

  · 纯文本：input = ["文本1", "文本2"]，一次可以放多条（本模块默认 16 条一批）
  · 图文混排：input = [{"content": [{"type": "text", ...},
                                   {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}]}]
  · dimensions 只支持 256 / 512 / 1024 / 2048，本项目用 2048（demo.py 里已查证）
  · 返回 data[i] = {"index": i, "embedding": [...]}，另外带 usage（prompt_tokens / cost）

实测注意点：图文混排的图片也按 token 计费（usage.prompt_tokens_details.image_tokens），
一次请求里混着"纯文本条目"和"图文条目"容易被上游挑刺，所以本模块把两类分开：
纯文本按批发，带图的块一条一个请求（图片本来就少）。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import requests

from app.core.chunking import Block

# —— 常量：接口地址、模型、维度、密钥文件位置 ————————————————————————————

EMBED_URL = "https://openrouter.ai/api/v1/embeddings"
RERANK_URL = "https://openrouter.ai/api/v1/rerank"
DEFAULT_MODEL = "voyageai/voyage-multimodal-3.5"
DEFAULT_DIM = 2048  # 该模型支持 256 / 512 / 1024 / 2048
ALLOWED_DIMS = (256, 512, 1024, 2048)

DEFAULT_RERANK_MODEL = "voyageai/rerank-3"
# 重排前把单篇文档截断到这个长度：模型对「查询-文档」合计上限是 32K token（查询 ≤8K），
# 我们单块最长约 1400 字符，4000 字符足够留出余量，也顺手压住费用。
DEFAULT_RERANK_DOC_CHARS = 4000
# 上游没回 cost 时的兜底单价（$0.05 / 百万 token，来自 OpenRouter 模型页），仅用于报表估算
RERANK_COST_PER_MTOKEN = 0.05

# 回答用的模型：任意 OpenAI Compatible 端点都行，默认走 OpenRouter 上一个便宜好用的
DEFAULT_CHAT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_CHAT_MODEL = "google/gemini-2.5-flash"

# backend/.env（该文件已被 .gitignore 忽略，只放本机密钥，不要提交）
DEFAULT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# 上游允许重试的状态码：限流 + 各种临时故障；400 之类的参数错误不该重试
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


class OpenRouterError(RuntimeError):
    """调用 OpenRouter 失败（参数错、配额、上游故障等），消息里会带响应片段。"""


# —— 结果类型：向量 + 这次调用的花销 ————————————————————————————————————


@dataclass(frozen=True, slots=True)
class EmbedUsage:
    """一次（或多次）嵌入调用的用量汇总，用于在报告里如实汇报花了多少。"""

    requests: int = 0
    prompt_tokens: int = 0
    image_tokens: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0

    def __add__(self, other: "EmbedUsage") -> "EmbedUsage":
        return EmbedUsage(
            requests=self.requests + other.requests,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            image_tokens=self.image_tokens + other.image_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            seconds=self.seconds + other.seconds,
        )


@dataclass(slots=True)
class EmbedResult:
    """vectors 与传入的 blocks（或 texts）一一对应，顺序不变。"""

    vectors: list[list[float]]
    usage: EmbedUsage
    dim: int


# —— 密钥读取 ——————————————————————————————————————————————————————


def read_env_value(name: str, *, env_file: Path | None = DEFAULT_ENV_FILE, default: str | None = None) -> str | None:
    """按「环境变量 → .env 文件」的顺序取一个配置值。

    .env 只做最朴素的 KEY=VALUE 解析（支持 # 注释和首尾引号），不引入额外依赖。
    """
    import os

    from_env = os.environ.get(name, "").strip()
    if from_env:
        return from_env

    if env_file is not None and Path(env_file).is_file():
        for raw_line in Path(env_file).read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name:
                return value.strip().strip("'\"")
    return default


def read_api_key(*, env_file: Path | None = DEFAULT_ENV_FILE) -> str:
    """找 OPENROUTER_API_KEY；找不到就抛错，并提示该怎么配。"""
    key = read_env_value("OPENROUTER_API_KEY", env_file=env_file)
    if not key:
        raise OpenRouterError(
            f"没有找到 OPENROUTER_API_KEY：请设置环境变量，或写到 {env_file}（参考 backend/.env.example）"
        )
    return key


# —— 内部工具 ——————————————————————————————————————————————————————


def _data_uri(path: Path) -> str:
    """把本地图片转成 data URI：OpenRouter 只接受 http(s) 链接或 data URI，本地文件必须内联。"""
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _build_input(block: Block) -> str | dict:
    """把一块内容转成接口认识的条目：有图就图文混排，没图就是纯字符串（更省事也更快）。"""
    if not block.images:
        return block.text
    return {
        "content": [
            {"type": "text", "text": block.text},
            *(
                {"type": "image_url", "image_url": {"url": _data_uri(image)}}
                for image in block.images
            ),
        ]
    }


def _usage_of(payload: dict, seconds: float) -> EmbedUsage:
    """从响应的 usage 里抠出用量；上游字段有缺就按 0 处理，不让报表因为缺字段挂掉。"""
    usage = payload.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    return EmbedUsage(
        requests=1,
        prompt_tokens=int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0),
        image_tokens=int(details.get("image_tokens") or 0),
        cost_usd=float(usage.get("cost") or 0.0),
        seconds=seconds,
    )


class _Client:
    """薄薄一层 HTTP 封装：发请求、按需重试、把错误信息包装成能看懂的中文异常。

    只认「一个地址 + 一个 payload」，嵌入和重排共用同一套重试/报错逻辑。
    """

    def __init__(
        self,
        *,
        api_key: str,
        url: str,
        timeout_s: float,
        max_retries: int,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = api_key
        self.url = url
        self.timeout_s = timeout_s
        self.max_retries = max(1, max_retries)
        self.session = session or requests.Session()
        self.sleep = sleep
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # 这两个头是可选的，只为在 OpenRouter 后台能认出是哪个应用发来的请求
            "HTTP-Referer": "https://github.com/rag-lite",
            "X-Title": "rag-lite",
        }

    def post(self, payload: dict) -> tuple[dict, float]:
        """发一次请求，返回 (响应 JSON, 本次耗时秒数)。失败会按退避策略重试。"""
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            started = time.perf_counter()
            try:
                response = self.session.post(
                    self.url, headers=self.headers, json=payload, timeout=self.timeout_s
                )
            except requests.RequestException as exc:  # 网络层问题，值得重试
                last_error = OpenRouterError(f"请求失败：{exc}")
                wait = float(attempt)
            else:
                elapsed = time.perf_counter() - started
                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, dict) and data.get("error"):
                        # 少数情况下 200 里也塞了错误体
                        raise OpenRouterError(f"接口返回错误：{json.dumps(data['error'], ensure_ascii=False)[:300]}")
                    return data, elapsed
                body = (getattr(response, "text", "") or "")[:300]
                last_error = OpenRouterError(f"HTTP {response.status_code}：{body}")
                if response.status_code not in RETRYABLE_STATUS:
                    raise last_error
                retry_after = getattr(response, "headers", {}) or {}
                try:
                    wait = float(retry_after.get("Retry-After"))
                except (TypeError, ValueError):
                    wait = float(2 ** (attempt - 1))
            if attempt == self.max_retries:
                break
            self.sleep(min(wait, 30.0))
        raise last_error or OpenRouterError("未知错误")


def _embed_batch(
    client: _Client,
    items: Sequence[str | dict],
    *,
    model: str,
    dim: int,
    input_type: str | None,
) -> tuple[list[list[float]], EmbedUsage]:
    """发一批嵌入请求，返回按 index 排好序的向量与用量。"""
    payload: dict = {
        "model": model,
        "dimensions": dim,
        "encoding_format": "float",
        "input": list(items),
    }
    if input_type:
        # Voyage 系模型区分「文档」和「查询」，检索时两边的向量才在同一个空间里对齐
        payload["input_type"] = input_type

    data, elapsed = client.post(payload)
    rows = data.get("data") or []
    if len(rows) != len(items):
        raise OpenRouterError(f"返回条数不符：期望 {len(items)} 条，实际 {len(rows)} 条")

    ordered: list[list[float] | None] = [None] * len(items)
    for position, row in enumerate(rows):
        index = int(row.get("index", position))  # 上游可能乱序，用 index 归位更稳
        vector = row.get("embedding")
        if not isinstance(vector, list) or not vector:
            raise OpenRouterError(f"第 {index} 条没有 embedding：{str(row)[:200]}")
        if len(vector) != dim:
            raise OpenRouterError(f"向量维度不符：期望 {dim}，实际 {len(vector)}（检查 dimensions 参数）")
        ordered[index] = [float(value) for value in vector]
    missing = [i for i, vector in enumerate(ordered) if vector is None]
    if missing:
        raise OpenRouterError(f"缺少第 {missing[:5]} 条的向量")
    return [vector for vector in ordered if vector is not None], _usage_of(data, elapsed)


def _rerank_batch(
    client: _Client,
    documents: Sequence[str],
    *,
    model: str,
    query: str,
    top_n: int | None,
) -> tuple[list[int], list[float], int, float, float]:
    """发一次重排请求，返回 (原下标顺序, 分数, token 数, 费用, 耗时)，分数从高到低。"""
    payload: dict = {
        "model": model,
        "query": query,
        "documents": list(documents),  # 纯文本文档直接传字符串即可（接口也支持 {"text": ...} 对象）
    }
    if top_n is not None:
        payload["top_n"] = top_n

    data, elapsed = client.post(payload)
    rows = data.get("results") or []
    if not rows:
        raise OpenRouterError(f"重排没有返回任何结果：{json.dumps(data, ensure_ascii=False)[:200]}")

    pairs: list[tuple[int, float]] = []
    for row in rows:
        index = int(row.get("index", -1))
        if not 0 <= index < len(documents):
            raise OpenRouterError(f"重排返回的下标越界：{index}（文档共 {len(documents)} 篇）")
        pairs.append((index, float(row.get("relevance_score") or 0.0)))
    if len({index for index, _ in pairs}) != len(pairs):
        raise OpenRouterError("重排结果里有重复下标，无法对应回原文档")

    pairs.sort(key=lambda pair: pair[1], reverse=True)  # 上游一般已排序，这里再排一次保险

    usage = data.get("usage") or {}
    tokens = int(usage.get("total_tokens") or 0)
    cost = usage.get("cost")
    cost_usd = float(cost) if cost is not None else tokens * RERANK_COST_PER_MTOKEN / 1_000_000
    return [index for index, _ in pairs], [score for _, score in pairs], tokens, cost_usd, elapsed


def _batches(chars: Sequence[int], *, batch: int, max_chars: int) -> list[list[int]]:
    """按「条数 + 字符数」双上限切批：单批太大既慢又容易踩上游限制。"""
    groups: list[list[int]] = []
    current: list[int] = []
    current_chars = 0
    for index, char_count in enumerate(chars):
        if current and (len(current) >= batch or current_chars + char_count > max_chars):
            groups.append(current)
            current, current_chars = [], 0
        current.append(index)
        current_chars += char_count
    if current:
        groups.append(current)
    return groups


# —— 对外主函数 ————————————————————————————————————————————————————


def embed_texts(
    texts: Sequence[str],
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    dim: int = DEFAULT_DIM,
    batch: int = 16,
    max_chars_per_request: int = 32000,
    input_type: str | None = "search_document",
    timeout_s: float = 120.0,
    max_retries: int = 4,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[int, int], None] | None = None,
) -> EmbedResult:
    """把一串文本变成向量（用于提问时的查询向量，或纯文本入库）。"""
    if dim not in ALLOWED_DIMS:
        raise OpenRouterError(f"dimensions 只能是 {ALLOWED_DIMS}，收到 {dim}")
    if not texts:
        return EmbedResult(vectors=[], usage=EmbedUsage(), dim=dim)

    client = _Client(
        api_key=api_key,
        url=EMBED_URL,
        timeout_s=timeout_s,
        max_retries=max_retries,
        session=session,
        sleep=sleep,
    )
    vectors: list[list[float] | None] = [None] * len(texts)
    usage = EmbedUsage()
    chars = [len(text) for text in texts]
    done = 0
    for group in _batches(chars, batch=batch, max_chars=max_chars_per_request):
        batch_vectors, batch_usage = _embed_batch(
            client, [texts[i] for i in group], model=model, dim=dim, input_type=input_type
        )
        for offset, index in enumerate(group):
            vectors[index] = batch_vectors[offset]
        usage = usage + batch_usage
        done += len(group)
        if on_progress:
            on_progress(done, len(texts))
    return EmbedResult(vectors=[v for v in vectors if v is not None], usage=usage, dim=dim)


def embed_blocks(
    blocks: Sequence[Block],
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    dim: int = DEFAULT_DIM,
    batch: int = 16,
    max_chars_per_request: int = 32000,
    input_type: str | None = "search_document",
    timeout_s: float = 120.0,
    max_retries: int = 4,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[int, int], None] | None = None,
) -> EmbedResult:
    """把分块结果整批变成向量，返回顺序与 blocks 一致。

    纯文本块按批发送；带图片的块一条一个请求（图文混排容易被上游挑剔，单独发最稳）。
    """
    plain = [index for index, block in enumerate(blocks) if not block.images]
    rich = [index for index, block in enumerate(blocks) if block.images]

    vectors: list[list[float] | None] = [None] * len(blocks)
    usage = EmbedUsage()
    done = 0

    if plain:
        plain_result = embed_texts(
            [blocks[index].text for index in plain],
            api_key=api_key,
            model=model,
            dim=dim,
            batch=batch,
            max_chars_per_request=max_chars_per_request,
            input_type=input_type,
            timeout_s=timeout_s,
            max_retries=max_retries,
            session=session,
            sleep=sleep,
            on_progress=None,
        )
        for offset, index in enumerate(plain):
            vectors[index] = plain_result.vectors[offset]
        usage = usage + plain_result.usage
        done += len(plain)
        if on_progress:
            on_progress(done, len(blocks))

    if rich:
        client = _Client(
            api_key=api_key,
            url=EMBED_URL,
            timeout_s=timeout_s,
            max_retries=max_retries,
            session=session,
            sleep=sleep,
        )
        for index in rich:
            batch_vectors, batch_usage = _embed_batch(
                client, [_build_input(blocks[index])], model=model, dim=dim, input_type=input_type
            )
            vectors[index] = batch_vectors[0]
            usage = usage + batch_usage
            done += 1
            if on_progress:
                on_progress(done, len(blocks))

    return EmbedResult(vectors=[v for v in vectors if v is not None], usage=usage, dim=dim)


# —— 重排 ————————————————————————————————————————————————————————


@dataclass(slots=True)
class RerankResult:
    """重排结果。`order` / `scores` 是**原文档下标**，已按相关度从高到低排好。"""

    order: list[int]
    scores: list[float]
    requests: int
    tokens: int
    cost_usd: float
    seconds: float

    def top(self, count: int) -> list[int]:
        return self.order[:count]


def rerank(
    query: str,
    documents: Sequence[str],
    *,
    api_key: str,
    model: str = DEFAULT_RERANK_MODEL,
    top_n: int | None = None,
    truncate_chars: int = DEFAULT_RERANK_DOC_CHARS,
    timeout_s: float = 120.0,
    max_retries: int = 4,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RerankResult:
    """按与 `query` 的相关度给 `documents` 重新排序。

    Args:
        top_n: 只让上游返回前 N 条；默认 None = 全部返回（便于对照观察重排前后的变化）。
        truncate_chars: 单篇文档截断长度，防止长表格把费用顶上去。
    """
    cleaned = [" ".join((document or "").split())[:truncate_chars] for document in documents]
    if not cleaned:
        return RerankResult(order=[], scores=[], requests=0, tokens=0, cost_usd=0.0, seconds=0.0)

    client = _Client(
        api_key=api_key,
        url=RERANK_URL,
        timeout_s=timeout_s,
        max_retries=max_retries,
        session=session,
        sleep=sleep,
    )
    order, scores, tokens, cost_usd, elapsed = _rerank_batch(
        client, cleaned, model=model, query=query, top_n=top_n
    )
    return RerankResult(
        order=order, scores=scores, requests=1, tokens=tokens, cost_usd=cost_usd, seconds=elapsed
    )


# —— 对话（回答生成）————————————————————————————————————————————


@dataclass(slots=True)
class ChatUsage:
    """一次回答的用量。上游没回 cost 时给 0，报表里按 0 显示即可。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


class ChatStream:
    """流式对话：`send(messages)` 逐段产出文本，迭代结束后从 `.usage` 读用量。

    用 `openai` SDK 调任意 OpenAI Compatible 端点（默认指向 OpenRouter）。
    实测：带上 `stream_options={"include_usage": True}` 后，OpenRouter 会在最后一段回 usage（含 cost）。
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = DEFAULT_CHAT_BASE_URL,
        temperature: float = 0.2,
        timeout_s: float = 120.0,
        client: object | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.usage = ChatUsage()
        self._client = client

    def _openai(self):
        if self._client is None:
            from openai import OpenAI  # 延迟导入：只有真的要回答时才需要它

            self._client = OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=self.timeout_s)
        return self._client

    def send(self, messages: list[dict]) -> Iterator[str]:
        """发送一轮对话，逐段 yield 文本增量。"""
        stream = self._openai().chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            temperature=self.temperature,
            # 只有带上这个参数，流式响应最后才会补一段 usage
            stream_options={"include_usage": True},
        )
        usage = ChatUsage()
        try:
            for chunk in stream:
                partial = getattr(chunk, "usage", None)
                if partial is not None:
                    usage = ChatUsage(
                        prompt_tokens=int(getattr(partial, "prompt_tokens", 0) or 0),
                        completion_tokens=int(getattr(partial, "completion_tokens", 0) or 0),
                        cost_usd=float(getattr(partial, "cost", 0.0) or 0.0),
                    )
                if not getattr(chunk, "choices", None):
                    continue  # 最后那段「只有 usage、没有 choices」的响应
                delta = chunk.choices[0].delta
                text = getattr(delta, "content", None)
                if text:
                    yield text
        finally:
            # 即使是调用方中途取消（提前 break），也要把已经拿到的用量留下
            self.usage = usage
